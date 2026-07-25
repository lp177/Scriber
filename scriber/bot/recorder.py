"""Audio sink that groups incoming Discord voice packets into per-speaker segments."""

from __future__ import annotations

import threading
import time
from typing import Callable

import discord
import numpy as np
from discord.ext import voice_recv

#: Discord voice PCM format: signed 16-bit little-endian, stereo, 48000 Hz.
BYTES_PER_SECOND = 48000 * 2 * 2
#: Bytes per stereo frame (one 48 kHz sample across both channels).
_FRAME_BYTES = 4
#: Largest intra-buffer gap (in 48 kHz frames) reconstructed with silence.
#: Discord stops sending packets while a speaker's voice gate is closed (DTX),
#: so short pauses inside a speech burst arrive as a jump in the RTP timestamp
#: with no packets in between. Without filling those gaps the buffer is a
#: time-compressed jump-cut of the real audio, which ruins the kept meeting
#: recording (speech placed at real timestamps but shorter than reality =
#: choppy audio). Gaps beyond this cap (~2 s; the segmenter flushes after
#: 0.8 s of silence anyway) are treated as a new burst instead of being filled.
_MAX_GAP_FILL_FRAMES = 2 * 48000
#: Tail window used to estimate the speaker's noise floor before a gap.
_TAIL_WINDOW_FRAMES = 960  # 20 ms
#: Comfort-noise ceiling (absolute sample amplitude, ~-55 dBFS).
_NOISE_CAP = 60
#: Ramp applied to the first frames of the packet that resumes after a fill,
#: so the (transmitter-clipped) onset pops in less harshly.
_FILL_FADE_FRAMES = 240  # 5 ms
#: Gaps up to this long are concealed by extending the previous audio (what
#: live Discord clients do), instead of inserting silence: short voice-gate
#: flaps mid-speech then go unnoticed in the recording, matching what
#: listeners heard live. Longer gaps get comfort noise — repeating speech for
#: hundreds of ms would sound robotic.
_CONCEAL_MAX_FRAMES = 150 * 48  # 150 ms
#: Speech-level RMS above which the tail is worth extending instead of muting.
_CONCEAL_MIN_RMS = 150.0


def _conceal_gap(tail: bytes, frames: int) -> bytes | None:
    """Packet-loss-style concealment: extend the tail audio over a short gap.

    Tiles the last up-to-20 ms of received audio across the gap with a decay
    ramp, the same trick receivers use for lost frames. Returns None when the
    gap is too long or the tail is too quiet to extend (caller falls back to
    comfort noise).
    """
    if frames > _CONCEAL_MAX_FRAMES or not tail:
        return None
    samples = np.frombuffer(tail, dtype=np.int16)
    if samples.size < 2:
        return None
    stereo = samples[: samples.size - (samples.size % 2)].reshape(-1, 2)
    rms = float(np.sqrt(np.mean(stereo.astype(np.float64) ** 2)))
    if rms < _CONCEAL_MIN_RMS:
        return None
    repeats = -(-frames // stereo.shape[0])  # ceil division
    tiled = np.tile(stereo, (repeats, 1))[:frames]
    # Decay across the gap so a longer conceal fades toward silence.
    ramp = np.linspace(0.95, 0.25, frames)
    return (tiled * ramp[:, None]).astype(np.int16).tobytes()


def _comfort_fill(tail: bytes, frames: int) -> bytes:
    """Build the PCM that stands in for a transmission gap.

    Absolute digital silence between two speech chunks is perceptually jarring
    ("the audio cut out") — live Discord clients mask DTX pauses with comfort
    noise. Match that: fill with low-level noise at the speaker's own noise
    floor (estimated from the faded tail before the gap, hard-capped very
    low). A source that is digitally silent keeps plain zeros. The noise is
    mono duplicated into both channels so stereo diagnostics stay clean.
    """
    floor = 0.0
    if tail:
        samples = np.frombuffer(tail, dtype=np.int16).astype(np.float64)
        floor = float(np.sqrt(np.mean(samples * samples)))
    amplitude = min(int(floor * 1.7), _NOISE_CAP)
    if amplitude < 8:
        return b"\x00" * (frames * _FRAME_BYTES)
    noise = np.random.randint(-amplitude, amplitude + 1, size=frames, dtype=np.int16)
    return np.repeat(noise, 2).tobytes()


def _fade_in(pcm: bytes) -> bytes:
    """Apply a short linear fade-in to the start of a stereo PCM packet."""
    frames = min(_FILL_FADE_FRAMES, len(pcm) // _FRAME_BYTES)
    if frames <= 0:
        return pcm
    samples = np.frombuffer(pcm, dtype=np.int16).copy()
    ramp = np.linspace(0.0, 1.0, frames)
    head = samples[: frames * 2].reshape(frames, 2) * ramp[:, None]
    samples[: frames * 2] = head.astype(np.int16).reshape(-1)
    return samples.tobytes()


class _UserBuffer:
    """Accumulated PCM audio for a single speaker, pending segment emission."""

    __slots__ = (
        "display_name", "data", "first_packet_ts", "last_packet_ts",
        "expected_rtp", "last_ssrc",
    )

    def __init__(self, display_name: str, now: float) -> None:
        self.display_name = display_name
        self.data = bytearray()
        self.first_packet_ts = now
        self.last_packet_ts = now
        #: RTP timestamp (48 kHz units, uint32) the next packet should carry if
        #: the stream is contiguous; None until the first packet is appended.
        self.expected_rtp: int | None = None
        #: SSRC the RTP continuity belongs to — a user whose packets arrive on
        #: a different stream (screen share, renegotiation) must not have a
        #: bogus "gap" computed across unrelated timestamp bases.
        self.last_ssrc: int | None = None


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
        #: Per-user gap reconstruction stats: user_id -> [fill count, seconds].
        self._fill_stats: dict[str, list[float]] = {}
        #: Per-packet forensic trace lines, drained by the session's flush
        #: loop into ``{meeting}.packets.log`` (see drain_packet_log()).
        self._packet_log: list[str] = []
        #: SSRCs observed per user id (a user should normally have exactly one).
        self._user_ssrcs: dict[str, set[int]] = {}
        #: Packets that arrived without a resolvable user, per SSRC.
        self._unmapped: dict[int | None, int] = {}

    def gap_fill_stats(self) -> dict[str, tuple[int, float]]:
        """Per-user count and total seconds of RTP gaps filled with silence."""
        with self._lock:
            return {
                uid: (int(count), float(seconds))
                for uid, (count, seconds) in self._fill_stats.items()
            }

    def drain_packet_log(self) -> list[str]:
        """Return and clear the buffered per-packet trace lines."""
        with self._lock:
            lines = self._packet_log
            self._packet_log = []
            return lines

    def stream_report(self) -> dict:
        """Reception anomalies summary: SSRCs per user, unmapped packet counts."""
        with self._lock:
            return {
                "user_ssrcs": {uid: sorted(s) for uid, s in self._user_ssrcs.items()},
                "unmapped": dict(self._unmapped),
            }

    def wants_opus(self) -> bool:
        """Request decoded PCM audio instead of raw opus packets."""
        return False

    def write(self, user: discord.User | discord.Member | None, data: voice_recv.VoiceData) -> None:
        """Append one voice packet to the speaker's buffer (audio-thread safe).

        Uses the packet's RTP timestamp (48 kHz sample units) to reconstruct
        Discord's silence-gated gaps: when the timestamp jumps forward more
        than the received audio accounts for, the missing span is filled with
        silence so the buffer stays time-accurate (see ``_MAX_GAP_FILL_FRAMES``).
        """
        packet = getattr(data, "packet", None)
        rtp_ts = getattr(packet, "timestamp", None)
        seq = getattr(packet, "sequence", None)
        ssrc = getattr(packet, "ssrc", None)
        now = time.monotonic()
        pcm = data.pcm
        if user is None:
            # Packets that cannot be mapped to a user are DROPPED — count and
            # trace them, because intermittent SSRC->user mapping loss chops a
            # speaker's recording while sounding fine live.
            with self._lock:
                self._unmapped[ssrc] = self._unmapped.get(ssrc, 0) + 1
                self._packet_log.append(
                    f"{now:.3f} ? {ssrc} {seq} {rtp_ts} {len(pcm) if pcm else 0}"
                )
            return
        if not pcm:
            return
        user_id = str(user.id)
        frames = len(pcm) // _FRAME_BYTES
        with self._lock:
            self._packet_log.append(
                f"{now:.3f} {user_id} {ssrc} {seq} {rtp_ts} {len(pcm)}"
            )
            if ssrc is not None:
                self._user_ssrcs.setdefault(user_id, set()).add(ssrc)
            buffer = self._buffers.get(user_id)
            if buffer is None:
                buffer = _UserBuffer(user.display_name, now)
                self._buffers[user_id] = buffer
            else:
                # Keep the latest display name for this user.
                buffer.display_name = user.display_name
                if (
                    rtp_ts is not None
                    and buffer.expected_rtp is not None
                    and ssrc == buffer.last_ssrc
                ):
                    # uint32 arithmetic: reordered/reset packets yield a huge
                    # "gap" and are simply appended without fill. An SSRC
                    # change means an unrelated timestamp base — no gap math.
                    gap = (rtp_ts - buffer.expected_rtp) & 0xFFFFFFFF
                    if 0 < gap <= _MAX_GAP_FILL_FRAMES:
                        tail = bytes(
                            buffer.data[-(_TAIL_WINDOW_FRAMES * _FRAME_BYTES):]
                        )
                        # Short mid-speech gap: conceal like a live client
                        # would; otherwise fill with comfort noise.
                        filler = _conceal_gap(tail, gap)
                        if filler is None:
                            filler = _comfort_fill(tail, gap)
                        buffer.data.extend(filler)
                        # Soften the resume: the transmitter's gate often
                        # re-opens mid-word, so the first packet after a gap
                        # pops in at full speech level.
                        pcm = _fade_in(pcm)
                        stats = self._fill_stats.setdefault(user_id, [0, 0.0])
                        stats[0] += 1
                        stats[1] += gap / 48000
            buffer.data.extend(pcm)
            buffer.last_packet_ts = now
            buffer.last_ssrc = ssrc
            if rtp_ts is not None:
                buffer.expected_rtp = (rtp_ts + frames) & 0xFFFFFFFF

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
