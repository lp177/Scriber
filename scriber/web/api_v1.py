"""Public read/write REST API (v1), authenticated by API tokens.

Mounted at ``/api/v1``. Read endpoints accept any valid token; write endpoints
require the ``readwrite`` scope. See :mod:`scriber.web.api_auth`. The endpoints
mirror the dashboard's data model and reuse its helpers so both stay in sync.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, Response

from scriber import database
from scriber.web import routes as dash
from scriber.web.api_auth import require_api_token, require_api_token_rw

router = APIRouter()


@router.get("/")
async def index(token: dict = Depends(require_api_token)) -> dict[str, Any]:
    """Discovery endpoint: the caller's scope and the available routes."""
    return {
        "name": "Scriber API",
        "version": "v1",
        "scope": token.get("scope"),
        "endpoints": [
            "GET  /api/v1/me",
            "GET  /api/v1/stats",
            "GET  /api/v1/meetings",
            "GET  /api/v1/meetings/{id}",
            "GET  /api/v1/meetings/{id}/transcript",
            "GET  /api/v1/meetings/{id}/summary",
            "GET  /api/v1/participants",
            "GET  /api/v1/participants/{id}",
            "GET  /api/v1/participants/{id}/memory",
            "GET  /api/v1/participants/{id}/avatar",
            "PUT  /api/v1/meetings/{id}/transcript   (readwrite)",
            "PUT  /api/v1/meetings/{id}/summary      (readwrite)",
            "PUT  /api/v1/participants/{id}          (readwrite)",
            "PUT  /api/v1/participants/{id}/memory   (readwrite)",
        ],
    }


@router.get("/me")
async def me(token: dict = Depends(require_api_token)) -> dict[str, Any]:
    """Return information about the calling token."""
    return {
        "name": token.get("name"),
        "scope": token.get("scope"),
        "created_at": token.get("created_at"),
        "last_used_at": token.get("last_used_at"),
    }


@router.get("/stats")
async def stats(request: Request, _t: dict = Depends(require_api_token)) -> dict[str, Any]:
    """Aggregate meeting statistics plus the live active-session count."""
    data = database.get_stats()
    bot = request.app.state.bot
    data["active_sessions"] = len(bot.active_sessions) if bot else 0
    return data


@router.get("/meetings")
async def meetings(
    limit: int = 50, offset: int = 0, _t: dict = Depends(require_api_token)
) -> dict[str, Any]:
    """Paginated meeting list, newest first (without the processing log)."""
    total, rows = database.list_meetings(limit=limit, offset=offset)
    items = [
        dash._with_file_flags({k: v for k, v in row.items() if k != "log"}) for row in rows
    ]
    return {"total": total, "items": items}


@router.get("/meetings/{meeting_id}")
async def meeting(meeting_id: str, _t: dict = Depends(require_api_token)) -> dict[str, Any]:
    """Full meeting row including the generation log."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return dash._with_file_flags(row)


@router.get("/meetings/{meeting_id}/transcript")
async def transcript(
    meeting_id: str, download: int = 0, _t: dict = Depends(require_api_token)
) -> Response:
    """Serve the transcript as plain text (or a download with ``download=1``)."""
    return dash._serve_meeting_file(
        meeting_id, "transcript_path", media_type="text/plain", download=bool(download)
    )


@router.get("/meetings/{meeting_id}/summary")
async def summary(
    meeting_id: str, download: int = 0, _t: dict = Depends(require_api_token)
) -> Response:
    """Serve the summary as Markdown (or a download with ``download=1``)."""
    return dash._serve_meeting_file(
        meeting_id, "summary_path", media_type="text/markdown", download=bool(download)
    )


@router.get("/participants")
async def participants(
    limit: int = 50, offset: int = 0, _t: dict = Depends(require_api_token)
) -> dict[str, Any]:
    """Paginated participant list, most recent session first."""
    total, rows = database.list_users(limit=limit, offset=offset)
    items = [
        {
            "id": row["id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "has_avatar": dash._resolve_data_file(row.get("avatar_path")) is not None,
            "session_count": row["session_count"],
            "last_session_at": row["last_session_at"],
        }
        for row in rows
    ]
    return {"total": total, "items": items}


@router.get("/participants/{user_id}")
async def participant(user_id: str, _t: dict = Depends(require_api_token)) -> dict[str, Any]:
    """Full participant profile including memory content and joined sessions."""
    row = database.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    data = dash._user_detail_base(row)
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


@router.get("/participants/{user_id}/memory")
async def participant_memory(
    user_id: str, _t: dict = Depends(require_api_token)
) -> dict[str, str]:
    """Return the participant's raw Markdown memory file."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    return {"content": dash._memory_manager().read(user_id)}


@router.get("/participants/{user_id}/avatar")
async def participant_avatar(user_id: str, _t: dict = Depends(require_api_token)) -> Response:
    """Serve the participant's avatar image."""
    row = database.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    path = dash._resolve_data_file(row.get("avatar_path"))
    if path is None:
        raise HTTPException(status_code=404, detail="Avatar not found")
    media_type = dash._AVATAR_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(path, media_type=media_type)


# --------------------------- write endpoints (readwrite scope) ---------------

@router.put("/meetings/{meeting_id}/transcript")
async def put_transcript(
    meeting_id: str, body: dash.ContentBody, _t: dict = Depends(require_api_token_rw)
) -> dict[str, bool]:
    """Overwrite a meeting's transcript file."""
    return dash._write_meeting_file(meeting_id, "transcript_path", ".txt", body.content)


@router.put("/meetings/{meeting_id}/summary")
async def put_summary(
    meeting_id: str, body: dash.ContentBody, _t: dict = Depends(require_api_token_rw)
) -> dict[str, bool]:
    """Overwrite a meeting's summary file."""
    return dash._write_meeting_file(meeting_id, "summary_path", ".md", body.content)


@router.put("/participants/{user_id}")
async def put_participant(
    user_id: str, body: dash.UserUpdate, _t: dict = Depends(require_api_token_rw)
) -> dict[str, Any]:
    """Update a participant's display name and/or description."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    fields: dict[str, str] = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name
    if body.description is not None:
        fields["description"] = body.description
    if fields:
        database.update_user(user_id, **fields)
    return {"ok": True, "participant": dash._user_detail_base(database.get_user(user_id))}


@router.put("/participants/{user_id}/memory")
async def put_participant_memory(
    user_id: str, body: dash.ContentBody, _t: dict = Depends(require_api_token_rw)
) -> dict[str, bool]:
    """Overwrite a participant's Markdown memory file."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="Participant not found")
    dash._memory_manager().write(user_id, body.content)
    return {"ok": True}
