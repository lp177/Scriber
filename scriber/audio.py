"""Meeting audio archive: capture, mixdown, and segment storage for replay/regen.

When ``AUDIO_KEEP`` is enabled, the meeting session feeds every finished speech
segment (raw s16le stereo 48 kHz PCM from Discord) into an :class:`AudioArchive`.
During the recording the archive spools segments to disk as a *compact track*
(all segments back-to-back, downmixed to mono) plus an in-memory index of who
spoke when. When the meeting stops, ``finalize()`` produces:

- ``{id}.ogg`` — the playable/downloadable meeting audio: every segment placed
  at its real time offset and mixed into one mono 48 kHz Opus file. Falls back
  to ``{id}.wav`` when ffmpeg is not installed.
- ``{id}.segments.ogg`` (or ``.wav``) + ``{id}.segments.json`` — the compact
  per-speaker segment track and its index, used to re-transcribe the meeting
  with a different engine while keeping speaker attribution.

Everything here is best-effort by design: an audio problem must never fail the
meeting itself. The blocking helpers (finalize, decode) are run by callers in a
worker thread (``asyncio.to_thread``).
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
import wave
from pathlib import Path

import numpy as np

log = logging.getLogger(__name__)

#: Sample rate of the archived audio (Discord voice native rate).
SAMPLE_RATE = 48_000
#: Sample rate segments are decoded to for re-transcription (Whisper's rate).
REGEN_SAMPLE_RATE = 16_000
#: Opus bitrate for the encoded files — comfortable headroom for voice.
OPUS_BITRATE = "48k"

#: Every file suffix the archive may create for a meeting id (the packet
#: trace is written by the session next to the audio and expires with it).
_ALL_SUFFIXES: tuple[str, ...] = (
    ".ogg",
    ".wav",
    ".segments.ogg",
    ".segments.wav",
    ".segments.json",
    ".segments.raw",
    ".mix.raw",
    ".packets.log",
)


def ffmpeg_available() -> bool:
    """True when the ffmpeg binary is on PATH (Opus encode/decode support)."""
    return shutil.which("ffmpeg") is not None


def mix_path_for(audio_dir: Path, meeting_id: str) -> Path | None:
    """Return the existing playable mix file for a meeting, or None."""
    for suffix in (".ogg", ".wav"):
        candidate = audio_dir / f"{meeting_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def segments_available(audio_dir: Path, meeting_id: str) -> bool:
    """True when the segment track + index needed for regeneration exist."""
    index = audio_dir / f"{meeting_id}.segments.json"
    if not index.is_file():
        return False
    track = _segments_track(audio_dir, meeting_id)
    if track is None:
        return False
    # An .ogg track needs ffmpeg to be decoded again.
    return track.suffix != ".ogg" or ffmpeg_available()


def delete_meeting_audio(audio_dir: Path, meeting_id: str) -> None:
    """Remove every audio artifact of a meeting (best-effort)."""
    for suffix in _ALL_SUFFIXES:
        path = audio_dir / f"{meeting_id}{suffix}"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            log.warning("Failed to delete audio file %s", path)


def _segments_track(audio_dir: Path, meeting_id: str) -> Path | None:
    """Return the compact segment track file (ogg or wav), or None."""
    for suffix in (".segments.ogg", ".segments.wav"):
        candidate = audio_dir / f"{meeting_id}{suffix}"
        if candidate.is_file():
            return candidate
    return None


def downmix_to_mono(pcm: bytes) -> tuple[np.ndarray, dict]:
    """s16le stereo 48 kHz bytes -> (mono int16 samples, stereo diagnostics).

    Discord voice is normally mono duplicated into both channels, where a
    plain channel average is perfect. But some processed setups (virtual
    mixers, stereo mic chains) send genuinely different L/R with phase offset;
    averaging those causes comb filtering / cancellation — audio that sounds
    hollow and chopped in the recording while being fine live. So: average
    only when the channels are near-identical, otherwise keep the stronger
    channel untouched. The diagnostics (correlation + per-channel RMS) are
    stored in the segment index to make this visible per speaker.
    """
    usable = len(pcm) - (len(pcm) % 4)
    if usable <= 0:
        return np.empty(0, dtype=np.int16), {}
    stereo = np.frombuffer(pcm[:usable], dtype=np.int16).reshape(-1, 2)
    left = stereo[:, 0].astype(np.float32)
    right = stereo[:, 1].astype(np.float32)
    rms_l = float(np.sqrt(np.mean(left**2)))
    rms_r = float(np.sqrt(np.mean(right**2)))
    if rms_l < 1.0 or rms_r < 1.0:
        # One channel silent (or both): correlation is meaningless; the mean
        # equals the live channel halved, so take the stronger one instead.
        corr = 1.0 if rms_l < 1.0 and rms_r < 1.0 else 0.0
    else:
        corr = float(np.corrcoef(left, right)[0, 1])
        if np.isnan(corr):
            corr = 1.0
    if corr >= 0.95:
        mono = stereo.mean(axis=1).astype(np.int16)
        mode = "mean"
    else:
        mono = (stereo[:, 0] if rms_l >= rms_r else stereo[:, 1]).copy()
        mode = "left" if rms_l >= rms_r else "right"
    return mono, {
        "stereo_corr": round(corr, 3),
        "rms_l": round(rms_l, 1),
        "rms_r": round(rms_r, 1),
        "downmix": mode,
    }


def _write_wav_from_raw(raw_path: Path, wav_path: Path) -> None:
    """Wrap a raw s16le mono 48 kHz file into a WAV container (streamed)."""
    with open(raw_path, "rb") as src, wave.open(str(wav_path), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        while True:
            chunk = src.read(1 << 20)
            if not chunk:
                break
            out.writeframes(chunk)


def _encode_opus(raw_path: Path, ogg_path: Path) -> None:
    """Encode a raw s16le mono 48 kHz file to Opus-in-Ogg with ffmpeg."""
    result = subprocess.run(
        [
            "ffmpeg", "-y", "-v", "error",
            "-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", "1",
            "-i", str(raw_path),
            "-c:a", "libopus", "-b:a", OPUS_BITRATE, "-vbr", "on",
            str(ogg_path),
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg encoding failed: "
            + result.stderr.decode("utf-8", "replace").strip()[:400]
        )


class AudioArchive:
    """Spools one meeting's speech segments and produces the final audio files.

    ``append()`` is called from the event loop for every finished segment and
    only does a small file append plus an index entry. ``finalize()`` and
    ``discard()`` are called once when the meeting ends.
    """

    def __init__(self, audio_dir: Path, meeting_id: str) -> None:
        self.audio_dir = audio_dir
        self.meeting_id = meeting_id
        audio_dir.mkdir(parents=True, exist_ok=True)
        self._raw_path = audio_dir / f"{meeting_id}.segments.raw"
        self._raw = open(self._raw_path, "wb")
        #: Index entries: user_id, name, start (s), duration (s), and the
        #: segment's position in the compact track (offset/length in samples).
        self._index: list[dict] = []
        self._samples_written = 0
        self._failed = False

    def append(self, user_id: str, display_name: str, start: float, pcm: bytes) -> None:
        """Spool one finished speech segment (stereo 48 kHz PCM bytes)."""
        if self._failed or self._raw.closed:
            return
        mono, stereo_diag = downmix_to_mono(pcm)
        if mono.size == 0:
            return
        try:
            self._raw.write(mono.tobytes())
        except OSError:
            # Disk trouble: give up on audio for this meeting, keep transcribing.
            log.exception("Meeting %s: audio spool write failed; disabling audio capture.",
                          self.meeting_id)
            self._failed = True
            return
        self._index.append(
            {
                "user_id": user_id,
                "name": display_name,
                "start": round(max(0.0, start), 3),
                "duration": round(mono.size / SAMPLE_RATE, 3),
                "offset": self._samples_written,
                "length": int(mono.size),
                **stereo_diag,
            }
        )
        self._samples_written += int(mono.size)

    def stereo_summary(self) -> dict[str, dict]:
        """Per-speaker stereo diagnostics aggregated over the spooled segments."""
        out: dict[str, dict] = {}
        for entry in self._index:
            if "stereo_corr" not in entry:
                continue
            summary = out.setdefault(entry["name"], {"min_corr": 1.0, "modes": set()})
            summary["min_corr"] = min(summary["min_corr"], entry["stereo_corr"])
            summary["modes"].add(entry.get("downmix", "mean"))
        return out

    def discard(self) -> None:
        """Close and delete everything (meeting cancelled)."""
        try:
            self._raw.close()
        except OSError:
            pass
        delete_meeting_audio(self.audio_dir, self.meeting_id)

    def finalize(self, duration_seconds: float) -> Path | None:
        """Produce the playable mix + segment track; return the mix path.

        Blocking (numpy mixdown + ffmpeg encode) — run in a worker thread.
        Returns None (and cleans up) when nothing was captured or on failure.
        """
        try:
            self._raw.close()
        except OSError:
            pass
        if self._failed or not self._index:
            delete_meeting_audio(self.audio_dir, self.meeting_id)
            return None
        try:
            return self._finalize_inner(duration_seconds)
        except Exception:
            log.exception("Meeting %s: audio finalize failed; no audio kept.", self.meeting_id)
            delete_meeting_audio(self.audio_dir, self.meeting_id)
            return None

    def _finalize_inner(self, duration_seconds: float) -> Path:
        """Mix the compact track onto the meeting timeline and encode both files."""
        last_end = max(e["start"] + e["duration"] for e in self._index)
        total_seconds = max(duration_seconds, last_end)
        total_samples = max(1, int(round(total_seconds * SAMPLE_RATE)))

        mix_raw = self.audio_dir / f"{self.meeting_id}.mix.raw"
        segments = np.memmap(self._raw_path, dtype=np.int16, mode="r")
        mix = np.memmap(mix_raw, dtype=np.int16, mode="w+", shape=(total_samples,))
        try:
            mix[:] = 0
            for entry in self._index:
                seg = segments[entry["offset"]: entry["offset"] + entry["length"]]
                at = int(round(entry["start"] * SAMPLE_RATE))
                at = min(max(0, at), total_samples)
                end = min(at + seg.size, total_samples)
                if end <= at:
                    continue
                # Saturating add so overlapping speakers cannot wrap around.
                summed = mix[at:end].astype(np.int32) + seg[: end - at].astype(np.int32)
                mix[at:end] = np.clip(summed, -32768, 32767).astype(np.int16)
            mix.flush()
        finally:
            del mix
            del segments

        use_ffmpeg = ffmpeg_available()
        if use_ffmpeg:
            mix_path = self.audio_dir / f"{self.meeting_id}.ogg"
            seg_path = self.audio_dir / f"{self.meeting_id}.segments.ogg"
            _encode_opus(mix_raw, mix_path)
            _encode_opus(self._raw_path, seg_path)
        else:
            log.warning(
                "ffmpeg not found — storing meeting %s audio as uncompressed WAV. "
                "Install ffmpeg to get much smaller Opus files.",
                self.meeting_id,
            )
            mix_path = self.audio_dir / f"{self.meeting_id}.wav"
            seg_path = self.audio_dir / f"{self.meeting_id}.segments.wav"
            _write_wav_from_raw(mix_raw, mix_path)
            _write_wav_from_raw(self._raw_path, seg_path)

        index_path = self.audio_dir / f"{self.meeting_id}.segments.json"
        index_path.write_text(
            json.dumps({"sample_rate": SAMPLE_RATE, "segments": self._index}),
            encoding="utf-8",
        )
        mix_raw.unlink(missing_ok=True)
        self._raw_path.unlink(missing_ok=True)
        return mix_path


# ---------------------------------------------------------------------------
# Reader side (transcript regeneration)
# ---------------------------------------------------------------------------


def load_segment_index(audio_dir: Path, meeting_id: str) -> list[dict]:
    """Load the segment index for a meeting (raises on a missing/corrupt file)."""
    index_path = audio_dir / f"{meeting_id}.segments.json"
    data = json.loads(index_path.read_text(encoding="utf-8"))
    segments = data.get("segments")
    if not isinstance(segments, list):
        raise ValueError(f"Corrupt segment index for meeting {meeting_id}")
    return segments


def decode_segments_track(audio_dir: Path, meeting_id: str) -> np.ndarray:
    """Decode the compact segment track to mono int16 samples at 16 kHz.

    Blocking (ffmpeg decode / WAV read) — run in a worker thread. Index sample
    offsets are stored at 48 kHz; divide by 3 to address into this array.
    """
    track = _segments_track(audio_dir, meeting_id)
    if track is None:
        raise FileNotFoundError(f"No segment audio stored for meeting {meeting_id}")
    if track.suffix == ".wav":
        with wave.open(str(track), "rb") as src:
            raw = src.readframes(src.getnframes())
        # The archive writes 48 kHz mono WAVs; decimate like the live pipeline.
        return np.frombuffer(raw, dtype=np.int16)[::3]
    result = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-i", str(track),
            "-f", "s16le", "-acodec", "pcm_s16le",
            "-ar", str(REGEN_SAMPLE_RATE), "-ac", "1",
            "-",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(
            "ffmpeg decoding failed: "
            + result.stderr.decode("utf-8", "replace").strip()[:400]
        )
    return np.frombuffer(result.stdout, dtype=np.int16)


def slice_segment(track_16k: np.ndarray, entry: dict) -> np.ndarray:
    """Cut one indexed segment out of the decoded 16 kHz compact track."""
    start = max(0, int(entry["offset"]) // 3)
    length = max(0, int(entry["length"]) // 3)
    return track_16k[start: min(start + length, track_16k.size)]


def pcm16_to_wav_bytes(samples: np.ndarray, sample_rate: int = REGEN_SAMPLE_RATE) -> bytes:
    """Wrap mono int16 samples into an in-memory WAV file (for API uploads)."""
    import io

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(sample_rate)
        out.writeframes(samples.astype(np.int16).tobytes())
    return buffer.getvalue()
