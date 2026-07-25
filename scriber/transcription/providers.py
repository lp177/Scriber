"""Speech-to-text engines for transcript regeneration.

Regeneration re-transcribes a meeting's archived speech segments (see
:mod:`scriber.audio`) with a different engine than the live pipeline used:

- ``whisper`` — local faster-whisper with any model profile (tiny … large-v3,
  turbo). Nothing leaves the machine.
- ``voxtral`` — Mistral's Voxtral audio transcription API.
- ``elevenlabs`` — ElevenLabs Scribe speech-to-text API.
- ``google`` — Google Cloud Speech-to-Text v2 (Chirp models).

Like the summarizer, the cloud engines use raw ``httpx`` (no provider SDKs by
design). Each archived segment is transcribed separately so the rebuilt
transcript keeps per-speaker attribution.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from typing import Awaitable, Callable

import httpx
import numpy as np

from scriber import config
from scriber.audio import pcm16_to_wav_bytes
from scriber.config import SttProviderConfig

log = logging.getLogger(__name__)

#: HTTP timeout per segment request, in seconds.
REQUEST_TIMEOUT = 120.0
#: Concurrent in-flight requests against a cloud STT API.
API_CONCURRENCY = 4
#: Segments shorter than this many seconds are skipped (mirrors the live pipeline).
MIN_SEGMENT_SECONDS = 0.4
#: Sample rate of the decoded archive segments handed to the engines.
SAMPLE_RATE = 16_000

#: Common local Whisper model profiles offered in the dashboard. Any other
#: faster-whisper model name typed by the user works too.
WHISPER_PROFILES: tuple[str, ...] = (
    "tiny",
    "base",
    "small",
    "medium",
    "large-v3",
    "large-v3-turbo",
    "distil-large-v3",
)


class TranscriptionProviderError(Exception):
    """Raised when a regeneration engine fails."""


def engine_catalog() -> list[dict]:
    """Describe the available regeneration engines and their configured state.

    ``ready`` says whether the engine can be used right now; a cloud engine
    becomes ready once its API key (and, for Google, the project id) is set in
    the settings. ``models`` are suggestions — a custom model id is accepted.
    """
    cfg = config.get()
    voxtral = cfg.stt_providers["voxtral"]
    elevenlabs = cfg.stt_providers["elevenlabs"]
    google = cfg.stt_providers["google"]
    return [
        {
            "id": "whisper",
            "label": "Whisper (local)",
            "ready": True,
            "requires": None,
            "default_model": cfg.whisper_model,
            "models": list(WHISPER_PROFILES),
        },
        {
            "id": "voxtral",
            "label": "Voxtral (Mistral)",
            "ready": voxtral.ready,
            "requires": "VOXTRAL_API_KEY",
            "default_model": voxtral.model or "voxtral-mini-latest",
            "models": ["voxtral-mini-latest", "voxtral-small-latest"],
        },
        {
            "id": "elevenlabs",
            "label": "ElevenLabs Scribe",
            "ready": elevenlabs.ready,
            "requires": "ELEVENLABS_API_KEY",
            "default_model": elevenlabs.model or "scribe_v2",
            "models": ["scribe_v2", "scribe_v1"],
        },
        {
            "id": "google",
            "label": "Google Chirp",
            "ready": google.ready,
            "requires": "GOOGLE_SPEECH_API_KEY + GOOGLE_SPEECH_PROJECT",
            "default_model": google.model or "chirp_3",
            "models": ["chirp_3", "chirp_2", "latest_long"],
        },
    ]


def engine_label(engine: str, model: str) -> str:
    """Human-readable version label, e.g. ``Whisper (large-v3)``."""
    names = {entry["id"]: entry["label"] for entry in engine_catalog()}
    return f"{names.get(engine, engine)} ({model})"


def _too_short(segment: np.ndarray) -> bool:
    return segment.size < int(SAMPLE_RATE * MIN_SEGMENT_SECONDS)


async def transcribe_segments(
    engine: str,
    model: str,
    language: str,
    segments: list[np.ndarray],
    progress: Callable[[], None],
) -> list[str]:
    """Transcribe every 16 kHz mono int16 segment with the chosen engine.

    Returns one text per segment (empty for skipped/silent ones), calling
    ``progress()`` after each finished segment. Raises
    :class:`TranscriptionProviderError` when the engine fails.
    """
    if engine == "whisper":
        return await _whisper_segments(model, language, segments, progress)
    if engine in ("voxtral", "elevenlabs", "google"):
        provider = config.get().stt_providers[engine]
        if not provider.ready:
            raise TranscriptionProviderError(
                f"The {engine} engine is not configured — set its API key in Settings."
            )
        return await _api_segments(engine, provider, model, language, segments, progress)
    raise TranscriptionProviderError(f"Unknown transcription engine {engine!r}")


# ------------------------------- local whisper -------------------------------


async def _whisper_segments(
    model: str,
    language: str,
    segments: list[np.ndarray],
    progress: Callable[[], None],
) -> list[str]:
    """Run a dedicated faster-whisper instance over the segments.

    A separate model instance is loaded so the live engine's model (used by an
    ongoing recording) is left untouched; it is released when the job ends.
    """
    from faster_whisper import WhisperModel  # deferred: heavy import

    cfg = config.get()
    lang = None if language in ("", "auto") else language

    def _load() -> WhisperModel:
        return WhisperModel(
            model,
            device=cfg.whisper_device,
            compute_type=cfg.whisper_compute_type,
            download_root=str(cfg.data_dir / "models"),
        )

    def _one(instance: WhisperModel, segment: np.ndarray) -> str:
        audio = segment.astype(np.float32) / 32768.0
        pieces, _info = instance.transcribe(audio, language=lang, vad_filter=True)
        return " ".join(piece.text.strip() for piece in pieces).strip()

    try:
        instance = await asyncio.to_thread(_load)
    except Exception as exc:
        raise TranscriptionProviderError(f"Could not load Whisper model {model!r}: {exc}") from exc
    try:
        texts: list[str] = []
        for segment in segments:
            if _too_short(segment):
                texts.append("")
            else:
                try:
                    texts.append(await asyncio.to_thread(_one, instance, segment))
                except Exception as exc:
                    raise TranscriptionProviderError(
                        f"Whisper transcription failed: {exc}"
                    ) from exc
            progress()
        return texts
    finally:
        del instance


# -------------------------------- cloud engines ------------------------------


async def _api_segments(
    engine: str,
    provider: SttProviderConfig,
    model: str,
    language: str,
    segments: list[np.ndarray],
    progress: Callable[[], None],
) -> list[str]:
    """Fan the segments out to a cloud STT API with bounded concurrency.

    Each segment gets one retry (transient network hiccups); a second failure
    aborts the whole job — cloud STT failures are usually systemic (bad key,
    quota), and a half-transcribed meeting would be misleading.
    """
    semaphore = asyncio.Semaphore(API_CONCURRENCY)
    caller = _API_CALLERS[engine]
    # Once a segment fails permanently, queued segments short-circuit instead
    # of firing more doomed requests (cloud failures are usually systemic).
    aborted = asyncio.Event()

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:

        async def one(segment: np.ndarray) -> str:
            if _too_short(segment):
                progress()
                return ""
            async with semaphore:
                if aborted.is_set():
                    raise TranscriptionProviderError(
                        f"{engine} transcription aborted after an earlier failure"
                    )
                try:
                    try:
                        text = await caller(client, provider, model, language, segment)
                    except TranscriptionProviderError:
                        raise
                    except Exception as first:
                        log.warning("%s segment failed (%s); retrying once.", engine, first)
                        try:
                            text = await caller(client, provider, model, language, segment)
                        except Exception as exc:
                            raise TranscriptionProviderError(
                                f"{engine} transcription failed: {exc}"
                            ) from exc
                except TranscriptionProviderError:
                    aborted.set()
                    raise
            progress()
            return text

        # return_exceptions keeps the client open until every in-flight task
        # settles (no retries against a closed client); the first real error
        # is then re-raised to fail the job.
        results = await asyncio.gather(
            *(one(segment) for segment in segments), return_exceptions=True
        )

    failure = next(
        (
            r
            for r in results
            if isinstance(r, TranscriptionProviderError)
            and "aborted after an earlier failure" not in str(r)
        ),
        next((r for r in results if isinstance(r, BaseException)), None),
    )
    if failure is not None:
        if isinstance(failure, TranscriptionProviderError):
            raise failure
        raise TranscriptionProviderError(f"{engine} transcription failed: {failure}")
    return [r if isinstance(r, str) else "" for r in results]


def _http_detail(response: httpx.Response) -> str:
    """Short, readable error out of a provider HTTP response."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error") or data.get("detail") or data.get("message")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    text = response.text.strip()
    return text[:300] if text else response.reason_phrase


def _raise_for_status(engine: str, response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise TranscriptionProviderError(
            f"{engine} API returned HTTP {response.status_code}: {_http_detail(response)}"
        )


async def _voxtral_one(
    client: httpx.AsyncClient,
    provider: SttProviderConfig,
    model: str,
    language: str,
    segment: np.ndarray,
) -> str:
    """Mistral audio transcription: multipart upload, OpenAI-style response."""
    base = (provider.base_url or "https://api.mistral.ai").rstrip("/")
    data: dict[str, str] = {"model": model}
    if language not in ("", "auto"):
        data["language"] = language
    response = await client.post(
        f"{base}/v1/audio/transcriptions",
        # Mistral documents x-api-key for the audio endpoint while the rest of
        # the platform uses a Bearer header — send both, either one is enough.
        headers={
            "x-api-key": provider.api_key,
            "Authorization": f"Bearer {provider.api_key}",
        },
        data=data,
        files={"file": ("segment.wav", pcm16_to_wav_bytes(segment), "audio/wav")},
    )
    _raise_for_status("voxtral", response)
    text = response.json().get("text", "")
    return text.strip() if isinstance(text, str) else ""


async def _elevenlabs_one(
    client: httpx.AsyncClient,
    provider: SttProviderConfig,
    model: str,
    language: str,
    segment: np.ndarray,
) -> str:
    """ElevenLabs Scribe speech-to-text: multipart upload with xi-api-key."""
    base = (provider.base_url or "https://api.elevenlabs.io").rstrip("/")
    data: dict[str, str] = {"model_id": model}
    if language not in ("", "auto"):
        data["language_code"] = language
    response = await client.post(
        f"{base}/v1/speech-to-text",
        headers={"xi-api-key": provider.api_key},
        data=data,
        files={"file": ("segment.wav", pcm16_to_wav_bytes(segment), "audio/wav")},
    )
    _raise_for_status("elevenlabs", response)
    text = response.json().get("text", "")
    return text.strip() if isinstance(text, str) else ""


async def _google_one(
    client: httpx.AsyncClient,
    provider: SttProviderConfig,
    model: str,
    language: str,
    segment: np.ndarray,
) -> str:
    """Google Speech-to-Text v2 inline recognition (Chirp models).

    Uses the regional endpoint derived from ``GOOGLE_SPEECH_LOCATION`` and the
    implicit ``_`` recognizer, authenticated with an API key. Chirp models
    accept ``auto`` for language detection; otherwise pass a BCP-47 code
    (e.g. ``fr-FR``).
    """
    location = provider.location or "eu"
    host = (
        "speech.googleapis.com"
        if location == "global"
        else f"{location}-speech.googleapis.com"
    )
    url = (
        f"https://{host}/v2/projects/{provider.project}/locations/{location}"
        f"/recognizers/_:recognize"
    )
    language_code = "auto" if language in ("", "auto") else language
    response = await client.post(
        url,
        # The key goes in a header, NOT a ?key= query param: httpx logs every
        # request URL at INFO level, so a query param would leak the secret
        # into the process/container logs once per segment.
        headers={"x-goog-api-key": provider.api_key},
        json={
            "config": {
                "autoDecodingConfig": {},
                "model": model,
                "languageCodes": [language_code],
            },
            "content": base64.b64encode(pcm16_to_wav_bytes(segment)).decode("ascii"),
        },
    )
    _raise_for_status("google", response)
    results = response.json().get("results") or []
    parts: list[str] = []
    for result in results:
        alternatives = result.get("alternatives") or []
        if alternatives and isinstance(alternatives[0], dict):
            transcript = alternatives[0].get("transcript", "")
            if isinstance(transcript, str) and transcript.strip():
                parts.append(transcript.strip())
    return " ".join(parts)


_API_CALLERS: dict[
    str,
    Callable[
        [httpx.AsyncClient, SttProviderConfig, str, str, np.ndarray], Awaitable[str]
    ],
] = {
    "voxtral": _voxtral_one,
    "elevenlabs": _elevenlabs_one,
    "google": _google_one,
}
