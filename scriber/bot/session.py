"""Meeting session: owns the voice connection, segment transcription and transcript output."""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import logging
import secrets
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord.ext import voice_recv

from scriber import config, database
from scriber.bot.recorder import SegmentingSink

if TYPE_CHECKING:
    from scriber.summary.summarizer import Summarizer
    from scriber.transcription.whisper_engine import WhisperEngine

log = logging.getLogger(__name__)

#: Interval (seconds) between sink flush checks while recording.
FLUSH_INTERVAL = 0.25

#: Extensions an avatar may be stored under (kept in sync with the web layer).
_AVATAR_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp")

#: Pixel size requested for auto-synced Discord avatars.
_AVATAR_SIZE = 128


def _new_meeting_id() -> str:
    """Generate a unique, sortable meeting ID."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{secrets.token_hex(2)}"


def _format_hms(seconds: float) -> str:
    """Format a duration in seconds as HH:MM:SS."""
    total = max(0, int(seconds))
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


class MeetingSession:
    """One recording session in a guild: capture, transcribe and persist a meeting."""

    def __init__(
        self,
        *,
        guild: discord.Guild,
        text_channel: Any,
        voice_channel: discord.abc.Connectable,
        started_by: discord.Member,
        whisper: "WhisperEngine",
        summarizer: "Summarizer",
        language: str | None = None,
    ) -> None:
        self.meeting_id: str = _new_meeting_id()
        self.guild = guild
        self.text_channel = text_channel
        self.voice_channel = voice_channel
        self.started_by = started_by
        self.whisper = whisper
        self.summarizer = summarizer
        # Per-meeting transcription language override (a code or ``"auto"``);
        # ``None`` means fall back to the configured WHISPER_LANGUAGE.
        self.language = language
        self.voice_client: voice_recv.VoiceRecvClient | None = None
        self.started_at: datetime = datetime.now(timezone.utc)
        self.entries: list[tuple[float, str, str]] = []
        self.participants: set[str] = set()
        #: Stable identity map: Discord user id -> latest seen display name.
        self.participant_names: dict[str, str] = {}
        self.transcript_path: Path | None = None
        self._sink: SegmentingSink | None = None
        self._flush_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: set[concurrent.futures.Future[None]] = set()
        self._start_monotonic: float = time.monotonic()
        #: Monotonic time of the last transcribed segment; drives inactivity
        #: auto-stop. Initialized to the session start so a silent meeting times
        #: out from when recording began.
        self._last_activity_monotonic: float = time.monotonic()
        self._closed: bool = False

    @property
    def seconds_since_activity(self) -> float:
        """Seconds since the last transcript entry (or session start if none yet)."""
        return time.monotonic() - self._last_activity_monotonic

    async def start(self) -> None:
        """Connect to the voice channel, start capturing audio and create the DB row."""
        self._loop = asyncio.get_running_loop()
        self.voice_client = await self.voice_channel.connect(cls=voice_recv.VoiceRecvClient)
        self.started_at = datetime.now(timezone.utc)
        self._start_monotonic = time.monotonic()
        self._last_activity_monotonic = self._start_monotonic
        self._sink = SegmentingSink(self.on_segment)
        self.voice_client.listen(self._sink)
        self._flush_task = asyncio.create_task(self._flush_loop())
        database.create_meeting(
            {
                "id": self.meeting_id,
                "guild_id": str(self.guild.id),
                "guild_name": self.guild.name,
                "channel_id": str(getattr(self.text_channel, "id", "")),
                "channel_name": str(getattr(self.text_channel, "name", "")),
                "voice_channel_id": str(getattr(self.voice_channel, "id", "")),
                "voice_channel_name": str(getattr(self.voice_channel, "name", "")),
                "started_by_id": str(self.started_by.id),
                "started_by_name": self.started_by.display_name,
                "started_at": self.started_at.isoformat(),
                "status": "recording",
            }
        )
        database.append_log(
            self.meeting_id,
            f"Recording started in voice channel '{getattr(self.voice_channel, 'name', '?')}' "
            f"by {self.started_by.display_name}.",
        )
        log.info("Meeting %s: recording started in guild %s.", self.meeting_id, self.guild.id)

    def on_segment(self, user_id: str, display_name: str, ts: float, pcm: bytes) -> None:
        """Schedule transcription of a finished speech segment (thread-safe)."""
        if self._closed or self._loop is None:
            return
        # Record stable identity keyed by Discord user id (name may change over time).
        self.participant_names[user_id] = display_name
        future = asyncio.run_coroutine_threadsafe(
            self._transcribe_segment(display_name, ts, pcm), self._loop
        )
        self._pending.add(future)
        future.add_done_callback(self._pending.discard)

    async def _transcribe_segment(self, display_name: str, ts: float, pcm: bytes) -> None:
        """Run Whisper on one segment and record the resulting transcript entry."""
        if self._closed:
            return
        self.participants.add(display_name)
        try:
            text = await self.whisper.transcribe(pcm, language=self.language)
        except Exception as exc:
            log.warning(
                "Meeting %s: transcription failed for a segment from %s: %s",
                self.meeting_id, display_name, exc,
            )
            database.append_log(
                self.meeting_id, f"Transcription failed for a segment from {display_name}: {exc}"
            )
            return
        if self._closed or not text:
            return
        self.entries.append((ts, display_name, text))
        self._last_activity_monotonic = time.monotonic()
        database.update_meeting(self.meeting_id, segment_count=len(self.entries))

    async def _flush_loop(self) -> None:
        """Periodically flush stale speaker buffers while recording."""
        try:
            while True:
                await asyncio.sleep(FLUSH_INTERVAL)
                if self._sink is not None:
                    self._sink.flush_stale()
        except asyncio.CancelledError:
            pass

    async def _teardown_voice(self) -> None:
        """Stop the flush task, stop listening and disconnect from voice."""
        if self._flush_task is not None:
            self._flush_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._flush_task
            self._flush_task = None
        if self.voice_client is not None:
            with contextlib.suppress(Exception):
                self.voice_client.stop_listening()

    async def _disconnect_voice(self) -> None:
        """Disconnect the voice client, ignoring errors."""
        if self.voice_client is not None:
            with contextlib.suppress(Exception):
                await self.voice_client.disconnect(force=True)
            self.voice_client = None

    async def _drain_pending(self) -> None:
        """Wait for all queued transcription tasks to complete."""
        pending = list(self._pending)
        if pending:
            await asyncio.gather(
                *(asyncio.wrap_future(future) for future in pending), return_exceptions=True
            )

    async def _sync_one_avatar(self, uid: str, avatars_dir: Path) -> None:
        """Fetch one participant's Discord avatar into ``avatars_dir`` (best-effort)."""
        try:
            member = self.guild.get_member(int(uid))
        except (TypeError, ValueError):
            return
        if member is None:
            with contextlib.suppress(Exception):
                member = await self.guild.fetch_member(int(uid))
        if member is None:
            return
        # Only the member's own avatar (server-specific first) — skip Discord's
        # generic default so those users keep the nicer initials placeholder.
        asset = member.guild_avatar or member.avatar
        if asset is None:
            return
        state = database.get_user_avatar(uid)
        if state is None or state.get("avatar_source") == "manual":
            return  # user set a custom avatar in the dashboard — don't overwrite it
        ext = ".gif" if asset.is_animated() else ".png"
        target = (avatars_dir / f"{uid}{ext}").resolve()
        if state.get("discord_avatar_key") == asset.key and target.exists():
            return  # unchanged since the last sync
        try:
            sized = (
                asset.with_size(_AVATAR_SIZE)
                if asset.is_animated()
                else asset.replace(size=_AVATAR_SIZE, format="png")
            )
            data = await asyncio.wait_for(sized.read(), timeout=10)
        except Exception as exc:
            log.debug("Meeting %s: avatar fetch failed for user %s: %s", self.meeting_id, uid, exc)
            return
        try:
            avatars_dir.mkdir(parents=True, exist_ok=True)
            # Drop any avatar previously stored under a different extension.
            for other in _AVATAR_EXTS:
                if other != ext:
                    (avatars_dir / f"{uid}{other}").unlink(missing_ok=True)
            target.write_bytes(data)
        except OSError as exc:
            log.debug("Meeting %s: avatar write failed for user %s: %s", self.meeting_id, uid, exc)
            return
        database.set_avatar(uid, str(target), "discord", asset.key)

    async def _sync_participant_avatars(self) -> None:
        """Best-effort: pull each speaker's Discord avatar into the dashboard.

        Runs after the transcript is persisted. Never raises — a Discord/CDN
        problem must not fail the meeting. Manually-uploaded avatars are left
        untouched, and an unchanged avatar is not re-downloaded.
        """
        avatars_dir = (config.get().data_dir / "avatars").resolve()
        await asyncio.gather(
            *(self._sync_one_avatar(uid, avatars_dir) for uid in self.participant_names),
            return_exceptions=True,
        )

    async def stop(self) -> tuple[str, dict]:
        """Finish the recording: drain audio, write the transcript file, update the DB.

        Returns the transcript text and the metadata dict expected by the summarizer.
        The meeting status is set to ``summarizing``; the caller drives summarization.
        """
        await self._teardown_voice()
        if self._sink is not None:
            self._sink.flush_all()
        await self._disconnect_voice()
        await self._drain_pending()
        self._closed = True

        ended_at = datetime.now(timezone.utc)
        duration = max(0.0, (ended_at - self.started_at).total_seconds())
        self.entries.sort(key=lambda entry: entry[0])
        word_count = sum(len(text.split()) for _, _, text in self.entries)
        participants = sorted(self.participants)

        header_lines = [
            f"Scriber meeting transcript — {self.meeting_id}",
            f"Server: {self.guild.name}",
            f"Voice channel: {getattr(self.voice_channel, 'name', '?')}",
            f"Started at (UTC): {self.started_at.isoformat()}",
            f"Duration: {_format_hms(duration)}",
            f"Participants: {', '.join(participants) if participants else 'none'}",
        ]
        body_lines = [
            f"[{_format_hms(ts - self._start_monotonic)}] {name}: {text}"
            for ts, name, text in self.entries
        ]
        transcript_text = "\n".join(header_lines) + "\n\n" + "\n".join(body_lines) + "\n"

        transcripts_dir = config.get().data_dir / "transcripts"
        transcripts_dir.mkdir(parents=True, exist_ok=True)
        path = transcripts_dir / f"{self.meeting_id}.txt"
        path.write_text(transcript_text, encoding="utf-8")
        self.transcript_path = path

        # Persist per-user identities and their participation in this meeting,
        # keyed by the stable Discord user id.
        for user_id, name in self.participant_names.items():
            database.upsert_user(user_id, name)
            database.record_participation(self.meeting_id, user_id, name)

        # Best-effort: refresh each speaker's avatar from Discord for the dashboard.
        try:
            await self._sync_participant_avatars()
        except Exception:  # pragma: no cover - defensive; sync is already guarded
            log.warning("Meeting %s: participant avatar sync failed.", self.meeting_id, exc_info=True)

        participant_count = len(self.participant_names)
        database.update_meeting(
            self.meeting_id,
            status="summarizing",
            ended_at=ended_at.isoformat(),
            duration_seconds=duration,
            transcript_path=str(path),
            segment_count=len(self.entries),
            word_count=word_count,
            participant_count=participant_count,
        )
        database.append_log(
            self.meeting_id,
            f"Recording stopped; transcript written ({len(self.entries)} segments, "
            f"{word_count} words, {participant_count} participants).",
        )
        log.info("Meeting %s: transcript written to %s.", self.meeting_id, path)

        meta = {
            "guild_name": self.guild.name,
            "voice_channel_name": str(getattr(self.voice_channel, "name", "?")),
            "started_at": self.started_at.isoformat(),
            "duration_seconds": duration,
            "participants": participants,
        }
        return transcript_text, meta

    async def cancel(self) -> None:
        """Abort the recording and discard all captured data."""
        self._closed = True
        await self._teardown_voice()
        await self._disconnect_voice()
        for future in list(self._pending):
            future.cancel()
        self._pending.clear()
        self.entries.clear()
        # Remove any partial transcript file (normally none exists before stop()).
        transcript_path = (
            self.transcript_path
            if self.transcript_path is not None
            else config.get().data_dir / "transcripts" / f"{self.meeting_id}.txt"
        )
        with contextlib.suppress(OSError):
            transcript_path.unlink(missing_ok=True)
        self.transcript_path = None

        ended_at = datetime.now(timezone.utc)
        duration = max(0.0, (ended_at - self.started_at).total_seconds())
        database.update_meeting(
            self.meeting_id,
            status="cancelled",
            ended_at=ended_at.isoformat(),
            duration_seconds=duration,
        )
        database.append_log(
            self.meeting_id, "Recording cancelled — all captured data has been discarded."
        )
        log.info("Meeting %s: cancelled and discarded.", self.meeting_id)
