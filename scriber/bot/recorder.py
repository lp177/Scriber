"""Audio sink that groups incoming Discord voice packets into per-speaker segments."""

from __future__ import annotations

import threading
import time
from typing import Callable

import discord
from discord.ext import voice_recv

#: Discord voice PCM format: signed 16-bit little-endian, stereo, 48000 Hz.
BYTES_PER_SECOND = 48000 * 2 * 2


class _UserBuffer:
    """Accumulated PCM audio for a single speaker, pending segment emission."""

    __slots__ = ("display_name", "data", "first_packet_ts", "last_packet_ts")

    def __init__(self, display_name: str, now: float) -> None:
        self.display_name = display_name
        self.data = bytearray()
        self.first_packet_ts = now
        self.last_packet_ts = now


class SegmentingSink(voice_recv.AudioSink):
    """Buffers raw PCM per user and emits speech segments on silence or length limits.

    ``write()`` is called from a non-async audio thread, so it only performs
    lock-protected bytearray appends. Segments are emitted by ``flush_stale()``
    and ``flush_all()``, which the meeting session drives from an asyncio task.

    The ``on_segment`` callback receives ``(user_id, display_name, monotonic
    timestamp of the segment start, pcm bytes)``, where ``user_id`` is the
    stable Discord user id as a string (``str(user.id)``).
    """

    def __init__(self, on_segment: Callable[[str, str, float, bytes], None]) -> None:
        super().__init__()
        self._on_segment = on_segment
        self._lock = threading.Lock()
        self._buffers: dict[str, _UserBuffer] = {}

    def wants_opus(self) -> bool:
        """Request decoded PCM audio instead of raw opus packets."""
        return False

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData) -> None:
        """Append one voice packet to the speaker's buffer (audio-thread safe)."""
        if user is None:
            # Packets that cannot be mapped to a user are ignored.
            return
        pcm = data.pcm
        if not pcm:
            return
        user_id = str(user.id)
        now = time.monotonic()
        with self._lock:
            buffer = self._buffers.get(user_id)
            if buffer is None:
                buffer = _UserBuffer(user.display_name, now)
                self._buffers[user_id] = buffer
            else:
                # Keep the latest display name for this user.
                buffer.display_name = user.display_name
            buffer.data.extend(pcm)
            buffer.last_packet_ts = now

    def flush_stale(self, max_silence: float = 0.8, max_len_seconds: float = 30.0) -> None:
        """Emit segments for speakers that went silent or exceeded the length limit."""
        now = time.monotonic()
        ready: list[tuple[str, _UserBuffer]] = []
        with self._lock:
            for user_id in list(self._buffers):
                buffer = self._buffers[user_id]
                duration = len(buffer.data) / BYTES_PER_SECOND
                if (now - buffer.last_packet_ts) > max_silence or duration > max_len_seconds:
                    ready.append((user_id, self._buffers.pop(user_id)))
        for user_id, buffer in ready:
            self._on_segment(
                user_id, buffer.display_name, buffer.first_packet_ts, bytes(buffer.data)
            )

    def flush_all(self) -> None:
        """Emit every remaining buffered segment (called when the recording stops)."""
        with self._lock:
            ready = list(self._buffers.items())
            self._buffers.clear()
        for user_id, buffer in ready:
            self._on_segment(
                user_id, buffer.display_name, buffer.first_packet_ts, bytes(buffer.data)
            )

    def cleanup(self) -> None:
        """Called by voice-recv when listening stops.

        Buffered audio is intentionally kept here so the session can still drain
        it via ``flush_all()`` after ``stop_listening()``.
        """
