"""REST API routes for the Scriber admin dashboard."""

from __future__ import annotations

import hmac
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from pydantic import BaseModel

from scriber import config, database
from scriber.memory import MemoryManager
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
    """Add has_transcript / has_summary booleans to a meeting row."""
    row["has_transcript"] = _resolve_data_file(row.get("transcript_path")) is not None
    row["has_summary"] = _resolve_data_file(row.get("summary_path")) is not None
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
    for column in ("transcript_path", "summary_path"):
        path = _resolve_data_file(row.get(column))
        if path is not None:
            try:
                path.unlink()
            except OSError:
                logger.warning("Failed to delete file %s for meeting %s", path, meeting_id)
    database.delete_meeting(meeting_id)
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
    database.update_user(user_id, avatar_path=str(target))
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
    changes: dict[str, str], _user: str = Depends(require_auth)
) -> dict[str, Any]:
    """Persist editable configuration changes; 400 on any non-editable key."""
    manager = _manager()
    try:
        manager.update(changes)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "fields": manager.display_fields()}
