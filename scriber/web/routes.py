"""REST API routes for the Scriber admin dashboard."""

from __future__ import annotations

import hmac
import logging
import secrets
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from scriber import audio, config, database
from scriber.memory import MemoryManager
from scriber.transcription import providers as stt_providers
from scriber.transcription import regen
from scriber.web import api_auth
from scriber.web.auth import create_token, require_auth

logger = logging.getLogger(__name__)

router = APIRouter()


class LoginRequest(BaseModel):
    """Credentials submitted by the dashboard login form."""

    username: str
    password: str


class UserUpdate(BaseModel):
    """Editable user profile fields; omitted fields are left unchanged."""

    display_name: str | None = None
    description: str | None = None


class ContentBody(BaseModel):
    """Request body carrying a single ``content`` text field."""

    content: str


class TokenCreate(BaseModel):
    """Request to mint a new API token."""

    name: str
    scope: str = "read"


class TokenUpdate(BaseModel):
    """Rename an API token and/or change its scope."""

    name: str | None = None
    scope: str | None = None


class RegenRequest(BaseModel):
    """Request to regenerate a meeting transcript with a different engine."""

    engine: str
    model: str | None = None
    language: str | None = None


# Maximum accepted avatar upload size (5 MB).
_MAX_AVATAR_BYTES = 5 * 1024 * 1024

# Accepted upload content types mapped to the extension we store on disk.
_AVATAR_CONTENT_TYPES: dict[str, str] = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}

# On-disk extension mapped to the media type used when serving the avatar.
_AVATAR_MEDIA_TYPES: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

# Kept meeting audio: on-disk extension mapped to the served media type.
_AUDIO_MEDIA_TYPES: dict[str, str] = {
    ".ogg": "audio/ogg",
    ".wav": "audio/wav",
}


def _manager() -> config.ConfigManager:
    """Return the initialized ConfigManager singleton or fail with a 500."""
    if config.manager is None:
        raise HTTPException(status_code=500, detail="Configuration manager is not initialized")
    return config.manager


def _resolve_data_file(path_value: Any) -> Path | None:
    """Resolve a DB-stored file path; return it only if it exists under the data dir."""
    if not path_value or not isinstance(path_value, str):
        return None
    data_dir = config.get().data_dir.resolve()
    try:
        candidate = Path(path_value).resolve()
    except OSError:
        return None
    if not candidate.is_relative_to(data_dir):
        logger.warning("Refusing to serve file outside data dir: %s", path_value)
        return None
    if not candidate.is_file():
        return None
    return candidate


def _with_file_flags(row: dict[str, Any]) -> dict[str, Any]:
    """Add has_transcript / has_summary / has_audio booleans to a meeting row."""
    row["has_transcript"] = _resolve_data_file(row.get("transcript_path")) is not None
    row["has_summary"] = _resolve_data_file(row.get("summary_path")) is not None
    row["has_audio"] = _resolve_data_file(row.get("audio_path")) is not None
    return row


def _serve_meeting_file(meeting_id: str, column: str, media_type: str, download: bool) -> Response:
    """Serve the file referenced by *column* of the meeting row, inline or as attachment."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    path = _resolve_data_file(row.get(column))
    if path is None:
        raise HTTPException(status_code=404, detail="File not found")
    if download:
        return FileResponse(path, media_type=media_type, filename=path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="File not found") from exc
    return PlainTextResponse(text, media_type=media_type)


def _serve_meeting_audio(meeting_id: str, download: bool) -> Response:
    """Serve a meeting's kept audio file (inline for playback, or as a download)."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    path = _resolve_data_file(row.get("audio_path"))
    if path is None:
        raise HTTPException(status_code=404, detail="No audio is stored for this meeting")
    media_type = _AUDIO_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    if download:
        return FileResponse(path, media_type=media_type, filename=f"scriber-{path.name}")
    return FileResponse(path, media_type=media_type)


def _transcript_versions(meeting_id: str) -> dict[str, Any]:
    """List a meeting's transcript versions plus regeneration state.

    The original live transcript is presented as the pseudo-version
    ``"original"``; alternate versions come from the ``meeting_transcripts``
    table. Embeds the active/last regeneration job and the engine catalog so
    the dashboard needs a single request.
    """
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    items: list[dict[str, Any]] = []
    if _resolve_data_file(row.get("transcript_path")) is not None:
        items.append(
            {
                "id": "original",
                "engine": "whisper",
                "label": "Original (live recording)",
                "created_at": row.get("ended_at"),
            }
        )
    items.extend(
        {
            "id": version["id"],
            "engine": version["engine"],
            "label": version["label"],
            "created_at": version["created_at"],
        }
        for version in database.list_transcripts(meeting_id)
        if _resolve_data_file(version.get("path")) is not None
    )
    audio_dir = config.get().data_dir / "audio"
    return {
        "items": items,
        "job": regen.get_job(meeting_id),
        "can_regenerate": audio.segments_available(audio_dir, meeting_id),
        "engines": stt_providers.engine_catalog(),
    }


def _resolve_transcript_version(meeting_id: str, transcript_id: str) -> Path:
    """Return the file of a transcript version (``"original"`` or a row id)."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if transcript_id == "original":
        path = _resolve_data_file(row.get("transcript_path"))
    else:
        version = database.get_transcript(meeting_id, transcript_id)
        path = _resolve_data_file(version.get("path")) if version is not None else None
    if path is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")
    return path


def _serve_transcript_version(meeting_id: str, transcript_id: str, download: bool) -> Response:
    """Serve one transcript version as plain text, or as a download."""
    path = _resolve_transcript_version(meeting_id, transcript_id)
    if download:
        return FileResponse(path, media_type="text/plain", filename=path.name)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=404, detail="Transcript version not found") from exc
    return PlainTextResponse(text, media_type="text/plain")


def _memory_manager() -> MemoryManager:
    """Build a MemoryManager rooted at ``{data_dir}/memory``."""
    return MemoryManager(config.get().data_dir / "memory")


def _avatar_file(user_id: str, ext: str) -> Path:
    """Return a validated avatar file path under ``{data_dir}/avatars``.

    Guards against path traversal via a crafted user id or extension.
    """
    avatars_dir = (config.get().data_dir / "avatars").resolve()
    candidate = (avatars_dir / f"{user_id}{ext}").resolve()
    if not candidate.is_relative_to(avatars_dir):
        raise HTTPException(status_code=400, detail="Invalid user id")
    return candidate


def _user_detail_base(row: dict[str, Any]) -> dict[str, Any]:
    """Common user fields for detail/update responses (no memory or sessions)."""
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "description": row["description"],
        "has_avatar": _resolve_data_file(row.get("avatar_path")) is not None,
        "session_count": row.get("session_count", 0),
        "last_session_at": row.get("last_session_at"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _write_meeting_file(
    meeting_id: str, column: str, suffix: str, content: str
) -> dict[str, bool]:
    """Write text to a meeting's transcript/summary file under the data dir.

    Uses the existing path when the row already has one, otherwise a default
    ``{data_dir}/transcripts/{id}{suffix}`` path, recording it when it was null.
    """
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    data_dir = config.get().data_dir.resolve()
    path = _resolve_data_file(row.get(column))
    if path is None:
        path = (data_dir / "transcripts" / f"{meeting_id}{suffix}").resolve()
    if not path.is_relative_to(data_dir):
        raise HTTPException(status_code=400, detail="Invalid file path")
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(content, encoding="utf-8")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to write file") from exc
    if not row.get(column):
        database.update_meeting(meeting_id, **{column: str(path)})
    return {"ok": True}


@router.post("/login")
async def login(body: LoginRequest) -> dict[str, str]:
    """Check admin credentials and return a signed session token."""
    cfg = config.get()
    username_ok = hmac.compare_digest(
        body.username.encode("utf-8"), cfg.admin_username.encode("utf-8")
    )
    password_ok = hmac.compare_digest(
        body.password.encode("utf-8"), cfg.admin_password.encode("utf-8")
    )
    if not (username_ok and password_ok):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    return {"token": create_token(cfg.admin_username, cfg.web_secret)}


@router.get("/health")
async def health(request: Request) -> dict[str, Any]:
    """Unauthenticated health check with Discord connection status.

    Includes a short, non-sensitive ``notice`` when the bot needs operator
    attention (not configured, or not invited / missing permissions) so the
    login page can warn a visitor before they even sign in. The detailed error
    and the invite link stay behind auth on ``/bot-status``.
    """
    bot = request.app.state.bot
    setup_error = getattr(bot, "setup_error", None) if bot is not None else None
    connected = bool(bot is not None and bot.is_ready())
    notice: str | None = None
    if bot is None:
        notice = (
            "No Discord bot is configured yet (DISCORD_TOKEN is unset). The "
            "dashboard works, but recording stays disabled until a token is set."
        )
    elif setup_error:
        notice = (
            "The Discord bot needs attention — it may not be invited to your "
            "server, be missing permissions, or be misconfigured. Sign in to "
            "see the details and how to fix it."
        )
    return {
        "status": "ok",
        "bot_configured": bot is not None,
        "bot_connected": connected,
        "notice": notice,
    }


@router.get("/stats")
async def stats(request: Request, _user: str = Depends(require_auth)) -> dict[str, Any]:
    """Aggregate meeting statistics plus the live active-session count."""
    data = database.get_stats()
    bot = request.app.state.bot
    data["active_sessions"] = len(bot.active_sessions) if bot else 0
    return data


@router.get("/bot-status")
async def bot_status(request: Request, _user: str = Depends(require_auth)) -> dict[str, Any]:
    """Discord bot connection state plus any setup problem (e.g. refused command sync).

    ``setup_error`` is a human-readable message when the bot is degraded (with
    an ``invite_url`` to fix a missing-scope invite); both are None when healthy
    or when no bot is configured.
    """
    bot = request.app.state.bot
    if bot is None:
        return {"configured": False, "connected": False, "setup_error": None, "invite_url": None}
    return {
        "configured": True,
        "connected": bool(bot.is_ready()),
        "setup_error": getattr(bot, "setup_error", None),
        "invite_url": getattr(bot, "invite_url", None),
    }


@router.post("/bot-status/resync")
async def resync_bot_commands(
    request: Request, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Re-run the Discord slash-command sync without restarting the process.

    Lets an operator recover from a refused sync (e.g. right after inviting the
    bot with the ``applications.commands`` scope) straight from the dashboard.
    Returns the refreshed bot status; ``setup_error`` is null when the sync now
    succeeds.
    """
    bot = request.app.state.bot
    if bot is None:
        raise HTTPException(
            status_code=400, detail="No Discord bot is configured (DISCORD_TOKEN is unset)."
        )
    if not bot.is_ready():
        raise HTTPException(
            status_code=409,
            detail="The Discord bot is not connected yet. Wait a few seconds and try again.",
        )
    await bot.resync_commands()
    return {
        "configured": True,
        "connected": bool(bot.is_ready()),
        "setup_error": getattr(bot, "setup_error", None),
        "invite_url": getattr(bot, "invite_url", None),
    }


@router.get("/meetings")
async def list_meetings(
    limit: int = 50, offset: int = 0, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Paginated meeting list, newest first, without the log column."""
    total, rows = database.list_meetings(limit=limit, offset=offset)
    items = [
        _with_file_flags({k: v for k, v in row.items() if k != "log"}) for row in rows
    ]
    return {"total": total, "items": items}


@router.get("/meetings/{meeting_id}")
async def get_meeting(meeting_id: str, _user: str = Depends(require_auth)) -> dict[str, Any]:
    """Full meeting row including the generation log."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    return _with_file_flags(row)


@router.get("/meetings/{meeting_id}/transcript")
async def get_transcript(
    meeting_id: str, download: int = 0, _user: str = Depends(require_auth)
) -> Response:
    """Serve the meeting transcript as plain text, or as a download when download=1."""
    return _serve_meeting_file(
        meeting_id, "transcript_path", media_type="text/plain", download=bool(download)
    )


@router.get("/meetings/{meeting_id}/summary")
async def get_summary(
    meeting_id: str, download: int = 0, _user: str = Depends(require_auth)
) -> Response:
    """Serve the meeting summary as Markdown, or as a download when download=1."""
    return _serve_meeting_file(
        meeting_id, "summary_path", media_type="text/markdown", download=bool(download)
    )


@router.get("/meetings/{meeting_id}/audio")
async def get_audio(
    meeting_id: str, download: int = 0, _user: str = Depends(require_auth)
) -> Response:
    """Serve the meeting's kept audio, inline or as a download when download=1."""
    return _serve_meeting_audio(meeting_id, download=bool(download))


@router.get("/meetings/{meeting_id}/transcripts")
async def list_transcript_versions(
    meeting_id: str, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Transcript versions (original + regenerated), job state and engine catalog."""
    return _transcript_versions(meeting_id)


@router.get("/meetings/{meeting_id}/transcripts/{transcript_id}")
async def get_transcript_version(
    meeting_id: str, transcript_id: str, download: int = 0, _user: str = Depends(require_auth)
) -> Response:
    """Serve one transcript version (id ``original`` or a generated version id)."""
    return _serve_transcript_version(meeting_id, transcript_id, download=bool(download))


@router.post("/meetings/{meeting_id}/transcripts", status_code=202)
async def regenerate_transcript(
    meeting_id: str, body: RegenRequest, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Start regenerating the transcript from the archived audio (background job).

    409 when a job is already running for this meeting; 400 when the engine is
    unknown/unconfigured or the meeting has no archived audio segments.
    """
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if row.get("status") == "recording":
        raise HTTPException(
            status_code=409, detail="Meeting is still recording — stop it first"
        )
    try:
        job = regen.start_job(
            meeting_id, body.engine, body.model or "", body.language or ""
        )
    except ValueError as exc:
        status = 409 if "already running" in str(exc) else 400
        raise HTTPException(status_code=status, detail=str(exc)) from exc
    return {"ok": True, "job": job}


@router.delete("/meetings/{meeting_id}/transcripts/{transcript_id}")
async def delete_transcript_version(
    meeting_id: str, transcript_id: str, _user: str = Depends(require_auth)
) -> dict[str, bool]:
    """Delete a regenerated transcript version (the original cannot be deleted)."""
    if transcript_id == "original":
        raise HTTPException(status_code=400, detail="The original transcript cannot be deleted")
    version = database.get_transcript(meeting_id, transcript_id)
    if version is None:
        raise HTTPException(status_code=404, detail="Transcript version not found")
    path = _resolve_data_file(version.get("path"))
    if path is not None:
        try:
            path.unlink()
        except OSError:
            logger.warning("Failed to delete transcript file %s", path)
    database.delete_transcript(meeting_id, transcript_id)
    return {"ok": True}


@router.delete("/meetings/{meeting_id}")
async def delete_meeting(meeting_id: str, _user: str = Depends(require_auth)) -> dict[str, bool]:
    """Delete a meeting row and its files; refuse while it is still recording."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Meeting not found")
    if row.get("status") == "recording":
        raise HTTPException(
            status_code=409, detail="Meeting is currently recording and cannot be deleted"
        )
    job = regen.get_job(meeting_id)
    if job is not None and job.get("status") == "running":
        raise HTTPException(
            status_code=409,
            detail="A transcript regeneration is running for this meeting — "
            "wait for it to finish before deleting",
        )
    for column in ("transcript_path", "summary_path"):
        path = _resolve_data_file(row.get(column))
        if path is not None:
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete file %s for meeting %s", path, meeting_id)
    # Regenerated transcript versions and every kept audio artifact go too.
    for version in database.list_transcripts(meeting_id):
        path = _resolve_data_file(version.get("path"))
        if path is not None:
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete file %s for meeting %s", path, meeting_id)
    audio.delete_meeting_audio(config.get().data_dir / "audio", meeting_id)
    database.delete_meeting(meeting_id)
    regen.discard_job(meeting_id)
    return {"ok": True}


@router.put("/meetings/{meeting_id}/transcript")
async def put_transcript(
    meeting_id: str, body: ContentBody, _user: str = Depends(require_auth)
) -> dict[str, bool]:
    """Overwrite the meeting transcript file with client-supplied text."""
    return _write_meeting_file(meeting_id, "transcript_path", ".txt", body.content)


@router.put("/meetings/{meeting_id}/summary")
async def put_summary(
    meeting_id: str, body: ContentBody, _user: str = Depends(require_auth)
) -> dict[str, bool]:
    """Overwrite the meeting summary file with client-supplied Markdown."""
    return _write_meeting_file(meeting_id, "summary_path", ".md", body.content)


@router.get("/users")
async def list_users(
    limit: int = 50, offset: int = 0, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Paginated participant list, most recent session first."""
    total, rows = database.list_users(limit=limit, offset=offset)
    items = [
        {
            "id": row["id"],
            "display_name": row["display_name"],
            "description": row["description"],
            "has_avatar": _resolve_data_file(row.get("avatar_path")) is not None,
            "session_count": row["session_count"],
            "last_session_at": row["last_session_at"],
        }
        for row in rows
    ]
    return {"total": total, "items": items}


@router.get("/users/{user_id}")
async def get_user(user_id: str, _user: str = Depends(require_auth)) -> dict[str, Any]:
    """Full user profile including memory content and joined sessions."""
    row = database.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    data = _user_detail_base(row)
    data["memory"] = _memory_manager().read(user_id)
    data["sessions"] = [
        {
            "id": meeting["id"],
            "started_at": meeting["started_at"],
            "guild_name": meeting["guild_name"],
            "voice_channel_name": meeting["voice_channel_name"],
            "status": meeting["status"],
            "has_summary": _resolve_data_file(meeting.get("summary_path")) is not None,
        }
        for meeting in database.get_user_meetings(user_id)
    ]
    return data


@router.put("/users/{user_id}")
async def update_user(
    user_id: str, body: UserUpdate, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Update a user's display name and/or description."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    fields: dict[str, str] = {}
    if body.display_name is not None:
        fields["display_name"] = body.display_name
    if body.description is not None:
        fields["description"] = body.description
    if fields:
        database.update_user(user_id, **fields)
    row = database.get_user(user_id)
    return {"ok": True, "user": _user_detail_base(row)}


@router.get("/users/{user_id}/memory")
async def get_user_memory(
    user_id: str, _user: str = Depends(require_auth)
) -> dict[str, str]:
    """Return the raw Markdown memory file for a user."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"content": _memory_manager().read(user_id)}


@router.put("/users/{user_id}/memory")
async def put_user_memory(
    user_id: str, body: ContentBody, _user: str = Depends(require_auth)
) -> dict[str, bool]:
    """Overwrite a user's Markdown memory file."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    _memory_manager().write(user_id, body.content)
    return {"ok": True}


@router.post("/users/{user_id}/avatar")
async def upload_avatar(
    user_id: str,
    file: UploadFile = File(...),
    _user: str = Depends(require_auth),
) -> dict[str, Any]:
    """Store a user's avatar image (<=5 MB) and record its path."""
    if database.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="User not found")
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    ext = _AVATAR_CONTENT_TYPES.get(content_type)
    if ext is None:
        raise HTTPException(status_code=400, detail="Unsupported image type")
    data = await file.read()
    if len(data) > _MAX_AVATAR_BYTES:
        raise HTTPException(status_code=413, detail="Avatar exceeds the 5 MB limit")
    target = _avatar_file(user_id, ext)
    target.parent.mkdir(parents=True, exist_ok=True)
    # Remove any prior avatar stored under a different extension.
    for other_ext in _AVATAR_MEDIA_TYPES:
        if other_ext == ext:
            continue
        prior = _avatar_file(user_id, other_ext)
        if prior.exists():
            try:
                prior.unlink()
            except OSError:
                logger.warning("Failed to remove old avatar %s for user %s", prior, user_id)
    try:
        target.write_bytes(data)
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Failed to store avatar") from exc
    # Mark as a manual upload so the per-meeting Discord avatar sync won't overwrite it.
    database.set_avatar(user_id, str(target), "manual")
    return {"ok": True, "has_avatar": True}


@router.get("/users/{user_id}/avatar")
async def get_avatar(user_id: str, _user: str = Depends(require_auth)) -> Response:
    """Serve a user's avatar image, choosing the media type by file extension."""
    row = database.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    path = _resolve_data_file(row.get("avatar_path"))
    if path is None:
        raise HTTPException(status_code=404, detail="Avatar not found")
    media_type = _AVATAR_MEDIA_TYPES.get(path.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=404, detail="Avatar not found")
    return FileResponse(path, media_type=media_type)


@router.delete("/users/{user_id}")
async def delete_user(user_id: str, _user: str = Depends(require_auth)) -> dict[str, bool]:
    """Delete a user along with their avatar file, memory file, and DB rows."""
    row = database.get_user(user_id)
    if row is None:
        raise HTTPException(status_code=404, detail="User not found")
    avatar = _resolve_data_file(row.get("avatar_path"))
    if avatar is not None:
        try:
            avatar.unlink()
        except OSError:
            logger.warning("Failed to delete avatar %s for user %s", avatar, user_id)
    try:
        memory_path: Path | None = _memory_manager().path_for(user_id)
    except ValueError:
        memory_path = None
    if memory_path is not None and memory_path.exists():
        try:
            memory_path.unlink()
        except OSError:
            logger.warning(
                "Failed to delete memory file %s for user %s", memory_path, user_id
            )
    database.delete_user(user_id)
    return {"ok": True}


@router.get("/settings")
async def get_settings(_user: str = Depends(require_auth)) -> dict[str, Any]:
    """Current configuration fields with secrets masked."""
    return {"fields": _manager().display_fields()}


@router.put("/settings")
async def put_settings(
    changes: dict[str, str | int | float | bool], _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Persist editable configuration changes; 400 on any non-editable key.

    Scalar values are accepted (a number input naturally submits a JSON
    number); ``ConfigManager.update`` stringifies them before persisting.
    """
    manager = _manager()
    try:
        manager.update(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "fields": manager.display_fields()}


# ---------------------------------------------------------------------------
# API token management (dashboard-authenticated; the tokens themselves auth
# the separate /api/v1 data API — see scriber.web.api_auth / api_v1).
# ---------------------------------------------------------------------------


@router.get("/tokens")
async def list_tokens(_user: str = Depends(require_auth)) -> dict[str, Any]:
    """List all API tokens (metadata only — the secret is never returned)."""
    return {"tokens": database.list_api_tokens()}


@router.post("/tokens")
async def create_api_token(
    body: TokenCreate, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Mint a new API token and return the plaintext secret exactly once."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Token name is required")
    try:
        scope = api_auth.normalize_scope(body.scope)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    token = api_auth.generate_token()
    row = database.create_api_token(
        secrets.token_hex(8),
        name,
        api_auth.token_prefix(token),
        api_auth.hash_token(token),
        scope,
    )
    # The plaintext token is shown once here and never stored — only its hash is.
    return {"token": token, "api_token": row}


@router.patch("/tokens/{token_id}")
async def update_api_token(
    token_id: str, body: TokenUpdate, _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Rename an API token and/or change its scope."""
    name = body.name.strip() if body.name is not None else None
    if name is not None and not name:
        raise HTTPException(status_code=400, detail="Token name cannot be empty")
    scope: str | None = None
    if body.scope is not None:
        try:
            scope = api_auth.normalize_scope(body.scope)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    row = database.update_api_token(token_id, name=name, scope=scope)
    if row is None:
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True, "api_token": row}


@router.delete("/tokens/{token_id}")
async def delete_api_token(token_id: str, _user: str = Depends(require_auth)) -> dict[str, bool]:
    """Revoke (delete) an API token."""
    if not database.delete_api_token(token_id):
        raise HTTPException(status_code=404, detail="Token not found")
    return {"ok": True}
