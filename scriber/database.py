"""SQLite persistence layer for Scriber meetings.

Uses the stdlib ``sqlite3`` module with a single module-level connection
(``check_same_thread=False``) guarded by a ``threading.Lock``. All functions
are synchronous and fast, so they may be called directly from async code.
"""

from __future__ import annotations

import pathlib
import sqlite3
import threading
from datetime import datetime, timedelta, timezone

_conn: sqlite3.Connection | None = None
_lock = threading.Lock()

_COLUMNS: tuple[str, ...] = (
    "id",
    "guild_id",
    "guild_name",
    "channel_id",
    "channel_name",
    "voice_channel_id",
    "voice_channel_name",
    "started_by_id",
    "started_by_name",
    "started_at",
    "ended_at",
    "duration_seconds",
    "status",
    "transcript_path",
    "summary_path",
    "audio_path",
    "log",
    "segment_count",
    "word_count",
    "participant_count",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    id TEXT PRIMARY KEY,
    guild_id TEXT,
    guild_name TEXT,
    channel_id TEXT,
    channel_name TEXT,
    voice_channel_id TEXT,
    voice_channel_name TEXT,
    started_by_id TEXT,
    started_by_name TEXT,
    started_at TEXT,
    ended_at TEXT,
    duration_seconds REAL,
    status TEXT,
    transcript_path TEXT,
    summary_path TEXT,
    audio_path TEXT,
    log TEXT DEFAULT '',
    segment_count INTEGER DEFAULT 0,
    word_count INTEGER DEFAULT 0,
    participant_count INTEGER DEFAULT 0
)
"""

_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    avatar_path TEXT,
    avatar_source TEXT,
    discord_avatar_key TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_PARTICIPANTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_participants (
    meeting_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    display_name TEXT NOT NULL,
    PRIMARY KEY (meeting_id, user_id)
)
"""

# Alternate transcript versions regenerated from the saved meeting audio with a
# different engine/model. The ORIGINAL live transcript stays on
# ``meetings.transcript_path`` — rows here are additional versions.
_TRANSCRIPTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS meeting_transcripts (
    id TEXT PRIMARY KEY,
    meeting_id TEXT NOT NULL,
    engine TEXT NOT NULL,
    label TEXT NOT NULL,
    path TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_API_TOKENS_SCHEMA = """
CREATE TABLE IF NOT EXISTS api_tokens (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    token_prefix TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scope TEXT NOT NULL DEFAULT 'read',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    last_used_ip TEXT
)
"""

# Columns of the ``users`` table that ``update_user`` is permitted to modify.
_USER_UPDATABLE: tuple[str, ...] = ("display_name", "description", "avatar_path")


def _utcnow() -> str:
    """Return the current UTC time as an ISO8601 string (second precision)."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _require_conn() -> sqlite3.Connection:
    """Return the open connection or raise if init() was not called."""
    if _conn is None:
        raise RuntimeError("Database is not initialized; call database.init() first.")
    return _conn


def init(db_path: pathlib.Path) -> None:
    """Open (or create) the SQLite database and ensure the schema exists."""
    global _conn
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        if _conn is not None:
            _conn.close()
        _conn = sqlite3.connect(str(db_path), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute(_SCHEMA)
        _conn.execute(_USERS_SCHEMA)
        _conn.execute(_PARTICIPANTS_SCHEMA)
        _conn.execute(_TRANSCRIPTS_SCHEMA)
        _conn.execute(_API_TOKENS_SCHEMA)
        _migrate(_conn)
        _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Add columns introduced after the initial schema to a pre-existing DB."""
    user_cols = {row["name"] for row in conn.execute("PRAGMA table_info(users)")}
    if "avatar_source" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN avatar_source TEXT")
    if "discord_avatar_key" not in user_cols:
        conn.execute("ALTER TABLE users ADD COLUMN discord_avatar_key TEXT")
    meeting_cols = {row["name"] for row in conn.execute("PRAGMA table_info(meetings)")}
    if "audio_path" not in meeting_cols:
        conn.execute("ALTER TABLE meetings ADD COLUMN audio_path TEXT")


def close() -> None:
    """Close the database connection if open."""
    global _conn
    with _lock:
        if _conn is not None:
            _conn.close()
            _conn = None


def create_meeting(meeting: dict) -> None:
    """Insert a new meeting row; the caller provides the ``id``."""
    if not meeting.get("id"):
        raise ValueError("Meeting dict must contain a non-empty 'id'.")
    unknown = set(meeting) - set(_COLUMNS)
    if unknown:
        raise ValueError("Unknown meeting column(s): " + ", ".join(sorted(unknown)))
    columns = [c for c in _COLUMNS if c in meeting]
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO meetings ({', '.join(columns)}) VALUES ({placeholders})"
    values = [meeting[c] for c in columns]
    with _lock:
        conn = _require_conn()
        conn.execute(sql, values)
        conn.commit()


def update_meeting(meeting_id: str, **fields) -> None:
    """Update the given columns of a meeting row."""
    if not fields:
        return
    unknown = set(fields) - (set(_COLUMNS) - {"id"})
    if unknown:
        raise ValueError("Unknown meeting column(s): " + ", ".join(sorted(unknown)))
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [meeting_id]
    with _lock:
        conn = _require_conn()
        conn.execute(f"UPDATE meetings SET {assignments} WHERE id = ?", values)
        conn.commit()


def append_log(meeting_id: str, message: str) -> None:
    """Append a timestamped line to the meeting's log column."""
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    line = f"[{timestamp}] {message}\n"
    with _lock:
        conn = _require_conn()
        conn.execute(
            "UPDATE meetings SET log = COALESCE(log, '') || ? WHERE id = ?",
            (line, meeting_id),
        )
        conn.commit()


def get_meeting(meeting_id: str) -> dict | None:
    """Return a meeting row as a dict, or None if not found."""
    with _lock:
        conn = _require_conn()
        row = conn.execute(
            "SELECT * FROM meetings WHERE id = ?", (meeting_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def list_meetings(limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
    """Return (total count, meeting rows newest-first) with pagination."""
    with _lock:
        conn = _require_conn()
        total = conn.execute("SELECT COUNT(*) FROM meetings").fetchone()[0]
        rows = conn.execute(
            "SELECT * FROM meetings ORDER BY started_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
    return total, [dict(row) for row in rows]


def delete_meeting(meeting_id: str) -> None:
    """Delete a meeting row and its associated participant/transcript rows."""
    with _lock:
        conn = _require_conn()
        conn.execute(
            "DELETE FROM meeting_participants WHERE meeting_id = ?", (meeting_id,)
        )
        conn.execute(
            "DELETE FROM meeting_transcripts WHERE meeting_id = ?", (meeting_id,)
        )
        conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        conn.commit()


def list_meeting_ids_by_status(status: str) -> list[str]:
    """Return the ids of every meeting currently in the given status."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            "SELECT id FROM meetings WHERE status = ?", (status,)
        ).fetchall()
    return [row["id"] for row in rows]


def list_expired_audio(cutoff_iso: str) -> list[dict]:
    """Return meetings whose kept audio ended before ``cutoff_iso`` (retention)."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            """
            SELECT id, audio_path, ended_at
            FROM meetings
            WHERE audio_path IS NOT NULL AND ended_at IS NOT NULL AND ended_at < ?
            """,
            (cutoff_iso,),
        ).fetchall()
    return [dict(row) for row in rows]


# ---------------------------------------------------------------------------
# Alternate transcript versions (regenerated from the saved meeting audio)
# ---------------------------------------------------------------------------


def create_transcript(
    transcript_id: str, meeting_id: str, engine: str, label: str, path: str
) -> dict:
    """Insert an alternate transcript version row and return it."""
    now = _utcnow()
    with _lock:
        conn = _require_conn()
        conn.execute(
            "INSERT INTO meeting_transcripts (id, meeting_id, engine, label, path, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (transcript_id, meeting_id, engine, label, path, now),
        )
        conn.commit()
    return {
        "id": transcript_id,
        "meeting_id": meeting_id,
        "engine": engine,
        "label": label,
        "path": path,
        "created_at": now,
    }


def list_transcripts(meeting_id: str) -> list[dict]:
    """Return a meeting's alternate transcript versions, oldest first."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            "SELECT * FROM meeting_transcripts WHERE meeting_id = ? "
            "ORDER BY created_at ASC, id ASC",
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_transcript(meeting_id: str, transcript_id: str) -> dict | None:
    """Return one alternate transcript version row, or None."""
    with _lock:
        conn = _require_conn()
        row = conn.execute(
            "SELECT * FROM meeting_transcripts WHERE id = ? AND meeting_id = ?",
            (transcript_id, meeting_id),
        ).fetchone()
    return dict(row) if row is not None else None


def delete_transcript(meeting_id: str, transcript_id: str) -> bool:
    """Delete an alternate transcript version row; True if a row was removed."""
    with _lock:
        conn = _require_conn()
        cur = conn.execute(
            "DELETE FROM meeting_transcripts WHERE id = ? AND meeting_id = ?",
            (transcript_id, meeting_id),
        )
        conn.commit()
        return cur.rowcount > 0


def get_stats() -> dict:
    """Return aggregate statistics for the dashboard."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=29)).strftime("%Y-%m-%d")
    with _lock:
        conn = _require_conn()
        totals = conn.execute(
            """
            SELECT
                COUNT(*) AS total_meetings,
                SUM(CASE WHEN status = 'completed' THEN 1 ELSE 0 END) AS completed,
                SUM(CASE WHEN status = 'cancelled' THEN 1 ELSE 0 END) AS cancelled,
                SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
                COALESCE(SUM(duration_seconds), 0) AS total_duration_seconds,
                COALESCE(SUM(word_count), 0) AS total_words
            FROM meetings
            """
        ).fetchone()
        by_day = conn.execute(
            """
            SELECT substr(started_at, 1, 10) AS date, COUNT(*) AS count
            FROM meetings
            WHERE substr(started_at, 1, 10) >= ?
            GROUP BY date
            ORDER BY date ASC
            """,
            (cutoff,),
        ).fetchall()
        top_guilds = conn.execute(
            """
            SELECT guild_name, COUNT(*) AS count
            FROM meetings
            WHERE guild_name IS NOT NULL AND guild_name != ''
            GROUP BY guild_name
            ORDER BY count DESC
            LIMIT 5
            """
        ).fetchall()
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    return {
        "total_meetings": totals["total_meetings"] or 0,
        "completed": totals["completed"] or 0,
        "cancelled": totals["cancelled"] or 0,
        "errors": totals["errors"] or 0,
        "total_duration_seconds": totals["total_duration_seconds"] or 0,
        "total_words": totals["total_words"] or 0,
        "total_users": total_users or 0,
        "meetings_by_day": [
            {"date": row["date"], "count": row["count"]} for row in by_day
        ],
        "top_guilds": [
            {"guild_name": row["guild_name"], "count": row["count"]}
            for row in top_guilds
        ],
    }


def upsert_user(user_id: str, display_name: str) -> None:
    """Insert a new user, or refresh an existing user's display name.

    On first insert ``created_at``/``updated_at`` are set to now and
    ``description`` defaults to empty. If the user already exists only
    ``display_name`` and ``updated_at`` change; ``description`` and
    ``avatar_path`` are preserved.
    """
    now = _utcnow()
    with _lock:
        conn = _require_conn()
        conn.execute(
            """
            INSERT INTO users (id, display_name, description, avatar_path, created_at, updated_at)
            VALUES (?, ?, '', NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                display_name = excluded.display_name,
                updated_at = excluded.updated_at
            """,
            (user_id, display_name, now, now),
        )
        conn.commit()


def update_user(user_id: str, **fields) -> None:
    """Update a user's columns (``display_name``/``description``/``avatar_path``).

    Unknown keys are ignored defensively; ``updated_at`` is always refreshed.
    """
    updates = {name: value for name, value in fields.items() if name in _USER_UPDATABLE}
    if not updates:
        return
    assignments = ", ".join(f"{name} = ?" for name in updates)
    values = list(updates.values()) + [_utcnow(), user_id]
    with _lock:
        conn = _require_conn()
        conn.execute(
            f"UPDATE users SET {assignments}, updated_at = ? WHERE id = ?",
            values,
        )
        conn.commit()


def set_avatar(
    user_id: str, avatar_path: str, source: str, discord_avatar_key: str | None = None
) -> None:
    """Record a user's avatar and where it came from.

    ``source`` is ``'discord'`` (auto-synced from the member's Discord avatar) or
    ``'manual'`` (uploaded from the dashboard). A manual upload passes
    ``discord_avatar_key=None`` to clear the stored key so a later meeting's
    auto-sync will not overwrite the deliberate choice.
    """
    with _lock:
        conn = _require_conn()
        conn.execute(
            "UPDATE users SET avatar_path = ?, avatar_source = ?, "
            "discord_avatar_key = ?, updated_at = ? WHERE id = ?",
            (avatar_path, source, discord_avatar_key, _utcnow(), user_id),
        )
        conn.commit()


def get_user_avatar(user_id: str) -> dict | None:
    """Return a user's avatar bookkeeping (path/source/discord key), or None."""
    with _lock:
        conn = _require_conn()
        row = conn.execute(
            "SELECT avatar_path, avatar_source, discord_avatar_key FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def get_user(user_id: str) -> dict | None:
    """Return a user row as a dict, or None if not found.

    Adds two computed fields: ``session_count`` (distinct meetings joined) and
    ``last_session_at`` (latest joined meeting's ``started_at``, or None).
    """
    with _lock:
        conn = _require_conn()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if row is None:
            return None
        computed = conn.execute(
            """
            SELECT
                COUNT(DISTINCT mp.meeting_id) AS session_count,
                MAX(m.started_at) AS last_session_at
            FROM meeting_participants mp
            LEFT JOIN meetings m ON m.id = mp.meeting_id
            WHERE mp.user_id = ?
            """,
            (user_id,),
        ).fetchone()
    result = dict(row)
    result["session_count"] = computed["session_count"] or 0
    result["last_session_at"] = computed["last_session_at"]
    return result


def list_users(limit: int = 50, offset: int = 0) -> tuple[int, list[dict]]:
    """Return (total user count, user rows) with pagination.

    Each row carries ``id``, ``display_name``, ``description``, ``avatar_path``,
    ``session_count`` and ``last_session_at``. Ordered by ``last_session_at``
    descending with NULLs last, then ``display_name``.
    """
    with _lock:
        conn = _require_conn()
        total = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            """
            SELECT
                u.id AS id,
                u.display_name AS display_name,
                u.description AS description,
                u.avatar_path AS avatar_path,
                COUNT(DISTINCT mp.meeting_id) AS session_count,
                MAX(m.started_at) AS last_session_at
            FROM users u
            LEFT JOIN meeting_participants mp ON mp.user_id = u.id
            LEFT JOIN meetings m ON m.id = mp.meeting_id
            GROUP BY u.id
            ORDER BY last_session_at IS NULL, last_session_at DESC, display_name ASC
            LIMIT ? OFFSET ?
            """,
            (limit, offset),
        ).fetchall()
    return total, [dict(row) for row in rows]


def record_participation(meeting_id: str, user_id: str, display_name: str) -> None:
    """Record that a user took part in a meeting (latest display name wins)."""
    with _lock:
        conn = _require_conn()
        conn.execute(
            """
            INSERT OR REPLACE INTO meeting_participants
                (meeting_id, user_id, display_name)
            VALUES (?, ?, ?)
            """,
            (meeting_id, user_id, display_name),
        )
        conn.commit()


def get_user_meetings(user_id: str) -> list[dict]:
    """Return the meetings a user joined, newest first (``started_at`` DESC)."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            """
            SELECT
                m.id AS id,
                m.started_at AS started_at,
                m.ended_at AS ended_at,
                m.guild_name AS guild_name,
                m.voice_channel_name AS voice_channel_name,
                m.status AS status,
                m.summary_path AS summary_path,
                m.transcript_path AS transcript_path
            FROM meeting_participants mp
            JOIN meetings m ON m.id = mp.meeting_id
            WHERE mp.user_id = ?
            ORDER BY m.started_at DESC, m.id DESC
            """,
            (user_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def get_meeting_participants(meeting_id: str) -> list[dict]:
    """Return a meeting's participants as dicts, ordered by display name."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            """
            SELECT user_id, display_name
            FROM meeting_participants
            WHERE meeting_id = ?
            ORDER BY display_name ASC
            """,
            (meeting_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def delete_user(user_id: str) -> None:
    """Delete a user row and all of that user's participation rows."""
    with _lock:
        conn = _require_conn()
        conn.execute(
            "DELETE FROM meeting_participants WHERE user_id = ?", (user_id,)
        )
        conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        conn.commit()


# ---------------------------------------------------------------------------
# API tokens (for the read/write REST API, distinct from dashboard sessions)
# ---------------------------------------------------------------------------

# Columns returned to the dashboard — never includes ``token_hash``.
_API_TOKEN_PUBLIC = (
    "id, name, token_prefix, scope, created_at, last_used_at, last_used_ip"
)


def create_api_token(
    token_id: str, name: str, token_prefix: str, token_hash: str, scope: str
) -> dict:
    """Insert a new API token row and return its public representation."""
    now = _utcnow()
    with _lock:
        conn = _require_conn()
        conn.execute(
            "INSERT INTO api_tokens "
            "(id, name, token_prefix, token_hash, scope, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (token_id, name, token_prefix, token_hash, scope, now),
        )
        conn.commit()
        row = conn.execute(
            f"SELECT {_API_TOKEN_PUBLIC} FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()
    return dict(row)


def list_api_tokens() -> list[dict]:
    """Return all API tokens (without the hash), newest first."""
    with _lock:
        conn = _require_conn()
        rows = conn.execute(
            f"SELECT {_API_TOKEN_PUBLIC} FROM api_tokens ORDER BY created_at DESC, id DESC"
        ).fetchall()
    return [dict(row) for row in rows]


def get_api_token_by_hash(token_hash: str) -> dict | None:
    """Return the token row matching a SHA-256 hash, or None (used for auth)."""
    with _lock:
        conn = _require_conn()
        row = conn.execute(
            f"SELECT {_API_TOKEN_PUBLIC} FROM api_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
    return dict(row) if row is not None else None


def touch_api_token(token_id: str, ip: str | None) -> None:
    """Record the last-used timestamp (and client IP) for a token."""
    with _lock:
        conn = _require_conn()
        conn.execute(
            "UPDATE api_tokens SET last_used_at = ?, last_used_ip = ? WHERE id = ?",
            (_utcnow(), ip, token_id),
        )
        conn.commit()


def update_api_token(
    token_id: str, *, name: str | None = None, scope: str | None = None
) -> dict | None:
    """Rename a token and/or change its scope; return the updated public row."""
    assignments: list[str] = []
    values: list[str] = []
    if name is not None:
        assignments.append("name = ?")
        values.append(name)
    if scope is not None:
        assignments.append("scope = ?")
        values.append(scope)
    with _lock:
        conn = _require_conn()
        if assignments:
            conn.execute(
                f"UPDATE api_tokens SET {', '.join(assignments)} WHERE id = ?",
                (*values, token_id),
            )
            conn.commit()
        row = conn.execute(
            f"SELECT {_API_TOKEN_PUBLIC} FROM api_tokens WHERE id = ?", (token_id,)
        ).fetchone()
    return dict(row) if row is not None else None


def delete_api_token(token_id: str) -> bool:
    """Delete a token by id; return True if a row was removed."""
    with _lock:
        conn = _require_conn()
        cur = conn.execute("DELETE FROM api_tokens WHERE id = ?", (token_id,))
        conn.commit()
        return cur.rowcount > 0
