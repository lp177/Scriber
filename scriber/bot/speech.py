"""Read dashboard chat messages aloud in the meeting's voice channel.

A message posted from the dashboard with "read aloud" on is synthesized by
Mistral's Voxtral TTS (``/v1/audio/speech``, with the Voxtral API key and base
URL of the settings, model ``TTS_MODEL``, voice ``TTS_VOICE``) and played by
the bot in the voice channel it is recording, so the people in the call hear
it without watching the chat. Like the other providers it uses raw ``httpx``.

Each recording session owns one :class:`Speaker`: a small queue played one
message at a time. Its progress is stored on the chat row (``tts``: queued →
speaking → spoken, or failed / skipped) so the dashboard can show it. Reading
aloud is best-effort: a failure is logged on the meeting and never affects the
message itself nor the recording.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import io
import logging
import re
from typing import TYPE_CHECKING

import discord
import httpx

from scriber import config, database

if TYPE_CHECKING:
    from scriber.bot.session import MeetingSession

log = logging.getLogger(__name__)

#: HTTP timeout of one synthesis request, in seconds.
REQUEST_TIMEOUT = 60.0
#: Messages that may wait to be read aloud in one meeting.
MAX_QUEUE = 5
#: Mistral's list price for Voxtral TTS, in USD per 1,000 characters (estimate
#: shown in the meeting's cost; a model without a known price is recorded as such).
PRICE_PER_1K_CHARS: dict[str, float] = {
    "voxtral-mini-tts-2603": 0.016,
    "voxtral-mini-tts-latest": 0.016,
    "voxtral-mini-tts-3-0": 0.016,
}

# Markdown markers read out loud as noise ("asterisk asterisk…").
_MARKDOWN = re.compile(r"[*_~`|>#]+")
_URL = re.compile(r"https?://\S+")


class SpeechError(Exception):
    """A message cannot be read aloud."""


def unavailable_reason(session: "MeetingSession") -> str | None:
    """Why messages cannot be read aloud in this session right now, or ``None``."""
    if not config.get().stt_providers["voxtral"].ready:
        return "Reading aloud needs a Mistral API key (Voxtral settings)."
    voice_client = session.voice_client
    if voice_client is None or not voice_client.is_connected():
        return "The bot is not connected to the voice channel."
    me = session.guild.me
    if me is not None and not session.voice_channel.permissions_for(me).speak:
        return "The bot lacks the Speak permission in this voice channel."
    return None


def spoken_text(author: str, content: str) -> str:
    """What is read aloud: the author's name, then the message without markup."""
    text = _URL.sub("link", content)
    text = " ".join(_MARKDOWN.sub(" ", text).split())
    return f"{author}: {text}"


async def synthesize(text: str) -> tuple[bytes, str]:
    """Synthesize ``text`` with Voxtral TTS; returns the audio (MP3) and the model."""
    cfg = config.get()
    provider = cfg.stt_providers["voxtral"]
    if not provider.ready:
        raise SpeechError("No Mistral API key is configured.")
    base = (provider.base_url or "https://api.mistral.ai").rstrip("/")
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
        response = await client.post(
            f"{base}/v1/audio/speech",
            headers={
                "x-api-key": provider.api_key,
                "Authorization": f"Bearer {provider.api_key}",
            },
            json={
                "model": cfg.tts_model,
                "input": text,
                "voice_id": cfg.tts_voice,
                "response_format": "mp3",
            },
        )
    if response.status_code >= 400:
        raise SpeechError(
            f"Voxtral TTS returned HTTP {response.status_code}: {response.text[:300]}"
        )
    if response.headers.get("content-type", "").startswith("audio/"):
        return response.content, cfg.tts_model
    try:
        audio = base64.b64decode(response.json()["audio_data"])
    except (ValueError, KeyError, TypeError, binascii.Error) as exc:
        raise SpeechError("Voxtral TTS returned no audio.") from exc
    if not audio:
        raise SpeechError("Voxtral TTS returned no audio.")
    return audio, cfg.tts_model


class Speaker:
    """Reads a meeting's dashboard messages aloud, one at a time."""

    def __init__(self, session: "MeetingSession") -> None:
        self._session = session
        self._queue: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()
        self._task: asyncio.Task[None] | None = None
        self._closed = False

    def check(self) -> None:
        """Raise :class:`SpeechError` if a new message cannot be queued now."""
        if self._closed:
            raise SpeechError("The recording is ending.")
        reason = unavailable_reason(self._session)
        if reason is not None:
            raise SpeechError(reason)
        if self._queue.qsize() >= MAX_QUEUE:
            raise SpeechError(
                f"{MAX_QUEUE} messages are already waiting to be read aloud — try again shortly."
            )

    def say(self, row_id: int, author: str, content: str) -> None:
        """Queue a stored chat row (already marked ``queued``) to be read aloud."""
        self._queue.put_nowait((row_id, author, content))
        if self._task is None:
            self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        while True:
            row_id, author, content = await self._queue.get()
            try:
                await self._speak(row_id, author, content)
            except asyncio.CancelledError:
                database.set_chat_tts(row_id, "skipped")
                raise
            except Exception as exc:
                log.warning("Meeting %s: could not read a message aloud: %s", self._session.meeting_id, exc)
                database.set_chat_tts(row_id, "failed")
                database.append_log(
                    self._session.meeting_id, f"Could not read {author}'s message aloud: {exc}"
                )

    async def _speak(self, row_id: int, author: str, content: str) -> None:
        text = spoken_text(author, content)
        audio, model = await synthesize(text)
        price = PRICE_PER_1K_CHARS.get(model)
        database.record_usage(
            self._session.meeting_id,
            "tts",
            provider="mistral",
            model=model,
            cost_usd=len(text) / 1000 * price if price is not None else None,
            label=author,
        )
        voice_client = self._session.voice_client
        if voice_client is None or not voice_client.is_connected():
            raise SpeechError("The bot is not connected to the voice channel.")
        loop = asyncio.get_running_loop()
        done: asyncio.Future[None] = loop.create_future()

        def finished(error: Exception | None) -> None:
            if done.done():
                return
            if error is not None:
                done.set_exception(error)
            else:
                done.set_result(None)

        source = discord.FFmpegPCMAudio(io.BytesIO(audio), pipe=True)
        database.set_chat_tts(row_id, "speaking")
        # ``after`` runs on the player thread; hop back to the loop.
        voice_client.play(source, after=lambda error: loop.call_soon_threadsafe(finished, error))
        try:
            await done
        except asyncio.CancelledError:
            # VoiceRecvClient.stop() would also stop the recording: stop playing only.
            with contextlib.suppress(Exception):
                voice_client.stop_playing()
            raise
        database.set_chat_tts(row_id, "spoken")

    async def close(self) -> None:
        """Stop reading (the recording ends); messages still waiting are skipped."""
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        while not self._queue.empty():
            row_id, _author, _content = self._queue.get_nowait()
            database.set_chat_tts(row_id, "skipped")
