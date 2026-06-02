"""Model pricing estimation with OpenRouter-backed cache."""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR

CACHE_FILE = CONFIG_DIR / "pricing_cache.json"
CACHE_MAX_AGE_SECONDS = 7 * 24 * 3600  # 7 days
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Trimmed built-in fallback for offline/first-run use.
# Values are (input $/1M tokens, output $/1M tokens).
# Synced from OpenRouter April 2026. Overridden by cache after first refresh.
_BUILTIN_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4": (15.00, 75.00),
    "claude-sonnet-4": (3.00, 15.00),
    "claude-haiku-4": (1.00, 5.00),
    "gpt-4.1": (2.00, 8.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4o": (2.50, 10.00),
    "o3": (2.00, 8.00),
    "o4-mini": (1.10, 4.40),
    "gemini-2.5-pro": (1.25, 10.00),
    "gemini-2.5-flash": (0.30, 2.50),
    "deepseek-r1": (0.70, 2.50),
    "deepseek-v3": (0.27, 1.10),
}

# Lazy-loaded pricing state
_pricing_table: dict[str, tuple[float, float]] | None = None
_sorted_prefixes: list[str] | None = None


def _normalize_model_name(model: str) -> str:
    """Normalize model string for prefix matching.

    Strips provider prefixes like 'anthropic/', 'openai/', 'google/',
    converts to lowercase, and removes date suffixes.
    """
    normalized = model.lower().strip()
    # Strip provider prefix (e.g. "anthropic/claude-sonnet-4-6" -> "claude-sonnet-4-6")
    if "/" in normalized:
        normalized = normalized.rsplit("/", 1)[-1]
    return normalized


def _load_cache() -> dict[str, tuple[float, float]] | None:
    """Load pricing cache from disk. Returns None on any error."""
    try:
        if not CACHE_FILE.exists():
            return None
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("version") != 1:
            return None
        models = data.get("models", {})
        if not isinstance(models, dict):
            return None
        return {
            k: (float(v[0]), float(v[1]))
            for k, v in models.items()
            if isinstance(v, (list, tuple)) and len(v) >= 2
        }
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return None


def _save_cache(models: dict[str, tuple[float, float]]) -> None:
    """Atomically write pricing cache to disk."""
    data = {
        "fetched_at": time.time(),
        "version": 1,
        "models": {k: list(v) for k, v in models.items()},
    }
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp_path, CACHE_FILE)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        print(f"Warning: could not save pricing cache: {e}", file=sys.stderr)


def _cache_is_stale() -> bool:
    """Check if the pricing cache is missing or older than CACHE_MAX_AGE_SECONDS."""
    try:
        if not CACHE_FILE.exists():
            return True
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        fetched_at = data.get("fetched_at", 0)
        return (time.time() - fetched_at) > CACHE_MAX_AGE_SECONDS
    except (OSError, json.JSONDecodeError, ValueError, TypeError):
        return True


def _fetch_openrouter() -> dict[str, tuple[float, float]]:
    """Fetch model pricing from the OpenRouter public API.

    Returns a dict mapping normalized model names to (input_per_1M, output_per_1M).
    Raises on network errors.
    """
    req = urllib.request.Request(
        OPENROUTER_MODELS_URL,
        headers={"User-Agent": "clawjournal"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = json.loads(resp.read())

    models: dict[str, tuple[float, float]] = {}
    for entry in raw.get("data", []):
        try:
            model_id = entry.get("id", "")
            pricing = entry.get("pricing", {})
            if not isinstance(pricing, dict):
                continue
            prompt_str = pricing.get("prompt")
            completion_str = pricing.get("completion")
            if not prompt_str or not completion_str:
                continue
            input_per_token = float(prompt_str)
            output_per_token = float(completion_str)
            # Skip free models (cost 0)
            if input_per_token <= 0 and output_per_token <= 0:
                continue
            # Convert per-token to per-1M tokens
            input_per_1m = input_per_token * 1_000_000
            output_per_1m = output_per_token * 1_000_000
            # Normalize the key
            key = _normalize_model_name(model_id)
            if key and key not in models:
                models[key] = (round(input_per_1m, 4), round(output_per_1m, 4))
        except (ValueError, TypeError, KeyError):
            continue

    return models


def refresh_pricing(quiet: bool = False) -> bool:
    """Fetch pricing from OpenRouter and save to local cache.

    Returns True on success, False on network failure.
    """
    global _pricing_table
    try:
        fetched = _fetch_openrouter()
        if not fetched:
            if not quiet:
                print("Warning: OpenRouter returned no pricing data.", file=sys.stderr)
            return False
        _save_cache(fetched)
        # Reset lazy state so next _get_pricing() rebuilds both globals atomically
        _pricing_table = None
        if not quiet:
            print(f"Pricing cache refreshed: {len(fetched)} models.")
        return True
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as e:
        if not quiet:
            print(f"Warning: could not refresh pricing from OpenRouter: {e}", file=sys.stderr)
        return False


def ensure_pricing_fresh(quiet: bool = True) -> None:
    """Refresh pricing cache if stale. Called at scan/serve startup."""
    if _cache_is_stale():
        refresh_pricing(quiet=quiet)


def _get_pricing() -> tuple[dict[str, tuple[float, float]], list[str]]:
    """Lazy-load the pricing table from cache + builtins."""
    global _pricing_table, _sorted_prefixes
    if _pricing_table is None:
        base = dict(_BUILTIN_PRICING)
        cached = _load_cache()
        if cached:
            base.update(cached)
        _pricing_table = base
        _sorted_prefixes = sorted(base.keys(), key=len, reverse=True)
    return _pricing_table, _sorted_prefixes


def estimate_cost(
    model: str | None,
    input_tokens: int,
    output_tokens: int,
    *,
    cache_read_tokens: int = 0,
    cache_creation_tokens: int = 0,
) -> float | None:
    """Estimate cost in USD for a session given model and token counts.

    Returns None if model is None or empty. Uses longest-prefix matching
    against cached pricing data.

    When cache_read_tokens or cache_creation_tokens are provided, the
    input_tokens value should be the NON-cached input tokens only.
    Cache reads are priced at 10% of the input rate, cache creation at
    125% of the input rate (matching Anthropic's prompt caching pricing).
    """
    if not model:
        return None
    input_tokens = input_tokens or 0
    output_tokens = output_tokens or 0
    cache_read_tokens = cache_read_tokens or 0
    cache_creation_tokens = cache_creation_tokens or 0
    normalized = _normalize_model_name(model)
    pricing_table, sorted_prefixes = _get_pricing()
    for prefix in sorted_prefixes:
        if normalized.startswith(prefix):
            input_rate, output_rate = pricing_table[prefix]
            cost = (
                input_tokens * input_rate
                + output_tokens * output_rate
                + cache_read_tokens * input_rate * 0.1
                + cache_creation_tokens * input_rate * 1.25
            ) / 1_000_000
            return cost
    # No match — return None rather than guessing
    return None


def _model_family(normalized: str) -> str:
    """Coarse family key (leading alphabetic run) for grouping comparable models.

    e.g. 'claude-opus-4' -> 'claude', 'gpt-4.1-mini' -> 'gpt',
    'gemini-2.5-pro' -> 'gemini', 'deepseek-r1' -> 'deepseek'.
    """
    head = normalized.rsplit("/", 1)[-1]
    out: list[str] = []
    for ch in head:
        if ch.isalpha():
            out.append(ch)
        else:
            break
    return "".join(out) or head


def cheapest_equivalent_rate(model: str | None) -> tuple[float, float] | None:
    """Return the (input, output) per-1M rate of the cheapest same-family model
    that undercuts *model* on input price, or None if the model is unknown or is
    already the cheapest in its family.

    Used to estimate model-downgrade savings with a grounded substitution (e.g.
    opus -> haiku) instead of a flat guess.
    """
    if not model:
        return None
    normalized = _normalize_model_name(model)
    pricing_table, sorted_prefixes = _get_pricing()
    current: tuple[float, float] | None = None
    for prefix in sorted_prefixes:
        if normalized.startswith(prefix):
            current = pricing_table[prefix]
            break
    if current is None or current[0] <= 0:
        return None
    family = _model_family(normalized)
    cheapest: tuple[float, float] | None = None
    for name, (in_rate, out_rate) in pricing_table.items():
        if _model_family(name) != family:
            continue
        if in_rate < current[0] and (cheapest is None or in_rate < cheapest[0]):
            cheapest = (in_rate, out_rate)
    return cheapest


def downgrade_savings_ratio(model: str | None) -> float | None:
    """Fraction of input cost saved by switching *model* to its cheapest
    same-family sibling, clamped to [0, 1), or None if no cheaper sibling or
    price is known.

    Grounds the model-downgrade savings estimate in the live pricing table
    instead of a flat guess.
    """
    if not model:
        return None
    normalized = _normalize_model_name(model)
    pricing_table, sorted_prefixes = _get_pricing()
    current: tuple[float, float] | None = None
    for prefix in sorted_prefixes:
        if normalized.startswith(prefix):
            current = pricing_table[prefix]
            break
    if current is None or current[0] <= 0:
        return None
    cheaper = cheapest_equivalent_rate(model)
    if cheaper is None:
        return None
    ratio = 1.0 - (cheaper[0] / current[0])
    return max(0.0, min(1.0, ratio))


def format_cost(cost: float | None) -> str:
    """Format cost as a human-readable string like '$0.32' or '$1.73'."""
    if cost is None:
        return ""
    if cost == 0:
        return "$0.00"
    if cost < 0.01:
        return f"${cost:.4f}"
    if cost < 1.00:
        return f"${cost:.2f}"
    return f"${cost:.2f}"
