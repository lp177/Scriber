"""ScribBot: the Discord client wiring intents, slash commands and shared services."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import discord
from discord.ext import commands

from scriber import config
from scriber.bot.commands import ScribCommands

if TYPE_CHECKING:
    from scriber.bot.session import MeetingSession
    from scriber.memory import MemoryManager
    from scriber.summary.summarizer import Summarizer
    from scriber.transcription.whisper_engine import WhisperEngine

log = logging.getLogger(__name__)


def _enable_dave_receive_decryption() -> None:
    """Teach ``discord-ext-voice-recv`` to decrypt DAVE (voice E2EE) audio.

    Discord negotiates DAVE (MLS-based end-to-end encryption) for voice, and on
    servers that require it the connection cannot opt out — advertising DAVE
    protocol version 0 is rejected with close code 4017. discord.py builds a
    ``davey`` MLS session holding the group keys, but voice_recv reads raw
    packets and only strips the transport encryption, handing the still
    E2EE-encrypted payload to Opus (``OpusError: corrupted stream`` → empty
    transcripts).

    We wrap voice_recv's per-connection decryptor so that, after transport
    decryption, each packet is also run through the connection's davey session,
    keyed by the sender resolved from the packet's SSRC. When DAVE is inactive
    (or the MLS session is not ready yet) the transport-decrypted bytes pass
    through unchanged, so non-E2EE servers keep working too. Patching
    ``AudioReader.__init__`` covers every future voice connection.
    """
    # Standard 3-byte Opus silence frame; a valid frame that decodes to silence,
    # used as a fallback so one undecryptable packet never kills the reader.
    opus_silence = b"\xf8\xff\xfe"
    try:
        import davey
        from discord.ext.voice_recv import reader as voice_reader

        original_init = voice_reader.AudioReader.__init__

        def init_with_dave(self, *args, **kwargs) -> None:
            original_init(self, *args, **kwargs)
            decryptor = self.decryptor
            transport_decrypt = decryptor.decrypt_rtp
            voice_client = self.voice_client
            confirmed = {"dave": False}

            def decrypt_rtp(packet):
                data = transport_decrypt(packet)
                connection = getattr(voice_client, "_connection", None)
                # No DAVE negotiated: transport-decrypted bytes are already Opus.
                if not getattr(connection, "dave_protocol_version", 0):
                    return data
                session = getattr(connection, "dave_session", None)
                user_id = voice_client._ssrc_to_id.get(packet.ssrc)
                # DAVE active but this packet isn't decryptable yet (MLS session
                # not ready, or sender not resolved from SSRC): emit silence so
                # the reader thread keeps running until the session is ready,
                # instead of dying on a single undecodable frame.
                if session is None or not getattr(session, "ready", False) or user_id is None:
                    return opus_silence
                try:
                    out = session.decrypt(user_id, davey.MediaType.audio, data)
                except Exception:
                    log.debug("DAVE decrypt failed for ssrc %s.", packet.ssrc, exc_info=True)
                    return opus_silence
                if out and not confirmed["dave"]:
                    confirmed["dave"] = True
                    log.info("DAVE voice end-to-end encryption is being decrypted for this recording.")
                return out or opus_silence

            decryptor.decrypt_rtp = decrypt_rtp

        voice_reader.AudioReader.__init__ = init_with_dave
    except Exception:  # pragma: no cover - defensive against library internals moving
        log.warning(
            "Could not enable DAVE audio decryption; voice recordings on servers "
            "that require voice E2EE may come out empty.",
            exc_info=True,
        )


_enable_dave_receive_decryption()

# Permissions advertised by the invite URL, matching the README:
# View Channels, Send Messages, Attach Files, Connect.
INVITE_PERMISSIONS = discord.Permissions(1084416)


class ScribBot(commands.Bot):
    """Discord bot that records, transcribes and summarizes voice-channel meetings."""

    def __init__(
        self, whisper: "WhisperEngine", summarizer: "Summarizer", memory: "MemoryManager"
    ) -> None:
        intents = discord.Intents.default()
        intents.voice_states = True  # required for voice capture; message_content is not needed
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.whisper = whisper
        self.summarizer = summarizer
        self.memory = memory
        #: Active meeting sessions keyed by guild ID (one session per guild).
        self.active_sessions: dict[int, "MeetingSession"] = {}
        #: OAuth2 invite URL, set once the application id is known in setup_hook.
        self.invite_url: str | None = None
        #: Human-readable problem that left the bot degraded (e.g. command sync
        #: was refused). Surfaced by the web dashboard; None when healthy.
        self.setup_error: str | None = None
        #: The /scriber command group, set in setup_hook. The voice-state
        #: handler calls into it to auto-stop a session when left alone.
        #: Named ``scrib_commands`` because ``commands.Bot.commands`` is a
        #: read-only property (the prefix-command registry).
        self.scrib_commands: ScribCommands | None = None

    async def setup_hook(self) -> None:
        """Register the /scriber command group and sync the application command tree.

        A refused command sync (typically the bot was not invited with the
        ``applications.commands`` scope, or is not in the target server) is
        recorded on :attr:`setup_error` and surfaced by the dashboard rather
        than crashing the whole process. Once the invite is fixed, the dashboard
        can re-run the sync via :meth:`resync_commands` without a restart.
        """
        self.scrib_commands = ScribCommands(self)
        self.tree.add_command(self.scrib_commands)
        self._refresh_invite_url()
        if self.invite_url:
            log.info(
                "Invite the bot to a server (needs the applications.commands scope): %s",
                self.invite_url,
            )
        await self._sync_and_record()

    async def resync_commands(self) -> None:
        """Re-run the command sync and refresh :attr:`setup_error` / :attr:`invite_url`.

        Lets the dashboard recover from a refused sync (e.g. the bot was just
        invited with the ``applications.commands`` scope) without restarting the
        process. Safe to call repeatedly: the command group is added to the tree
        only once, in :meth:`setup_hook`.
        """
        self._refresh_invite_url()
        await self._sync_and_record()

    def _refresh_invite_url(self) -> None:
        """Compute the OAuth2 invite URL once the application id is known."""
        if self.application_id is not None and self.invite_url is None:
            self.invite_url = discord.utils.oauth_url(
                self.application_id,
                permissions=INVITE_PERMISSIONS,
                scopes=("bot", "applications.commands"),
            )

    async def _sync_and_record(self) -> None:
        """Sync commands, recording a human-readable :attr:`setup_error` on failure."""
        try:
            await self._sync_commands()
            self.setup_error = None
        except discord.Forbidden:
            self.setup_error = (
                "Discord refused to register the bot's slash commands "
                "(403 Missing Access). The bot is probably not invited with the "
                "'applications.commands' scope, or is not a member of the target "
                "server. Fix the invite with the link below, then retry."
            )
            log.error("%s Invite link: %s", self.setup_error, self.invite_url or "(unknown)")
        except discord.HTTPException as exc:
            self.setup_error = f"Could not sync slash commands with Discord: {exc}"
            log.error("Could not sync slash commands with Discord: %s", exc)

    async def _sync_commands(self) -> None:
        """Sync the application command tree to the configured guild, or globally."""
        cfg = config.get()
        if cfg.discord_guild_id:
            try:
                guild = discord.Object(id=int(cfg.discord_guild_id))
            except ValueError:
                log.error(
                    "DISCORD_GUILD_ID %r is not a valid guild ID; falling back to global sync.",
                    cfg.discord_guild_id,
                )
                await self.tree.sync()
                return
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to guild %s.", cfg.discord_guild_id)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (propagation may take up to an hour).")

    async def on_ready(self) -> None:
        """Log a line once the gateway connection is ready."""
        user = self.user
        log.info("Logged in as %s (id=%s).", user, user.id if user else "unknown")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        """Auto-stop the recording when the bot is left alone in the voice channel.

        Fires when someone leaves (or switches away from) the channel we are
        recording; if no non-bot members remain, finish the meeting exactly like
        ``/scriber stop`` would.
        """
        if self.scrib_commands is None:
            return
        # Only react to a member leaving/moving out of a channel, not joins.
        if before.channel is None or before.channel == after.channel:
            return
        session = self.active_sessions.get(member.guild.id)
        if session is None:
            return
        recording_channel_id = getattr(session.voice_channel, "id", None)
        if before.channel.id != recording_channel_id:
            return
        channel = member.guild.get_channel(recording_channel_id)
        members = getattr(channel, "members", []) if channel is not None else []
        if any(not m.bot for m in members):
            return  # humans still present — keep recording
        await self.scrib_commands.stop_session_automatically(
            member.guild.id, "⏹️ Auto-stopped: everyone left the voice channel."
        )


def create_bot(
    whisper: "WhisperEngine", summarizer: "Summarizer", memory: "MemoryManager"
) -> ScribBot:
    """Create the configured :class:`ScribBot` instance."""
    return ScribBot(whisper, summarizer, memory)
