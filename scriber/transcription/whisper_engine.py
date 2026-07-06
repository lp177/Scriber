"""Local Whisper transcription engine backed by faster-whisper.

Converts raw Discord voice PCM (signed 16-bit little-endian, stereo, 48 kHz)
into text using a lazily loaded faster-whisper model. All audio math is done
with numpy (the stdlib ``audioop`` module was removed in Python 3.13).
"""

from __future__ import annotations

import asyncio
import logging
import pathlib

import numpy as np
from faster_whisper import WhisperModel

from scriber import config

logger = logging.getLogger(__name__)

#: Sample rate of the PCM Discord delivers (Hz).
SOURCE_SAMPLE_RATE = 48_000
#: Sample rate expected by Whisper (Hz); reached by [::3] decimation.
TARGET_SAMPLE_RATE = 16_000
#: Segments shorter than this many seconds are skipped entirely.
MIN_SEGMENT_SECONDS = 0.4

#: Language codes Whisper understands (ISO 639-1, plus a few Whisper-specific
#: codes). ``"auto"`` (or an empty value) means automatic language detection.
WHISPER_LANGUAGES: frozenset[str] = frozenset({
    "af", "am", "ar", "as", "az", "ba", "be", "bg", "bn", "bo", "br", "bs",
    "ca", "cs", "cy", "da", "de", "el", "en", "es", "et", "eu", "fa", "fi",
    "fo", "fr", "gl", "gu", "ha", "haw", "he", "hi", "hr", "ht", "hu", "hy",
    "id", "is", "it", "ja", "jw", "ka", "kk", "km", "kn", "ko", "la", "lb",
    "ln", "lo", "lt", "lv", "mg", "mi", "mk", "ml", "mn", "mr", "ms", "mt",
    "my", "ne", "nl", "nn", "no", "oc", "pa", "pl", "ps", "pt", "ro", "ru",
    "sa", "sd", "si", "sk", "sl", "sn", "so", "sq", "sr", "su", "sv", "sw",
    "ta", "te", "tg", "th", "tk", "tl", "tr", "tt", "uk", "ur", "uz", "vi",
    "yi", "yo", "yue", "zh",
})


def normalize_language(value: str | None) -> str:
    """Normalize a user-supplied language value to a canonical form.

    Returns ``"auto"`` for empty input or the literal ``"auto"``; otherwise the
    lower-cased language code. Raises ``ValueError`` for an unsupported code.
    """
    if value is None:
        return "auto"
    code = value.strip().lower()
    if code in ("", "auto"):
        return "auto"
    if code not in WHISPER_LANGUAGES:
        raise ValueError(
            f"Unsupported language {value!r}. Use 'auto' or a Whisper language "
            f"code such as en, fr, de, es, it, pt, nl, ru, zh, ja."
        )
    return code


class WhisperEngine:
    """Transcribes Discord voice segments with a local faster-whisper model.

    The model is loaded on first use and reloaded transparently whenever the
    ``WHISPER_MODEL`` config value changes. Inference runs in a worker thread
    (``asyncio.to_thread``) and is serialized with an ``asyncio.Lock`` so only
    one transcription touches the model at a time.
    """

    def __init__(self, models_dir: pathlib.Path) -> None:
        """Create the engine; ``models_dir`` is the model download/cache root."""
        self._models_dir = models_dir
        self._model: WhisperModel | None = None
        self._loaded_model_name: str | None = None
        self._lock = asyncio.Lock()

    async def transcribe(self, pcm: bytes, language: str | None = None) -> str:
        """Transcribe a raw s16le stereo 48 kHz PCM segment to text.

        ``language`` overrides the configured ``WHISPER_LANGUAGE`` for this call
        (``"auto"`` forces detection); when ``None`` the configured value is
        used. Returns an empty string for segments shorter than 0.4 seconds.
        """
        audio = self._prepare_audio(pcm)
        if audio.shape[0] < int(TARGET_SAMPLE_RATE * MIN_SEGMENT_SECONDS):
            return ""
        cfg = config.get()
        effective = cfg.whisper_language if language is None else language
        async with self._lock:
            return await asyncio.to_thread(self._transcribe_sync, cfg, audio, effective)

    @staticmethod
    def _prepare_audio(pcm: bytes) -> np.ndarray:
        """Convert s16le stereo 48 kHz bytes to mono float32 16 kHz samples."""
        # Guard against a truncated trailing byte / partial stereo frame.
        usable = len(pcm) - (len(pcm) % 4)
        if usable <= 0:
            return np.empty(0, dtype=np.float32)
        samples = np.frombuffer(pcm[:usable], dtype=np.int16)
        # Stereo -> mono by averaging channels, then normalize to [-1.0, 1.0].
        mono = (samples.reshape(-1, 2).mean(axis=1) / 32768.0).astype(np.float32)
        # 48 kHz -> 16 kHz by simple decimation (keep every 3rd sample).
        return mono[::3]

    def _transcribe_sync(self, cfg: config.Config, audio: np.ndarray, lang: str) -> str:
        """Blocking worker: (re)load the model if needed and run inference."""
        model = self._ensure_model(cfg)
        language = None if lang in ("", "auto") else lang
        segments, _info = model.transcribe(audio, language=language, vad_filter=True)
        # ``segments`` is a generator; consume it here, inside the worker thread.
        return " ".join(seg.text.strip() for seg in segments).strip()

    def _ensure_model(self, cfg: config.Config) -> WhisperModel:
        """Return the loaded model, (re)loading it if the config model changed."""
        if self._model is None or self._loaded_model_name != cfg.whisper_model:
            logger.info(
                "Loading Whisper model %r (device=%s, compute_type=%s, download_root=%s)",
                cfg.whisper_model,
                cfg.whisper_device,
                cfg.whisper_compute_type,
                self._models_dir,
            )
            self._model = WhisperModel(
                cfg.whisper_model,
                device=cfg.whisper_device,
                compute_type=cfg.whisper_compute_type,
                download_root=str(self._models_dir),
            )
            self._loaded_model_name = cfg.whisper_model
            logger.info("Whisper model %r ready", cfg.whisper_model)
        return self._model
