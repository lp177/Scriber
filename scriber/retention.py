"""Retention cleanup for kept meeting audio.

Deletes meeting audio (the playable mix and the regeneration segment track)
once a meeting ended more than ``AUDIO_RETENTION_DAYS`` days ago; ``0`` means
audio is kept forever. The retention value is read live on every pass, so a
dashboard change takes effect within the hour without a restart. Transcripts
and summaries are never touched — only audio expires.
"""

from __future__ import annotations

import asyncio
import logging
import pathlib
from datetime import datetime, timedelta, timezone

from scriber import audio, config, database

log = logging.getLogger(__name__)

#: Seconds between retention passes (also runs once at startup).
CHECK_INTERVAL = 3600


def recover_interrupted(data_dir: pathlib.Path) -> None:
    """Clean up after a previous process that died mid-meeting.

    Runs once at startup, before any new recording can exist. A crash leaves
    two things behind: meeting rows stuck in ``recording`` (which would block
    deletion forever) and unfinalized audio spool temp files (which nothing
    else ever reclaims, ~350 MB per recorded hour). Best-effort: a failure
    here must never stop the app from starting.
    """
    for meeting_id in database.list_meeting_ids_by_status("recording"):
        database.update_meeting(meeting_id, status="error")
        database.append_log(
            meeting_id,
            "Recording was interrupted by a restart; marking the meeting as error.",
        )
        log.warning("Meeting %s was left in 'recording' by a previous run; marked error.",
                    meeting_id)
    audio_dir = data_dir / "audio"
    for pattern in ("*.segments.raw", "*.mix.raw"):
        for stale in audio_dir.glob(pattern):
            try:
                stale.unlink()
                log.warning("Removed stale audio spool file %s from an interrupted run.", stale)
            except OSError:
                log.warning("Failed to remove stale audio spool file %s", stale)


def purge_expired_audio() -> int:
    """Delete the audio of every meeting past retention; return how many."""
    days = config.get().audio_retention_days
    if days <= 0:
        return 0
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(
        timespec="seconds"
    )
    purged = 0
    audio_dir = config.get().data_dir / "audio"
    for row in database.list_expired_audio(cutoff):
        meeting_id = row["id"]
        audio.delete_meeting_audio(audio_dir, meeting_id)
        database.update_meeting(meeting_id, audio_path=None)
        database.append_log(
            meeting_id, f"Meeting audio deleted after the {days}-day retention period."
        )
        purged += 1
    if purged:
        log.info("Audio retention: deleted the audio of %d meeting(s).", purged)
    return purged


async def audio_retention_loop() -> None:
    """Run retention once at startup, then hourly. Never lets an error escape."""
    while True:
        try:
            await asyncio.to_thread(purge_expired_audio)
        except Exception:
            log.exception("Audio retention pass failed; retrying in an hour.")
        await asyncio.sleep(CHECK_INTERVAL)
