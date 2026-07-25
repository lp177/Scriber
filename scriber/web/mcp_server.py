"""Optional MCP server: AI-assistant access to Scriber's stored data.

A thin Model Context Protocol (streamable HTTP) wrapper around the same data
the REST API (``/api/v1``) serves: meetings, transcripts, summaries,
participants and per-participant memory. It listens on its own port
(``MCP_HOST``/``MCP_PORT``, default 8081) so each deployment decides whether
to publish it at all — the compose example binds it to the host loopback only.

Authentication reuses the REST API tokens (``Authorization: Bearer <token>``,
created in the dashboard under Settings → API access): every HTTP request
must carry a valid token, and the ``update_*`` tools additionally require the
``readwrite`` scope. The tools mirror the ``/api/v1`` endpoints and reuse the
dashboard helpers so all three layers stay in sync.

``scriber.__main__`` imports this module lazily: without the optional ``mcp``
package the MCP listener is skipped with a warning instead of stopping the
bot or the dashboard.
"""

from __future__ import annotations

import contextvars
import json
import logging
from typing import Any

from fastapi import HTTPException
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError

from scriber import database
from scriber.web import routes as dash
from scriber.web.api_auth import authenticate_bearer

log = logging.getLogger(__name__)

# Token row of the request currently being served, set by _BearerAuthASGI.
# Contextvars propagate into the tasks the MCP session spawns while handling
# a request, which is how the tools see the scope of the calling token.
_current_token: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "scriber_mcp_token", default=None
)

_UNAUTHORIZED_BODY = json.dumps(
    {
        "error": (
            "A valid API token is required: send 'Authorization: Bearer <token>'. "
            "Create one in the Scriber dashboard under Settings → API access."
        )
    }
).encode("utf-8")


class _BearerAuthASGI:
    """Pure ASGI middleware enforcing API-token auth on every HTTP request.

    Auth happens at the transport layer so no MCP handler can run
    unauthenticated, whatever the protocol exchange looks like. A pure ASGI
    wrapper (rather than Starlette's BaseHTTPMiddleware) keeps the downstream
    app in the same task, so the context variable set here is visible to it.
    """

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":  # pass lifespan straight through
            await self.app(scope, receive, send)
            return
        authorization = next(
            (
                value.decode("latin-1")
                for name, value in scope.get("headers", [])
                if name.lower() == b"authorization"
            ),
            None,
        )
        client = scope.get("client")
        row = authenticate_bearer(authorization, client[0] if client else None)
        if row is None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"www-authenticate", b"Bearer"),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        reset = _current_token.set(row)
        try:
            await self.app(scope, receive, send)
        finally:
            _current_token.reset(reset)


def _require_readwrite() -> None:
    """Fail (closed: also on a missing token row) unless the caller may write."""
    row = _current_token.get()
    if row is None or row.get("scope") != "readwrite":
        raise ToolError("This API token is read-only (needs the read & write scope).")


def _call(fn: Any, *args: Any, **kwargs: Any) -> Any:
    """Call a dashboard helper, converting HTTPException to a clean tool error."""
    try:
        return fn(*args, **kwargs)
    except HTTPException as exc:
        raise ToolError(str(exc.detail)) from exc


def _meeting_or_fail(meeting_id: str) -> dict[str, Any]:
    row = database.get_meeting(meeting_id)
    if row is None:
        raise ToolError("Meeting not found")
    return row


def _user_or_fail(user_id: str) -> dict[str, Any]:
    row = database.get_user(user_id)
    if row is None:
        raise ToolError("Participant not found")
    return row


def _read_meeting_file(meeting_id: str, column: str, kind: str) -> str:
    """Return the text of a meeting's transcript/summary file."""
    row = _meeting_or_fail(meeting_id)
    path = dash._resolve_data_file(row.get(column))
    if path is None:
        raise ToolError(f"This meeting has no {kind}")
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Failed to read the {kind} file") from exc


def create_mcp_app(bot: Any | None = None) -> Any:
    """Build the MCP ASGI app: the Scriber toolset wrapped in bearer auth.

    Stateless + JSON responses: every POST is self-contained, so any MCP
    client can connect without session bookkeeping and the server survives
    restarts mid-conversation.
    """
    server = FastMCP(
        "Scriber",
        instructions=(
            "Data recorded by Scriber, a self-hosted Discord meeting-recording "
            "bot: meetings with their transcripts and Markdown minutes, the "
            "participants (keyed by stable Discord user id), and a per-"
            "participant Markdown memory that is injected as context when "
            "summarizing their future meetings. Read tools work with any API "
            "token; update tools need one with the read & write scope."
        ),
        stateless_http=True,
        json_response=True,
    )

    # ------------------------------- read tools ------------------------------

    @server.tool()
    def get_stats() -> dict[str, Any]:
        """Aggregate statistics: meeting totals by status, total recorded duration and word count, participant count, meetings per day (last 30 days), top guilds, and how many recording sessions are active right now."""
        data = database.get_stats()
        data["active_sessions"] = len(bot.active_sessions) if bot else 0
        return data

    @server.tool()
    def list_meetings(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List recorded meetings, newest first, paginated (returns total plus items). Each item has the meeting id, guild and voice channel names, start/end times, status (recording, summarizing, completed, error or cancelled), duration, word count, and has_transcript/has_summary flags."""
        total, rows = database.list_meetings(limit=limit, offset=offset)
        items = [
            dash._with_file_flags({k: v for k, v in row.items() if k != "log"})
            for row in rows
        ]
        return {"total": total, "items": items}

    @server.tool()
    def get_meeting(meeting_id: str) -> dict[str, Any]:
        """Full detail of one meeting, including its processing log and its participants (Discord user ids and display names)."""
        data = dash._with_file_flags(_meeting_or_fail(meeting_id))
        data["participants"] = database.get_meeting_participants(meeting_id)
        return data

    @server.tool()
    def get_transcript(meeting_id: str) -> str:
        """A meeting's transcript: plain text, one line per spoken segment with a timestamp and the speaker's name."""
        return _read_meeting_file(meeting_id, "transcript_path", "transcript")

    @server.tool()
    def get_summary(meeting_id: str) -> str:
        """A meeting's AI-generated minutes, as Markdown."""
        return _read_meeting_file(meeting_id, "summary_path", "summary")

    @server.tool()
    def list_transcript_versions(meeting_id: str) -> dict[str, Any]:
        """A meeting's transcript versions: the original live transcript (id 'original') plus any versions regenerated from the kept meeting audio with a different engine (Whisper profiles, Voxtral, ElevenLabs Scribe, Google Chirp). Also reports whether regeneration is currently possible and any running regeneration job."""
        return _call(dash._transcript_versions, meeting_id)

    @server.tool()
    def get_transcript_version(meeting_id: str, transcript_id: str) -> str:
        """The text of one transcript version. Use transcript_id 'original' for the live transcript, or an id from list_transcript_versions for a regenerated one."""
        path = _call(dash._resolve_transcript_version, meeting_id, transcript_id)
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ToolError("Failed to read the transcript version file") from exc

    @server.tool()
    def list_participants(limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """List everyone who attended at least one recorded meeting, most recent session first, paginated (returns total plus items). Participants are keyed by their stable Discord user id."""
        total, rows = database.list_users(limit=limit, offset=offset)
        items = [
            {
                "id": row["id"],
                "display_name": row["display_name"],
                "description": row["description"],
                "session_count": row["session_count"],
                "last_session_at": row["last_session_at"],
            }
            for row in rows
        ]
        return {"total": total, "items": items}

    @server.tool()
    def get_participant(user_id: str) -> dict[str, Any]:
        """One participant's profile (display name, description), their memory notes, and every meeting they attended."""
        data = dash._user_detail_base(_user_or_fail(user_id))
        data["memory"] = dash._memory_manager().read(user_id)
        data["sessions"] = [
            {
                "id": m["id"],
                "started_at": m["started_at"],
                "guild_name": m["guild_name"],
                "voice_channel_name": m["voice_channel_name"],
                "status": m["status"],
                "has_summary": dash._resolve_data_file(m.get("summary_path")) is not None,
            }
            for m in database.get_user_meetings(user_id)
        ]
        return data

    @server.tool()
    def get_participant_memory(user_id: str) -> str:
        """A participant's memory notes: the Markdown file Scriber maintains about them (projects, vocabulary, proper nouns) and injects as context when summarizing meetings they attend."""
        _user_or_fail(user_id)
        return dash._memory_manager().read(user_id)

    # ------------------------- write tools (readwrite) -----------------------

    @server.tool()
    def update_transcript(meeting_id: str, content: str) -> dict[str, bool]:
        """Replace a meeting's transcript with new plain text. The whole file is overwritten — read it first to make a partial edit. Needs the read & write scope."""
        _require_readwrite()
        return _call(dash._write_meeting_file, meeting_id, "transcript_path", ".txt", content)

    @server.tool()
    def update_summary(meeting_id: str, content: str) -> dict[str, bool]:
        """Replace a meeting's Markdown minutes. The whole file is overwritten — read it first to make a partial edit. Needs the read & write scope."""
        _require_readwrite()
        return _call(dash._write_meeting_file, meeting_id, "summary_path", ".md", content)

    @server.tool()
    def update_participant(
        user_id: str, display_name: str | None = None, description: str | None = None
    ) -> dict[str, Any]:
        """Update a participant's display name and/or description; an omitted field is left unchanged. Needs the read & write scope."""
        _require_readwrite()
        _user_or_fail(user_id)
        fields: dict[str, str] = {}
        if display_name is not None:
            fields["display_name"] = display_name
        if description is not None:
            fields["description"] = description
        if fields:
            database.update_user(user_id, **fields)
        return {"ok": True, "participant": dash._user_detail_base(database.get_user(user_id))}

    @server.tool()
    def update_participant_memory(user_id: str, content: str) -> dict[str, bool]:
        """Replace a participant's Markdown memory notes. The whole file is overwritten — read it first to make a partial edit. Needs the read & write scope."""
        _require_readwrite()
        _user_or_fail(user_id)
        dash._memory_manager().write(user_id, content)
        return {"ok": True}

    return _BearerAuthASGI(server.streamable_http_app())
