"""The /scriber slash command group: start, stop and cancel meeting recordings."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

import discord
from discord import app_commands

from scriber import config, database
from scriber.bot.session import MeetingSession
from scriber.summary.summarizer import SummaryError
from scriber.transcription.whisper_engine import normalize_language

if TYPE_CHECKING:
    from scriber.bot.client import ScribBot

log = logging.getLogger(__name__)

#: Maximum characters per posted Discord message chunk.
MESSAGE_LIMIT = 1990
#: Summaries longer than this also get attached as a Markdown file.
ATTACH_THRESHOLD = 3500
#: Auto-stop a recording after this many seconds without any transcribed speech.
INACTIVITY_LIMIT_SECONDS = 120
#: How often the inactivity monitor checks a running session.
MONITOR_INTERVAL_SECONDS = 15


def _split_long_block(block: str, limit: int) -> list[str]:
    """Split an oversized paragraph on line boundaries, hard-splitting huge lines."""
    pieces: list[str] = []
    current = ""
    for line in block.split("\n"):
        while len(line) > limit:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(line[:limit])
            line = line[limit:]
        candidate = line if not current else f"{current}\n{line}"
        if len(candidate) <= limit:
            current = candidate
        else:
            pieces.append(current)
            current = line
    if current:
        pieces.append(current)
    return pieces


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    """Split text into chunks of at most ``limit`` chars, preferring paragraph boundaries."""
    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(paragraph) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_long_block(paragraph, limit))
            continue
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= limit:
            current = candidate
        else:
            chunks.append(current)
            current = paragraph
    if current:
        chunks.append(current)
    return [chunk for chunk in chunks if chunk.strip()]


class ScribCommands(app_commands.Group):
    """Slash command group ``/scriber`` with the start/stop/cancel subcommands."""

    def __init__(self, bot: "ScribBot") -> None:
        super().__init__(
            name="scriber",
            description="Record, transcribe and summarize voice-channel meetings.",
            guild_only=True,
        )
        self.bot = bot
        #: Per-guild inactivity-monitor tasks, keyed by guild id.
        self._monitors: dict[int, asyncio.Task[None]] = {}

    # -- helpers ----------------------------------------------------------

    @staticmethod
    async def _send_channel(
        channel: Any, content: str, file_path: Path | None = None, filename: str | None = None
    ) -> None:
        """Send a message (optionally with a file) directly to a channel."""
        if channel is None:
            log.error("Cannot deliver message: the meeting text channel is unavailable.")
            return
        if file_path is not None:
            await channel.send(content, file=discord.File(file_path, filename=filename))
        else:
            await channel.send(content)

    async def _send(
        self,
        interaction: discord.Interaction | None,
        channel: Any,
        content: str,
        file_path: Path | None = None,
        filename: str | None = None,
    ) -> None:
        """Send via the deferred interaction followup, falling back to the channel.

        When *interaction* is None (an automatic stop, not a slash command) the
        message goes straight to the channel. The followup webhook token also
        expires after 15 minutes, so very long transcription runs likewise fall
        back to the channel.
        """
        if interaction is not None:
            try:
                if file_path is not None:
                    await interaction.followup.send(
                        content, file=discord.File(file_path, filename=filename)
                    )
                else:
                    await interaction.followup.send(content)
                return
            except discord.HTTPException:
                pass
        await self._send_channel(channel, content, file_path, filename)

    async def _handle_summary_failure(
        self, interaction: discord.Interaction | None, session: MeetingSession, reason: str
    ) -> None:
        """Mark the meeting as errored and post the raw transcript so nothing is lost."""
        reason = reason[:800]
        database.update_meeting(session.meeting_id, status="error")
        database.append_log(session.meeting_id, f"Summarization failed: {reason}")
        transcript_path = session.transcript_path
        message = (
            "⚠️ I'm sorry — the meeting was recorded and transcribed, but generating "
            f"the summary failed: {reason}"
        )
        if transcript_path is not None and transcript_path.exists():
            message += "\nThe raw transcript is attached so nothing is lost."
            await self._send(
                interaction,
                session.text_channel,
                message,
                file_path=transcript_path,
                filename=f"scriber-transcript-{session.meeting_id}.txt",
            )
        else:
            message += "\nUnfortunately the transcript file could not be found on disk either."
            await self._send(interaction, session.text_channel, message)

    # -- subcommands ------------------------------------------------------

    @app_commands.command(
        name="start", description="Join your voice channel and start recording the meeting."
    )
    @app_commands.describe(
        lang="Transcription language for this meeting: 'auto' or a code such as en, fr, de "
        "(defaults to the configured language)."
    )
    async def start(self, interaction: discord.Interaction, lang: str | None = None) -> None:
        """Start a recording session in the caller's voice channel."""
        guild = interaction.guild
        member = interaction.user
        if guild is None or not isinstance(member, discord.Member):
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        voice_state = member.voice
        if voice_state is None or voice_state.channel is None:
            await interaction.response.send_message(
                "You must be in a voice channel of this server to start a recording.",
                ephemeral=True,
            )
            return
        if guild.id in self.bot.active_sessions:
            await interaction.response.send_message(
                "A recording session is already active in this server. "
                "Use `/scriber stop` or `/scriber cancel` first.",
                ephemeral=True,
            )
            return

        # Fail fast with a precise message when Discord won't let the bot into
        # the channel, instead of a ~60s voice-handshake timeout that surfaces
        # as a bare, unhelpful error. permissions_for() resolves the channel's
        # role/member overwrites, so this catches a "Private" channel that
        # denies View Channel / Connect to the bot's role.
        bot_perms = voice_state.channel.permissions_for(guild.me)
        missing = [
            label
            for label, granted in (
                ("View Channel", bot_perms.view_channel),
                ("Connect", bot_perms.connect),
            )
            if not granted
        ]
        if missing:
            await interaction.response.send_message(
                f"I can't join **{voice_state.channel.name}** — I'm missing the "
                f"**{'** and **'.join(missing)}** permission"
                f"{'s' if len(missing) > 1 else ''} on that channel. Add my role "
                "(or me) to that channel's permission settings with those allowed, "
                "then run `/scriber start` again.",
                ephemeral=True,
            )
            return

        language: str | None = None
        if lang is not None:
            try:
                language = normalize_language(lang)
            except ValueError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        await interaction.response.defer()
        session = MeetingSession(
            guild=guild,
            text_channel=interaction.channel,
            voice_channel=voice_state.channel,
            started_by=member,
            whisper=self.bot.whisper,
            summarizer=self.bot.summarizer,
            language=language,
        )
        self.bot.active_sessions[guild.id] = session
        try:
            await session.start()
        except Exception as exc:
            self.bot.active_sessions.pop(guild.id, None)
            log.exception("Failed to start a recording session in guild %s.", guild.id)
            with contextlib.suppress(Exception):
                await session.cancel()
            await self._send(
                interaction,
                interaction.channel,
                f"⚠️ Failed to start the recording: {exc}",
            )
            return

        # Watch for a silent meeting and auto-stop it (the left-alone case is
        # handled event-driven by the bot's on_voice_state_update).
        self._monitors[guild.id] = asyncio.create_task(self._inactivity_monitor(guild.id))

        cfg = config.get()
        voice_name = getattr(voice_state.channel, "name", str(voice_state.channel))
        effective_lang = session.language if session.language is not None else cfg.whisper_language
        lang_label = "auto-detect" if effective_lang in ("", "auto") else effective_lang
        targets = self.bot.summarizer.targets()
        failover_note = (
            " Providers are tried in this order until one succeeds." if len(targets) > 1 else ""
        )
        notice = (
            f"🔴 **Recording notice** — Scriber joined **{voice_name}** and is now recording. "
            f"The conversation is transcribed locally on this server (Whisper, model "
            f"`{cfg.whisper_model}`, language `{lang_label}`). When the meeting ends, the full "
            f"transcript will be sent to an external AI service for summarization: "
            f"**{self.bot.summarizer.display_target()}**.{failover_note} If you do not wish to be "
            f"recorded, please leave the voice channel now. Use `/scriber stop` to finish and get "
            f"the summary, or `/scriber cancel` to discard everything."
        )
        await self._send(interaction, session.text_channel, notice)

    @app_commands.command(
        name="stop", description="Stop recording, transcribe the meeting and post the summary."
    )
    async def stop(self, interaction: discord.Interaction) -> None:
        """Finish the active session: transcribe, summarize and post the result."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        if guild.id not in self.bot.active_sessions:
            await interaction.response.send_message(
                "There is no active recording session in this server.", ephemeral=True
            )
            return
        await interaction.response.defer()
        await self._finalize_and_post(guild.id, interaction=interaction)

    async def stop_session_automatically(self, guild_id: int, reason_note: str) -> None:
        """Automatically finish a session (left alone / inactivity), posting *reason_note*."""
        await self._finalize_and_post(guild_id, reason_note=reason_note)

    def _cancel_monitor(self, guild_id: int) -> None:
        """Cancel a guild's inactivity monitor, unless we are running inside it."""
        monitor = self._monitors.pop(guild_id, None)
        if monitor is not None and monitor is not asyncio.current_task():
            monitor.cancel()

    async def _inactivity_monitor(self, guild_id: int) -> None:
        """Auto-stop a session that has produced no transcribed speech for too long."""
        try:
            while True:
                await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
                session = self.bot.active_sessions.get(guild_id)
                if session is None:
                    return
                if session.seconds_since_activity >= INACTIVITY_LIMIT_SECONDS:
                    minutes = INACTIVITY_LIMIT_SECONDS // 60
                    await self._finalize_and_post(
                        guild_id,
                        reason_note=(
                            f"⏹️ Auto-stopped after {minutes} minute"
                            f"{'s' if minutes != 1 else ''} with no speech transcribed."
                        ),
                    )
                    return
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("Inactivity monitor for guild %s crashed.", guild_id)

    async def _finalize_and_post(
        self,
        guild_id: int,
        *,
        interaction: discord.Interaction | None = None,
        reason_note: str = "",
    ) -> None:
        """Claim the active session, stop it, summarize and post the result.

        Shared by ``/scriber stop`` (with an interaction) and the automatic stop
        paths (no interaction → posts straight to the meeting text channel). The
        session is popped atomically so only the first caller finalizes it, even
        if the command and an auto-stop fire at nearly the same time.
        """
        session = self.bot.active_sessions.pop(guild_id, None)
        if session is None:
            if interaction is not None:
                with contextlib.suppress(discord.HTTPException):
                    await interaction.followup.send(
                        "The recording was just stopped by another action.", ephemeral=True
                    )
            return
        self._cancel_monitor(guild_id)

        try:
            transcript_text, meta = await session.stop()
        except Exception as exc:
            log.exception("Failed to finalize recording for meeting %s.", session.meeting_id)
            database.update_meeting(session.meeting_id, status="error")
            database.append_log(session.meeting_id, f"Finalizing the recording failed: {exc}")
            await self._send(
                interaction,
                session.text_channel,
                f"⚠️ Something went wrong while finalizing the recording: {exc}",
            )
            return

        if reason_note:
            await self._send_channel(session.text_channel, reason_note)

        # Nothing was said: skip the summarizer (and its API call) and say so
        # plainly, instead of posting a summary of an empty transcript.
        if not session.entries:
            database.update_meeting(session.meeting_id, status="completed")
            database.append_log(session.meeting_id, "No speech captured; nothing to summarize.")
            await self._send(
                interaction,
                session.text_channel,
                "ℹ️ Recording stopped — no speech was captured, so there is nothing to summarize.",
            )
            return

        # (user_id, display_name) pairs for participant memory context and refresh.
        participants = list(session.participant_names.items())
        context = self.bot.memory.context_block(participants)
        try:
            summary = await session.summarizer.summarize(
                transcript_text, meta, participant_context=context
            )
        except SummaryError as exc:
            await self._handle_summary_failure(interaction, session, str(exc))
            return
        except Exception as exc:
            log.exception("Unexpected summarization error for meeting %s.", session.meeting_id)
            await self._handle_summary_failure(interaction, session, f"unexpected error: {exc}")
            return

        summary_path: Path | None = config.get().data_dir / "transcripts" / f"{session.meeting_id}.md"
        try:
            summary_path.parent.mkdir(parents=True, exist_ok=True)
            summary_path.write_text(summary, encoding="utf-8")
            database.update_meeting(
                session.meeting_id, status="completed", summary_path=str(summary_path)
            )
        except OSError as exc:
            log.error("Failed to write summary file for meeting %s: %s", session.meeting_id, exc)
            database.append_log(session.meeting_id, f"Failed to write summary file: {exc}")
            database.update_meeting(session.meeting_id, status="completed")
            summary_path = None
        database.append_log(
            session.meeting_id,
            f"Summary generated by {session.summarizer.display_target()}.",
        )

        chunks = split_message(summary) or ["(The summarizer returned an empty summary.)"]
        attach = len(summary) > ATTACH_THRESHOLD and summary_path is not None
        filename = f"scriber-summary-{session.meeting_id}.md"
        for index, chunk in enumerate(chunks):
            is_last = index == len(chunks) - 1
            file_path = summary_path if (attach and is_last) else None
            if index == 0:
                await self._send(
                    interaction, session.text_channel, chunk, file_path=file_path, filename=filename
                )
            else:
                await self._send_channel(session.text_channel, chunk, file_path, filename)

        # Best-effort per-user memory refresh. The summary is already delivered,
        # so a failure here must never fail the stop — log it and move on.
        when = meta["started_at"]
        for uid, name in participants:
            try:
                await self.bot.memory.update_from_meeting(
                    session.summarizer, uid, name, transcript_text, summary, when
                )
            except Exception as exc:
                log.warning(
                    "Meeting %s: memory update failed for %s: %s",
                    session.meeting_id, name, exc,
                )
                database.append_log(
                    session.meeting_id, f"Memory update failed for {name}: {exc}"
                )
            else:
                database.append_log(session.meeting_id, f"Updated memory for {name}.")

    @app_commands.command(
        name="cancel", description="Stop recording and discard everything without summarizing."
    )
    async def cancel(self, interaction: discord.Interaction) -> None:
        """Abort the active session and discard all captured data."""
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message(
                "This command can only be used inside a server.", ephemeral=True
            )
            return
        session = self.bot.active_sessions.get(guild.id)
        if session is None:
            await interaction.response.send_message(
                "There is no active recording session in this server.", ephemeral=True
            )
            return

        await interaction.response.defer()
        self.bot.active_sessions.pop(guild.id, None)
        self._cancel_monitor(guild.id)
        try:
            await session.cancel()
        except Exception as exc:
            log.exception("Error while cancelling meeting %s.", session.meeting_id)
            await self._send(
                interaction,
                session.text_channel,
                f"⚠️ Cleanup hit an error while cancelling the recording: {exc}. "
                "The session has been stopped and its data discarded.",
            )
            return
        await self._send(
            interaction,
            session.text_channel,
            "Recording cancelled — the transcript has been discarded.",
        )
