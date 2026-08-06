"""Provider-neutral meeting summarizer with failover.

Talks to Anthropic, OpenAI, or any OpenAI-compatible endpoint with raw
``httpx`` calls (no provider SDKs, by design). Providers are configured as an
ordered list and tried in order until one succeeds; only if every provider
fails is a :class:`SummaryError` raised. All configuration is read live via
``scriber.config.get()``.
"""

from __future__ import annotations

import logging
from urllib.parse import urlparse

import httpx

from scriber import config
from scriber.config import ProviderConfig

logger = logging.getLogger(__name__)

#: HTTP timeout for summary requests, in seconds.
REQUEST_TIMEOUT = 120.0

SYSTEM_PROMPT = (
    "You are an expert meeting-minutes writer. You receive the metadata and the "
    "full transcript of a voice meeting and produce a concise, well-structured "
    "summary in Markdown. Use exactly these level-2 headings, in this order:\n"
    "## Overview\n"
    "## Topics discussed\n"
    "## Decisions made\n"
    "## Blockers & open issues\n"
    "## Planning & action items\n"
    "## Questions postponed\n"
    "## Next steps\n"
    "Under \"Planning & action items\", state the owner together with each item "
    "whenever the owner is identifiable. Omit any section that would be empty. "
    "Keep speaker names exactly as they appear in the transcript. Reply in the "
    "dominant language of the transcript."
)


class SummaryError(Exception):
    """Raised when every configured summary provider fails."""


def _format_duration(seconds: object) -> str:
    """Render a duration in seconds as a short human-readable string."""
    try:
        total = int(float(seconds))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "unknown"
    hours, remainder = divmod(max(total, 0), 3600)
    minutes, secs = divmod(remainder, 60)
    parts: list[str] = []
    if hours:
        parts.append(f"{hours} h")
    if minutes:
        parts.append(f"{minutes} min")
    if secs or not parts:
        parts.append(f"{secs} s")
    return " ".join(parts)


def _build_user_prompt(
    transcript_text: str, meta: dict, participant_context: str = ""
) -> str:
    """Assemble the user prompt: metadata block, optional participant context, transcript.

    When ``participant_context`` is non-empty it is inserted, under an
    explanatory heading, BEFORE the transcript so the model can spell proper
    nouns / project names correctly without quoting the memory files verbatim.
    """
    participants = meta.get("participants") or []
    lines = [
        "Meeting metadata:",
        f"- Server: {meta.get('guild_name') or 'unknown'}",
        f"- Voice channel: {meta.get('voice_channel_name') or 'unknown'}",
        f"- Started at: {meta.get('started_at') or 'unknown'}",
        f"- Duration: {_format_duration(meta.get('duration_seconds'))}",
        f"- Participants: {', '.join(participants) if participants else 'unknown'}",
        "",
    ]
    if participant_context:
        lines.append(
            "Participant context (from their personal memory files — use to "
            "spell names/projects correctly; do not quote verbatim):\n"
            + participant_context
        )
        lines.append("")
    lines.extend(["Transcript:", transcript_text])
    return "\n".join(lines)


def _error_detail(response: httpx.Response) -> str:
    """Extract a short, readable error message from a provider response."""
    try:
        data = response.json()
    except ValueError:
        data = None
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict) and error.get("message"):
            return str(error["message"])
        if isinstance(error, str) and error:
            return error
    text = response.text.strip()
    return text[:500] if text else response.reason_phrase


class _ProviderError(Exception):
    """Internal: a single provider failed; the caller may fail over to the next."""


class Summarizer:
    """Summarizes meeting transcripts through the configured provider chain."""

    @staticmethod
    def _enabled_providers() -> list[ProviderConfig]:
        """Return the configured providers that are actually enabled, in order."""
        return [p for p in config.get().summary_providers if p.enabled]

    def targets(self) -> list[str]:
        """Return the target labels of the enabled providers, in failover order."""
        return [p.target_label() for p in self._enabled_providers()]

    def display_target(self) -> str:
        """Return the failover chain, e.g. ``api.anthropic.com / claude-opus-5``.

        With several providers configured, they are joined with an arrow in the
        order they are tried, e.g. ``… / claude-opus-5 → api.openai.com / gpt-4o``.
        """
        labels = self.targets()
        return " → ".join(labels) if labels else "no provider configured"

    async def complete(self, system_prompt: str, user_prompt: str) -> str:
        """Run the provider-failover loop for an arbitrary prompt pair.

        Tries each enabled provider in order, sharing a single
        ``httpx.AsyncClient``, and returns the first success. This is the common
        completion path reused by both meeting summaries and per-user memory
        updates, so its failover semantics are identical to the historical
        ``summarize`` loop.

        Raises:
            SummaryError: if no provider is configured, or every configured
                provider fails (the message lists each provider's failure).
        """
        providers = self._enabled_providers()
        if not providers:
            raise SummaryError(
                "No summary provider is configured. Set at least one "
                "SUMMARY_PROVIDER_1 / SUMMARY_API_KEY_1 / SUMMARY_MODEL_1 / "
                "SUMMARY_BASE_URL_1 block."
            )

        failures: list[str] = []
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            for provider in providers:
                label = provider.target_label()
                logger.info("Requesting summary from provider #%s (%s).", provider.index, label)
                try:
                    text = await self._summarize_one(
                        client, provider, system_prompt, user_prompt
                    )
                except _ProviderError as exc:
                    logger.warning(
                        "Summary provider #%s (%s) failed: %s; trying the next one.",
                        provider.index,
                        label,
                        exc,
                    )
                    failures.append(f"[#{provider.index} {label}] {exc}")
                    continue
                if len(providers) > 1:
                    logger.info("Summary produced by provider #%s (%s).", provider.index, label)
                return text

        raise SummaryError(
            f"All {len(providers)} configured summary provider(s) failed. "
            + " | ".join(failures)
        )

    async def summarize(
        self, transcript_text: str, meta: dict, participant_context: str = ""
    ) -> str:
        """Summarize the transcript, trying each provider until one succeeds.

        ``meta`` keys: ``guild_name``, ``voice_channel_name``, ``started_at``,
        ``duration_seconds``, ``participants`` (list of display names).

        When ``participant_context`` is non-empty it is injected before the
        transcript (see :func:`_build_user_prompt`) so the model spells proper
        nouns / project names correctly.

        Raises:
            SummaryError: if no provider is configured, or every configured
                provider fails.
        """
        user_prompt = _build_user_prompt(transcript_text, meta, participant_context)
        return await self.complete(SYSTEM_PROMPT, user_prompt)

    async def _summarize_one(
        self,
        client: httpx.AsyncClient,
        provider: ProviderConfig,
        system_prompt: str,
        user_prompt: str,
    ) -> str:
        """Call a single provider with ``system_prompt`` and return its text.

        Raises:
            _ProviderError: on any failure, so the caller can fail over.
        """
        base = provider.base_url.rstrip("/")
        kind = provider.provider

        if kind == "anthropic":
            url = f"{base}/v1/messages"
            headers = {
                "x-api-key": provider.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body: dict = {
                "model": provider.model,
                "max_tokens": 8192,
                "system": system_prompt,
                "messages": [{"role": "user", "content": user_prompt}],
            }
        elif kind in ("openai", "openai-compatible"):
            url = f"{base}/chat/completions"
            headers = {
                "Authorization": f"Bearer {provider.api_key}",
                "content-type": "application/json",
            }
            body = {
                "model": provider.model,
                "max_tokens": 8192,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
            }
        else:
            raise _ProviderError(
                f"unknown provider kind {kind!r} (expected 'anthropic', 'openai' "
                f"or 'openai-compatible')"
            )

        try:
            response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise _ProviderError(
                f"request to {urlparse(url).netloc} timed out after "
                f"{int(REQUEST_TIMEOUT)} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise _ProviderError(f"could not reach {urlparse(url).netloc}: {exc}") from exc

        if response.status_code >= 400:
            raise _ProviderError(f"HTTP {response.status_code}: {_error_detail(response)}")

        try:
            data = response.json()
        except ValueError as exc:
            raise _ProviderError("the response was not valid JSON") from exc

        text = self._extract_text(kind, data)
        if not text.strip():
            raise _ProviderError("the provider returned an empty response")
        return text.strip()

    @staticmethod
    def _extract_text(kind: str, data: object) -> str:
        """Pull the answer text out of a provider response payload."""
        try:
            if kind == "anthropic":
                blocks = data["content"]  # type: ignore[index]
                return "".join(
                    block.get("text", "")
                    for block in blocks
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            content = data["choices"][0]["message"]["content"]  # type: ignore[index]
            return content if isinstance(content, str) else ""
        except (KeyError, IndexError, TypeError) as exc:
            raise _ProviderError(
                "the provider response had an unexpected shape and could not be parsed"
            ) from exc
