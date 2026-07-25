"""Live meeting transcription router: local Whisper or a cloud STT engine.

``TRANSCRIBE_ENGINE`` selects which engine transcribes speech segments while a
meeting is running: ``whisper`` (local, the default — nothing leaves the
machine) or one of the cloud engines from
:mod:`scriber.transcription.providers` (``voxtral`` / ``elevenlabs`` /
``google``). The value is read live on every segment, so a dashboard change
applies immediately.

A meeting must never lose speech because a provider is down: any cloud
problem (engine unconfigured, API error, timeout) falls back to local Whisper
for that segment. When a cloud engine is active, the bot's recording notice
tells participants their speech audio is sent to that service.
"""

from __future__ import annotations

import logging

import httpx

from scriber import config
from scriber.audio import downmix_to_mono
from scriber.transcription import providers
from scriber.transcription.whisper_engine import WhisperEngine

log = logging.getLogger(__name__)

#: Display names used in logs and the Discord recording notice.
ENGINE_LABELS: dict[str, str] = {
    "whisper": "Whisper (local)",
    "voxtral": "Voxtral (Mistral AI)",
    "elevenlabs": "ElevenLabs Scribe",
    "google": "Google Speech-to-Text",
}


def effective_engine(cfg: config.Config) -> str:
    """The engine that will actually transcribe: the configured one if usable.

    An unknown or unconfigured cloud engine resolves to ``whisper`` — the same
    decision :class:`LiveTranscriber` makes per segment, exposed here so the
    recording notice announces what will really happen.
    """
    engine = cfg.transcribe_engine
    if engine == "whisper":
        return "whisper"
    provider = cfg.stt_providers.get(engine)
    if provider is None or engine not in providers._API_CALLERS or not provider.ready:
        return "whisper"
    return engine


class LiveTranscriber:
    """Routes per-segment transcription to the configured engine."""

    def __init__(self, whisper: WhisperEngine) -> None:
        self.whisper = whisper
        self._client: httpx.AsyncClient | None = None
        #: Conditions already warned about, to avoid one log line per segment.
        self._warned: set[str] = set()

    def _cloud_client(self) -> httpx.AsyncClient:
        """Shared HTTP client for cloud calls (recreated if ever closed)."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=providers.REQUEST_TIMEOUT)
        return self._client

    def _warn_once(self, key: str, message: str, *args) -> None:
        if key not in self._warned:
            self._warned.add(key)
            log.warning(message, *args)

    async def transcribe(self, pcm: bytes, language: str | None = None) -> str:
        """Transcribe one raw s16le stereo 48 kHz segment with the configured engine.

        ``language`` overrides the configured ``WHISPER_LANGUAGE`` for this
        call; ``None`` uses the configured value, ``"auto"`` forces detection.
        """
        cfg = config.get()
        engine = cfg.transcribe_engine
        if engine != "whisper":
            provider = cfg.stt_providers.get(engine)
            caller = providers._API_CALLERS.get(engine)
            if provider is None or caller is None or not provider.ready:
                self._warn_once(
                    f"unusable:{engine}",
                    "TRANSCRIBE_ENGINE=%r is unknown or not configured; "
                    "transcribing with local Whisper instead.",
                    engine,
                )
            else:
                mono, _diag = downmix_to_mono(pcm)
                segment = mono[::3]  # 48 kHz -> 16 kHz decimation
                if providers._too_short(segment):
                    return ""
                effective = cfg.whisper_language if language is None else language
                try:
                    return await caller(
                        self._cloud_client(), provider, provider.model, effective, segment
                    )
                except Exception as exc:
                    log.warning(
                        "%s live transcription failed (%s); falling back to "
                        "local Whisper for this segment.",
                        engine,
                        exc,
                    )
        return await self.whisper.transcribe(pcm, language=language)
