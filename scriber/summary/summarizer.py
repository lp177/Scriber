"""Provider-neutral meeting summarizer with failover.

Talks to Anthropic, OpenAI, or any OpenAI-compatible endpoint with raw
``httpx`` calls (no provider SDKs, by design). Providers are configured as an
ordered list and tried in order until one succeeds; only if every provider
fails is a :class:`SummaryError` raised. All configuration is read live via
``scriber.config.get()``.

Two *tasks* share the failover loop: ``"summary"`` (the meeting minutes, using
each provider's ``SUMMARY_MODEL_<n>``) and ``"memory"`` (the per-participant
memory refresh, using the cheaper ``SUMMARY_MEMORY_MODEL_<n>``). Every success
is returned as a :class:`Completion` carrying the text **and** the token usage
reported by the provider, plus an estimated cost (see
:mod:`scriber.summary.pricing`), so callers can record what a meeting cost.
"""

from __future__ import annotations

import dataclasses
import logging
from urllib.parse import urlparse

import httpx

from scriber import config
from scriber.config import ProviderConfig
from scriber.summary import pricing

logger = logging.getLogger(__name__)

#: Overall HTTP timeout for one provider request, in seconds. Generous on
#: purpose: with adaptive thinking a long transcript legitimately takes a few
#: minutes, and a request abandoned by the client is billed anyway. The
#: connect timeout stays short so an unreachable host fails over quickly.
REQUEST_TIMEOUT = 600.0
CONNECT_TIMEOUT = 15.0

#: Output-token ceiling per provider kind. On Anthropic models the (hidden)
#: thinking tokens count against it, so it is sized far above any plausible
#: minutes to make a cut-off summary impossible in practice. The OpenAI figure
#: stays under gpt-4o's 16,384 completion cap; self-hosted OpenAI-compatible
#: servers often reject requests exceeding their context window, hence the
#: smaller figure there. A truncated response is treated as a failure.
MAX_OUTPUT_TOKENS: dict[str, int] = {
    "anthropic": 32000,
    "openai": 16000,
    "openai-compatible": 8192,
}

#: ``output_config.effort`` per task (Anthropic only). Meeting minutes are
#: knowledge work where ``medium`` matches ``high`` at a lower cost; the memory
#: refresh is a short structured rewrite that ``low`` handles.
EFFORT_BY_TASK: dict[str, str] = {"summary": "medium", "memory": "low"}

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


@dataclasses.dataclass
class Completion:
    """A successful provider response together with what it consumed.

    Token counts come straight from the provider's ``usage`` object (zero when
    it reports none). ``cost_usd`` is the list-price estimate from
    :mod:`scriber.summary.pricing`, or ``None`` for an unknown model.
    """

    text: str
    task: str
    provider_index: int
    provider_kind: str
    model: str
    host: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float | None = None

    def usage_fields(self) -> dict:
        """Keyword arguments for :func:`scriber.database.record_usage`."""
        return {
            "provider": f"{self.provider_kind} #{self.provider_index} ({self.host})",
            "model": self.model,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "cost_usd": self.cost_usd,
        }

    def usage_label(self) -> str:
        """Short human-readable usage for the meeting log, e.g. ``12,345 in / 1,234 out tokens, ~$0.16``."""
        parts = [f"{self.input_tokens:,} in / {self.output_tokens:,} out tokens"]
        if self.cache_read_tokens:
            parts.append(f"{self.cache_read_tokens:,} cached")
        label = ", ".join(parts)
        if self.cost_usd is not None:
            label += f", ~${self.cost_usd:.4f}" if self.cost_usd < 0.01 else f", ~${self.cost_usd:.2f}"
        return label


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


def _mentions_generation_controls(detail: str) -> bool:
    """True when a 400 complains about ``thinking`` / ``output_config`` (older models)."""
    lowered = detail.lower()
    return any(token in lowered for token in ("thinking", "effort", "output_config"))


def _as_int(value: object) -> int:
    """Coerce a usage counter to a non-negative int (missing/invalid -> 0)."""
    try:
        return max(0, int(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


class _ProviderError(Exception):
    """Internal: a single provider failed; the caller may fail over to the next."""


class Summarizer:
    """Summarizes meeting transcripts through the configured provider chain."""

    @staticmethod
    def _enabled_providers() -> list[ProviderConfig]:
        """Return the configured providers that are actually enabled, in order."""
        return [p for p in config.get().summary_providers if p.enabled]

    def targets(self, task: str = "summary") -> list[str]:
        """Return the target labels of the enabled providers, in failover order."""
        return [p.target_label(task) for p in self._enabled_providers()]

    def display_target(self, task: str = "summary") -> str:
        """Return the failover chain, e.g. ``api.anthropic.com / claude-opus-5-5``.

        With several providers configured, they are joined with an arrow in the
        order they are tried, e.g. ``… / claude-opus-5-5 → api.openai.com / gpt-4o``.
        """
        labels = self.targets(task)
        return " → ".join(labels) if labels else "no provider configured"

    async def complete(
        self, system_prompt: str, user_prompt: str, *, task: str = "summary"
    ) -> Completion:
        """Run the provider-failover loop for an arbitrary prompt pair.

        Tries each enabled provider in order, sharing a single
        ``httpx.AsyncClient``, and returns the first success. ``task`` selects
        the model (``"summary"`` -> ``SUMMARY_MODEL_<n>``, ``"memory"`` ->
        ``SUMMARY_MEMORY_MODEL_<n>``) and the effort level. This is the common
        completion path reused by both meeting summaries and per-user memory
        updates, so its failover semantics are identical for both.

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
        timeout = httpx.Timeout(REQUEST_TIMEOUT, connect=CONNECT_TIMEOUT)
        async with httpx.AsyncClient(timeout=timeout) as client:
            for provider in providers:
                label = provider.target_label(task)
                logger.info(
                    "Requesting %s from provider #%s (%s).", task, provider.index, label
                )
                try:
                    completion = await self._summarize_one(
                        client, provider, system_prompt, user_prompt, task
                    )
                except _ProviderError as exc:
                    logger.warning(
                        "Provider #%s (%s) failed for %s: %s; trying the next one.",
                        provider.index,
                        label,
                        task,
                        exc,
                    )
                    failures.append(f"[#{provider.index} {label}] {exc}")
                    continue
                logger.info(
                    "%s produced by provider #%s (%s): %s.",
                    task.capitalize(),
                    provider.index,
                    label,
                    completion.usage_label(),
                )
                return completion

        raise SummaryError(
            f"All {len(providers)} configured summary provider(s) failed. "
            + " | ".join(failures)
        )

    async def summarize(
        self, transcript_text: str, meta: dict, participant_context: str = ""
    ) -> Completion:
        """Summarize the transcript, trying each provider until one succeeds.

        ``meta`` keys: ``guild_name``, ``voice_channel_name``, ``started_at``,
        ``duration_seconds``, ``participants`` (list of display names).

        When ``participant_context`` is non-empty it is injected before the
        transcript (see :func:`_build_user_prompt`) so the model spells proper
        nouns / project names correctly.

        Returns:
            The :class:`Completion`; its ``text`` is the Markdown minutes.

        Raises:
            SummaryError: if no provider is configured, or every configured
                provider fails.
        """
        user_prompt = _build_user_prompt(transcript_text, meta, participant_context)
        return await self.complete(SYSTEM_PROMPT, user_prompt, task="summary")

    async def _summarize_one(
        self,
        client: httpx.AsyncClient,
        provider: ProviderConfig,
        system_prompt: str,
        user_prompt: str,
        task: str,
    ) -> Completion:
        """Call a single provider with ``system_prompt`` and return its completion.

        Raises:
            _ProviderError: on any failure, so the caller can fail over.
        """
        base = provider.base_url.rstrip("/")
        kind = provider.provider
        model = provider.model_for(task)
        if not model:
            raise _ProviderError("no model configured for this provider")
        max_tokens = MAX_OUTPUT_TOKENS.get(kind, MAX_OUTPUT_TOKENS["openai-compatible"])

        if kind == "anthropic":
            url = f"{base}/v1/messages"
            headers = {
                "x-api-key": provider.api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
            body: dict = {
                "model": model,
                "max_tokens": max_tokens,
                # Adaptive thinking (the only mode on current models) with an
                # explicit effort: the hidden reasoning is what drives cost, and
                # the API default differs between model generations.
                "thinking": {"type": "adaptive"},
                "output_config": {"effort": EFFORT_BY_TASK.get(task, "medium")},
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
                "model": model,
                "max_tokens": max_tokens,
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

        response = await self._post(client, url, headers, body)
        if (
            kind == "anthropic"
            and response.status_code == 400
            and _mentions_generation_controls(_error_detail(response))
        ):
            # Models older than the adaptive-thinking generation reject the
            # thinking / effort controls: retry once without them rather than
            # failing over to another provider.
            logger.info(
                "%s rejected the thinking/effort controls; retrying without them.", model
            )
            body = {k: v for k, v in body.items() if k not in ("thinking", "output_config")}
            response = await self._post(client, url, headers, body)

        if response.status_code >= 400:
            raise _ProviderError(f"HTTP {response.status_code}: {_error_detail(response)}")

        try:
            data = response.json()
        except ValueError as exc:
            raise _ProviderError("the response was not valid JSON") from exc

        self._check_stop_reason(kind, data, max_tokens)
        text = self._extract_text(kind, data)
        if not text.strip():
            raise _ProviderError("the provider returned an empty response")

        usage = self._extract_usage(kind, data)
        return Completion(
            text=text.strip(),
            task=task,
            provider_index=provider.index,
            provider_kind=kind,
            model=model,
            host=urlparse(url).netloc,
            input_tokens=usage["input"],
            output_tokens=usage["output"],
            cache_read_tokens=usage["cache_read"],
            cache_write_tokens=usage["cache_write"],
            cost_usd=pricing.estimate_cost(
                model,
                usage["input"],
                usage["output"],
                usage["cache_read"],
                usage["cache_write"],
            ),
        )

    @staticmethod
    async def _post(
        client: httpx.AsyncClient, url: str, headers: dict, body: dict
    ) -> httpx.Response:
        """POST ``body`` and return the response; network failures become provider errors."""
        try:
            return await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise _ProviderError(
                f"request to {urlparse(url).netloc} timed out after "
                f"{int(REQUEST_TIMEOUT)} seconds"
            ) from exc
        except httpx.HTTPError as exc:
            raise _ProviderError(f"could not reach {urlparse(url).netloc}: {exc}") from exc

    @staticmethod
    def _check_stop_reason(kind: str, data: object, max_tokens: int) -> None:
        """Fail on a truncated or declined response instead of accepting partial text.

        A response cut off at ``max_tokens`` would otherwise be stored as if it
        were the complete minutes; a safety refusal would surface as an
        unhelpful "empty response". Both fail over to the next provider.
        """
        if not isinstance(data, dict):
            return
        if kind == "anthropic":
            reason = data.get("stop_reason")
            if reason == "max_tokens":
                raise _ProviderError(
                    f"the response was cut off at the {max_tokens:,}-token output limit"
                )
            if reason == "refusal":
                details = data.get("stop_details")
                category = details.get("category") if isinstance(details, dict) else None
                raise _ProviderError(
                    "the provider declined to answer"
                    + (f" (category: {category})" if category else "")
                )
            return
        try:
            choice = data["choices"][0]
        except (KeyError, IndexError, TypeError):
            return
        if not isinstance(choice, dict):
            return
        reason = choice.get("finish_reason")
        if reason == "length":
            raise _ProviderError(
                f"the response was cut off at the {max_tokens:,}-token output limit"
            )
        if reason == "content_filter":
            raise _ProviderError("the provider declined to answer (content filter)")
        message = choice.get("message")
        if isinstance(message, dict) and message.get("refusal") and not message.get("content"):
            raise _ProviderError(f"the provider declined to answer: {message['refusal']}")

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

    @staticmethod
    def _extract_usage(kind: str, data: object) -> dict[str, int]:
        """Read the provider's token counters; anything missing counts as zero.

        Anthropic reports uncached input, output, cache reads and cache writes
        separately. OpenAI's ``prompt_tokens`` *includes* the cached tokens
        (``prompt_tokens_details.cached_tokens``), so they are split out here
        to keep the same four-field shape.
        """
        zero = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
        usage = data.get("usage") if isinstance(data, dict) else None
        if not isinstance(usage, dict):
            return zero
        if kind == "anthropic":
            return {
                "input": _as_int(usage.get("input_tokens")),
                "output": _as_int(usage.get("output_tokens")),
                "cache_read": _as_int(usage.get("cache_read_input_tokens")),
                "cache_write": _as_int(usage.get("cache_creation_input_tokens")),
            }
        prompt = _as_int(usage.get("prompt_tokens"))
        details = usage.get("prompt_tokens_details")
        cached = min(prompt, _as_int(details.get("cached_tokens")) if isinstance(details, dict) else 0)
        return {
            "input": prompt - cached,
            "output": _as_int(usage.get("completion_tokens")),
            "cache_read": cached,
            "cache_write": 0,
        }
