"""Per-model list prices and the cost estimate shown in the dashboard.

Every provider call records what it consumed (``api_usage`` table); this module
turns those token counts into an **estimated** USD cost from a table of list
prices (USD per million tokens). The built-in table covers the Anthropic
models and the common OpenAI ones; the ``MODEL_PRICING`` setting (a JSON
object) overrides or extends it, e.g. to price a self-hosted model at zero or
to follow a price change without upgrading::

    MODEL_PRICING={"llama3.1:8b": {"input": 0, "output": 0},
                   "gpt-4o": {"input": 2.5, "output": 10, "cache_read": 1.25}}

Models are matched by **longest prefix** on the model id, so dated snapshots
(``claude-sonnet-4-5-20250929``) and family variants (``gpt-4o-mini`` vs
``gpt-4o``) resolve correctly. An unknown model yields ``None`` — the dashboard
then shows the token counts without a dollar figure.
"""

from __future__ import annotations

import dataclasses
import json
import logging

from scriber import config

log = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class Price:
    """List prices in USD per million tokens for one model (prefix)."""

    input: float
    output: float
    #: Cached-prompt read rate; ``None`` = 10% of ``input`` (the Anthropic default).
    cache_read: float | None = None
    #: Cache write rate; ``None`` = 125% of ``input`` (the Anthropic 5-minute rate).
    cache_write: float | None = None
    #: Some models have a second rate card for long prompts (Claude Haiku 5.5
    #: above 100K prompt tokens): applied when the prompt exceeds the threshold.
    long_prompt_threshold: int | None = None
    long_prompt: Price | None = None

    def cache_read_rate(self) -> float:
        """Effective cache-read price per million tokens."""
        return self.input * 0.1 if self.cache_read is None else self.cache_read

    def cache_write_rate(self) -> float:
        """Effective cache-write price per million tokens."""
        return self.input * 1.25 if self.cache_write is None else self.cache_write


# Anthropic list prices (first-party API). Keep the keys lowercase.
_ANTHROPIC: dict[str, Price] = {
    "claude-fable-5-1": Price(10.0, 50.0, cache_read=0.25, cache_write=12.5),
    "claude-mythos-5-1": Price(10.0, 50.0, cache_read=0.25, cache_write=12.5),
    "claude-fable-5": Price(10.0, 50.0, cache_read=1.0, cache_write=12.5),
    "claude-opus-5-5": Price(4.0, 20.0, cache_read=0.20, cache_write=5.0),
    "claude-opus-5": Price(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-8": Price(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-7": Price(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-6": Price(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-5": Price(5.0, 25.0, cache_read=0.50, cache_write=6.25),
    "claude-opus-4-1": Price(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-opus-4": Price(15.0, 75.0, cache_read=1.50, cache_write=18.75),
    "claude-sonnet-5-5": Price(2.0, 10.0, cache_read=0.20, cache_write=2.5),
    "claude-sonnet-5": Price(2.0, 10.0, cache_read=0.20, cache_write=2.5),
    "claude-sonnet-4-6": Price(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4-5": Price(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-sonnet-4": Price(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-3-7-sonnet": Price(3.0, 15.0, cache_read=0.30, cache_write=3.75),
    "claude-haiku-5-5": Price(
        0.10,
        0.50,
        cache_read=0.01,
        cache_write=0.125,
        long_prompt_threshold=100_000,
        long_prompt=Price(0.50, 2.50, cache_read=0.05, cache_write=0.625),
    ),
    "claude-haiku-4-5": Price(1.0, 5.0, cache_read=0.10, cache_write=1.25),
    "claude-3-5-haiku": Price(0.80, 4.0, cache_read=0.08, cache_write=1.0),
}

# OpenAI list prices as last checked (2026-10). OpenAI does not bill cache
# writes. Override with MODEL_PRICING if they change.
_OPENAI: dict[str, Price] = {
    "gpt-4o-mini": Price(0.15, 0.60, cache_read=0.075, cache_write=0.0),
    "gpt-4o": Price(2.50, 10.0, cache_read=1.25, cache_write=0.0),
    "gpt-4.1-nano": Price(0.10, 0.40, cache_read=0.025, cache_write=0.0),
    "gpt-4.1-mini": Price(0.40, 1.60, cache_read=0.10, cache_write=0.0),
    "gpt-4.1": Price(2.0, 8.0, cache_read=0.50, cache_write=0.0),
    "gpt-5-nano": Price(0.05, 0.40, cache_read=0.005, cache_write=0.0),
    "gpt-5-mini": Price(0.25, 2.0, cache_read=0.025, cache_write=0.0),
    "gpt-5": Price(1.25, 10.0, cache_read=0.125, cache_write=0.0),
    "o3-mini": Price(1.10, 4.40, cache_read=0.55, cache_write=0.0),
    "o3": Price(2.0, 8.0, cache_read=0.50, cache_write=0.0),
    "o4-mini": Price(1.10, 4.40, cache_read=0.275, cache_write=0.0),
}

BUILTIN_PRICES: dict[str, Price] = {**_ANTHROPIC, **_OPENAI}

# Cache of the parsed MODEL_PRICING override, keyed by its raw JSON text so a
# settings change is picked up on the next call without re-parsing every time.
_override_cache: tuple[str, dict[str, Price]] = ("", {})


def _parse_overrides(raw: str) -> dict[str, Price]:
    """Parse the ``MODEL_PRICING`` JSON object; invalid input logs and yields {}."""
    global _override_cache
    if raw == _override_cache[0]:
        return _override_cache[1]
    parsed: dict[str, Price] = {}
    if raw.strip():
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object keyed by model id")
            for model, spec in data.items():
                if not isinstance(spec, dict):
                    raise ValueError(f"entry for {model!r} must be an object")
                parsed[str(model).strip().lower()] = Price(
                    input=float(spec["input"]),
                    output=float(spec["output"]),
                    cache_read=(
                        float(spec["cache_read"]) if spec.get("cache_read") is not None else None
                    ),
                    cache_write=(
                        float(spec["cache_write"]) if spec.get("cache_write") is not None else None
                    ),
                )
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("MODEL_PRICING is not a valid price table (%s); ignoring it.", exc)
            parsed = {}
    _override_cache = (raw, parsed)
    return parsed


def _normalize(model: str) -> str:
    """Lowercase the id and drop a vendor prefix such as Bedrock's ``anthropic.``."""
    name = (model or "").strip().lower()
    if name.startswith("anthropic."):
        name = name[len("anthropic.") :]
    return name


def lookup(model: str) -> Price | None:
    """Return the price entry whose key is the longest prefix of ``model``, or None."""
    name = _normalize(model)
    if not name:
        return None
    try:
        overrides = _parse_overrides(config.get().model_pricing)
    except RuntimeError:  # config not initialized (tests / tooling)
        overrides = {}
    table = {**BUILTIN_PRICES, **overrides}
    best: tuple[int, Price] | None = None
    for prefix, price in table.items():
        if name.startswith(prefix) and (best is None or len(prefix) > best[0]):
            best = (len(prefix), price)
    return best[1] if best else None


def estimate_cost(
    model: str,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> float | None:
    """Estimated USD cost of one request, or ``None`` when the model is unknown."""
    price = lookup(model)
    if price is None:
        return None
    prompt_tokens = input_tokens + cache_read_tokens + cache_write_tokens
    if (
        price.long_prompt is not None
        and price.long_prompt_threshold is not None
        and prompt_tokens > price.long_prompt_threshold
    ):
        price = price.long_prompt
    total = (
        input_tokens * price.input
        + output_tokens * price.output
        + cache_read_tokens * price.cache_read_rate()
        + cache_write_tokens * price.cache_write_rate()
    )
    return total / 1_000_000
