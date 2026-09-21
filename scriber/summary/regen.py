"""Background summary (re)generation jobs.

Re-runs the summarizer for an already recorded meeting from the dashboard. It is
the retry path when every provider failed at ``/scriber stop`` (the meeting is
left in ``error`` with only its transcript), and a way to refresh the minutes
after the transcript was edited or regenerated with another engine.

Jobs mirror :mod:`scriber.transcription.regen`: fire-and-forget asyncio tasks
inside the web process, tracked in a module-level dict (one job per meeting at
a time). The dashboard polls the meeting row, which embeds the job state, so no
extra job API is needed. Job state survives until the next job for the same
meeting replaces it.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from scriber import config, database
from scriber.memory import MemoryManager
from scriber.summary.summarizer import Summarizer, SummaryError

log = logging.getLogger(__name__)

#: Active/last job per meeting id. Shape:
#: {status: running|done|error, source, error, posted, started_at}
_JOBS: dict[str, dict] = {}
#: Strong references to the running tasks (the event loop only keeps weak ones).
_TASKS: set[asyncio.Task[None]] = set()


def get_job(meeting_id: str) -> dict | None:
    """Return the current (or last finished) job for a meeting, if any."""
    job = _JOBS.get(meeting_id)
    return dict(job) if job is not None else None


def discard_job(meeting_id: str) -> None:
    """Forget a meeting's job entry (called when the meeting is deleted)."""
    _JOBS.pop(meeting_id, None)


def _has_speech(transcript_text: str) -> bool:
    """True when the transcript holds more than Scriber's metadata header block."""
    header, _, body = transcript_text.partition("\n\n")
    if header.startswith("Scriber meeting transcript"):
        return bool(body.strip())
    return bool(transcript_text.strip())


def start_job(
    meeting_id: str,
    transcript_path: Path,
    source_label: str,
    *,
    bot: Any | None = None,
    post_to_discord: bool = False,
) -> dict:
    """Validate and launch a summary generation job for a meeting.

    ``transcript_path`` is the (already validated) transcript version to
    summarize and ``source_label`` its display name. With ``post_to_discord``
    the finished summary is also sent to the meeting's text channel through
    ``bot``.

    Raises ValueError with a user-presentable message when the request cannot
    be started (job already running, no provider, bot unavailable for posting).
    """
    current = _JOBS.get(meeting_id)
    if current is not None and current["status"] == "running":
        raise ValueError("A summary generation is already running for this meeting.")
    if not Summarizer().targets():
        raise ValueError(
            "No summary provider is configured — add one in Settings first."
        )
    if post_to_discord and (bot is None or not bot.is_ready()):
        raise ValueError(
            "The Discord bot is not connected, so the summary cannot be posted to "
            "the channel. Try again without that option."
        )

    job = {
        "status": "running",
        "source": source_label,
        "error": None,
        "posted": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _JOBS[meeting_id] = job
    task = asyncio.get_running_loop().create_task(
        _run_job(meeting_id, job, transcript_path, bot if post_to_discord else None)
    )
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return dict(job)


async def _run_job(
    meeting_id: str, job: dict, transcript_path: Path, bot: Any | None
) -> None:
    """Generate the summary, then run the best-effort follow-ups."""
    try:
        follow_up = await _summarize(meeting_id, job, transcript_path)
    except (SummaryError, ValueError, OSError) as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        database.append_log(meeting_id, f"Summary generation failed: {str(exc)[:800]}")
        log.warning("Meeting %s: summary generation failed: %s", meeting_id, exc)
        return
    except Exception as exc:  # keep the web process alive whatever happens
        job["status"] = "error"
        job["error"] = f"Unexpected error: {exc}"
        database.append_log(meeting_id, f"Summary generation failed: {exc}")
        log.exception("Meeting %s: summary generation crashed.", meeting_id)
        return

    if bot is not None:
        job["posted"] = await _post_to_discord(bot, meeting_id, follow_up)
    # The summary is delivered: flip the job before the (slow) memory refresh so
    # the dashboard shows the new minutes right away.
    job["status"] = "done"
    if follow_up["refresh_memory"]:
        await _refresh_memories(meeting_id, follow_up)


async def _summarize(meeting_id: str, job: dict, transcript_path: Path) -> dict:
    """The actual pipeline; returns what the follow-up steps need."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise ValueError("Meeting not found.")
    transcript_text = await asyncio.to_thread(transcript_path.read_text, encoding="utf-8")
    if not _has_speech(transcript_text):
        raise ValueError("The transcript contains no speech, so there is nothing to summarize.")

    cfg = config.get()
    participants = [
        (entry["user_id"], entry["display_name"])
        for entry in database.get_meeting_participants(meeting_id)
    ]
    meta = {
        "guild_name": row.get("guild_name"),
        "voice_channel_name": row.get("voice_channel_name"),
        "started_at": row.get("started_at"),
        "duration_seconds": row.get("duration_seconds"),
        "participants": [name for _, name in participants],
    }
    context = MemoryManager(cfg.data_dir / "memory").context_block(participants)
    summarizer = Summarizer()
    summary = await summarizer.summarize(transcript_text, meta, participant_context=context)

    # The provider call can take minutes; the meeting may have been deleted
    # from the dashboard in the meantime. Re-check before persisting so a
    # deleted meeting cannot resurrect an orphan file.
    current = database.get_meeting(meeting_id)
    if current is None:
        raise ValueError("The meeting was deleted while its summary was being generated.")

    summary_path = cfg.data_dir / "transcripts" / f"{meeting_id}.md"
    await asyncio.to_thread(_write_text, summary_path, summary)
    database.update_meeting(meeting_id, status="completed", summary_path=str(summary_path))
    database.append_log(
        meeting_id,
        f"Summary generated from the dashboard ({job['source']}) by "
        f"{summarizer.display_target()}.",
    )
    return {
        "row": current,
        "summary": summary,
        "summary_path": summary_path,
        "summarizer": summarizer,
        "transcript_text": transcript_text,
        "participants": participants,
        # Memory was already refreshed from this meeting if it ever had a
        # summary; doing it again would duplicate its "Recent meetings" entry.
        "refresh_memory": not current.get("summary_path"),
    }


async def _post_to_discord(bot: Any, meeting_id: str, result: dict) -> bool:
    """Send the summary to the meeting's text channel. Best-effort, never raises."""
    row, summary = result["row"], result["summary"]
    try:
        # Imported lazily: the summary package must stay importable without the
        # bot stack, and a failing import must not leave the job hanging.
        import discord

        from scriber.bot.commands import ATTACH_THRESHOLD, split_message

        channel_id = int(row.get("channel_id") or 0)
        channel = bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)
        voice_name = row.get("voice_channel_name") or "the meeting"
        day = str(row.get("started_at") or "")[:10]
        await channel.send(
            f"📝 **Meeting minutes** for **{voice_name}**"
            f"{f' ({day})' if day else ''} — generated from the dashboard."
        )
        chunks = split_message(summary)
        attach = len(summary) > ATTACH_THRESHOLD
        for index, chunk in enumerate(chunks):
            if attach and index == len(chunks) - 1:
                file = discord.File(
                    result["summary_path"], filename=f"scriber-summary-{meeting_id}.md"
                )
                await channel.send(chunk, file=file)
            else:
                await channel.send(chunk)
    except Exception as exc:
        log.warning("Meeting %s: posting the summary to Discord failed: %s", meeting_id, exc)
        database.append_log(meeting_id, f"Posting the summary to Discord failed: {exc}")
        return False
    database.append_log(meeting_id, "Summary posted to the meeting's Discord channel.")
    return True


async def _refresh_memories(meeting_id: str, result: dict) -> None:
    """Best-effort per-user memory refresh, as after a live ``/scriber stop``."""
    memory = MemoryManager(config.get().data_dir / "memory")
    when = str(result["row"].get("started_at") or "")
    for user_id, name in result["participants"]:
        try:
            await memory.update_from_meeting(
                result["summarizer"],
                user_id,
                name,
                result["transcript_text"],
                result["summary"],
                when,
            )
        except Exception as exc:
            log.warning("Meeting %s: memory update failed for %s: %s", meeting_id, name, exc)
            database.append_log(meeting_id, f"Memory update failed for {name}: {exc}")
        else:
            database.append_log(meeting_id, f"Updated memory for {name}.")


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
