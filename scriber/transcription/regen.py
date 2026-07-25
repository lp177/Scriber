"""Background transcript regeneration jobs.

A regeneration job re-transcribes a meeting's archived speech segments with a
chosen engine/model (see :mod:`scriber.transcription.providers`) and stores the
result as an *alternate transcript version* (``meeting_transcripts`` table)
next to the original — nothing ever overwrites the original live transcript.

Jobs run as fire-and-forget asyncio tasks inside the web process, tracked in a
module-level dict (one job per meeting at a time). The dashboard polls the
meeting's transcript list, which embeds the job state, so no extra job API is
needed. Job state survives until the next job for the same meeting replaces it.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from pathlib import Path

from scriber import audio, config, database
from scriber.transcription import providers
from scriber.transcription.providers import TranscriptionProviderError

log = logging.getLogger(__name__)

#: Active/last job per meeting id. Shape:
#: {status: running|done|error, engine, model, label, done, total,
#:  error, transcript_id, started_at}
_JOBS: dict[str, dict] = {}


def get_job(meeting_id: str) -> dict | None:
    """Return the current (or last finished) job for a meeting, if any."""
    job = _JOBS.get(meeting_id)
    return dict(job) if job is not None else None


def discard_job(meeting_id: str) -> None:
    """Forget a meeting's job entry (called when the meeting is deleted)."""
    _JOBS.pop(meeting_id, None)


def _format_hms(seconds: float) -> str:
    """Format a duration in seconds as HH:MM:SS (same as the live transcript)."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def start_job(meeting_id: str, engine: str, model: str, language: str) -> dict:
    """Validate and launch a regeneration job for a meeting.

    Raises ValueError with a user-presentable message when the request cannot
    be started (job already running, engine unknown/unconfigured, no audio).
    """
    current = _JOBS.get(meeting_id)
    if current is not None and current["status"] == "running":
        raise ValueError("A transcript regeneration is already running for this meeting.")

    catalog = {entry["id"]: entry for entry in providers.engine_catalog()}
    entry = catalog.get(engine)
    if entry is None:
        raise ValueError(f"Unknown transcription engine {engine!r}.")
    if not entry["ready"]:
        raise ValueError(
            f"The {entry['label']} engine is not configured yet — set "
            f"{entry['requires']} in Settings first."
        )

    audio_dir = config.get().data_dir / "audio"
    if not audio.segments_available(audio_dir, meeting_id):
        raise ValueError(
            "No archived audio segments are available for this meeting, so its "
            "transcript cannot be regenerated."
        )

    model = (model or "").strip() or entry["default_model"]
    language = (language or "").strip() or "auto"
    job = {
        "status": "running",
        "engine": engine,
        "model": model,
        "label": providers.engine_label(engine, model),
        "done": 0,
        "total": 0,
        "error": None,
        "transcript_id": None,
        "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    _JOBS[meeting_id] = job
    asyncio.get_running_loop().create_task(_run_job(meeting_id, job, language))
    return dict(job)


async def _run_job(meeting_id: str, job: dict, language: str) -> None:
    """Decode the archive, run the engine, and store the new transcript version."""
    try:
        transcript_id = await _regenerate(meeting_id, job, language)
    except (TranscriptionProviderError, ValueError, FileNotFoundError, OSError) as exc:
        job["status"] = "error"
        job["error"] = str(exc)
        database.append_log(
            meeting_id, f"Transcript regeneration with {job['label']} failed: {exc}"
        )
        log.warning("Meeting %s: transcript regeneration failed: %s", meeting_id, exc)
    except Exception as exc:  # keep the web process alive whatever happens
        job["status"] = "error"
        job["error"] = f"Unexpected error: {exc}"
        database.append_log(
            meeting_id, f"Transcript regeneration with {job['label']} failed: {exc}"
        )
        log.exception("Meeting %s: transcript regeneration crashed.", meeting_id)
    else:
        job["status"] = "done"
        job["transcript_id"] = transcript_id
        database.append_log(
            meeting_id, f"Alternate transcript generated with {job['label']}."
        )


async def _regenerate(meeting_id: str, job: dict, language: str) -> str:
    """The actual pipeline; returns the new transcript version id."""
    row = database.get_meeting(meeting_id)
    if row is None:
        raise ValueError("Meeting not found.")

    cfg = config.get()
    audio_dir = cfg.data_dir / "audio"
    index = await asyncio.to_thread(audio.load_segment_index, audio_dir, meeting_id)
    track = await asyncio.to_thread(audio.decode_segments_track, audio_dir, meeting_id)
    if not index:
        raise ValueError("The audio archive of this meeting contains no segments.")

    slices = [audio.slice_segment(track, entry) for entry in index]
    job["total"] = len(slices)

    texts = await providers.transcribe_segments(
        job["engine"],
        job["model"],
        language,
        slices,
        progress=lambda: job.__setitem__("done", job["done"] + 1),
    )

    # The transcription phase can take minutes; the meeting may have been
    # deleted from the dashboard in the meantime. Re-check before persisting so
    # a deleted meeting cannot resurrect an orphan file/row.
    if database.get_meeting(meeting_id) is None:
        raise ValueError("The meeting was deleted while its transcript was being regenerated.")

    participants = sorted({entry["name"] for entry in index})
    header_lines = [
        f"Scriber meeting transcript — {meeting_id}",
        f"Engine: {job['label']}",
        f"Server: {row.get('guild_name') or '?'}",
        f"Voice channel: {row.get('voice_channel_name') or '?'}",
        f"Started at (UTC): {row.get('started_at') or '?'}",
        f"Duration: {_format_hms(float(row.get('duration_seconds') or 0))}",
        f"Participants: {', '.join(participants) if participants else 'none'}",
    ]
    body_lines = [
        f"[{_format_hms(entry['start'])}] {entry['name']}: {text}"
        for entry, text in zip(index, texts)
        if text
    ]
    transcript_text = "\n".join(header_lines) + "\n\n" + "\n".join(body_lines) + "\n"

    transcript_id = secrets.token_hex(4)
    transcripts_dir = cfg.data_dir / "transcripts"
    path: Path = transcripts_dir / f"{meeting_id}.{transcript_id}.txt"
    await asyncio.to_thread(_write_text, path, transcript_text)
    database.create_transcript(
        transcript_id, meeting_id, job["engine"], job["label"], str(path)
    )
    return transcript_id


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
