"""Tests for the pricing module with OpenRouter cache."""

import json
import time
from unittest.mock import patch, MagicMock
from io import BytesIO

import pytest

from clawjournal.pricing import (
    _BUILTIN_PRICING,
    _fetch_openrouter,
    _load_cache,
    _save_cache,
    _cache_is_stale,
    _normalize_model_name,
    cheapest_equivalent_rate,
    downgrade_savings_ratio,
    estimate_cost,
    format_cost,
    refresh_pricing,
    CACHE_FILE,
)
import clawjournal.pricing as pricing_module


@pytest.fixture(autouse=True)
def reset_pricing_state(tmp_path, monkeypatch):
    """Reset lazy-loaded pricing state and redirect cache to tmp_path."""
    monkeypatch.setattr(pricing_module, "CACHE_FILE", tmp_path / "pricing_cache.json")
    monkeypatch.setattr(pricing_module, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(pricing_module, "_pricing_table", None)
    monkeypatch.setattr(pricing_module, "_sorted_prefixes", None)
    yield


class TestEstimateCost:
    def test_builtin_fallback_no_cache(self):
        """Without a cache file, estimate_cost uses built-in pricing."""
        cost = estimate_cost("claude-opus-4", 1_000_000, 500_000)
        expected = (1_000_000 * 15.0 + 500_000 * 75.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_cached_pricing_used(self, tmp_path, monkeypatch):
        """Cached pricing takes precedence over builtin."""
        cache_data = {
            "fetched_at": time.time(),
            "version": 1,
            "models": {"claude-opus-4": [10.0, 50.0]},
        }
        cache_file = tmp_path / "pricing_cache.json"
        cache_file.write_text(json.dumps(cache_data))
        # Reset lazy state
        monkeypatch.setattr(pricing_module, "_pricing_table", None)
        monkeypatch.setattr(pricing_module, "_sorted_prefixes", None)

        cost = estimate_cost("claude-opus-4", 1_000_000, 500_000)
        expected = (1_000_000 * 10.0 + 500_000 * 50.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_prefix_matching(self):
        """Model names with date suffixes match via prefix."""
        cost = estimate_cost("claude-opus-4-20250514", 1_000_000, 0)
        expected = (1_000_000 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_none_model(self):
        assert estimate_cost(None, 100, 100) is None

    def test_empty_model(self):
        assert estimate_cost("", 100, 100) is None

    def test_unknown_model(self):
        assert estimate_cost("totally-unknown-model-xyz", 100, 100) is None

    def test_provider_prefix_stripped(self):
        cost = estimate_cost("anthropic/claude-opus-4", 1_000_000, 0)
        expected = (1_000_000 * 15.0) / 1_000_000
        assert cost == pytest.approx(expected)

    def test_zero_tokens(self):
        assert estimate_cost("claude-opus-4", 0, 0) == 0.0

    def test_cache_read_tokens_discounted(self):
        """Cache reads priced at 10% of input rate."""
        cost = estimate_cost("claude-opus-4", 0, 0, cache_read_tokens=1_000_000)
        expected = 1_000_000 * 15.0 * 0.1 / 1_000_000  # $1.50
        assert cost == pytest.approx(expected)

    def test_cache_creation_tokens_premium(self):
        """Cache creation priced at 125% of input rate."""
        cost = estimate_cost("claude-opus-4", 0, 0, cache_creation_tokens=1_000_000)
        expected = 1_000_000 * 15.0 * 1.25 / 1_000_000  # $18.75
        assert cost == pytest.approx(expected)

    def test_full_cost_with_all_token_types(self):
        """All token types combined correctly."""
        cost = estimate_cost(
            "claude-opus-4",
            input_tokens=100_000,       # non-cached input
            output_tokens=50_000,       # output
            cache_read_tokens=500_000,  # cache reads
            cache_creation_tokens=200_000,  # cache creation
        )
        expected = (
            100_000 * 15.0           # input: $1.50
            + 50_000 * 75.0          # output: $3.75
            + 500_000 * 15.0 * 0.1   # cache read: $0.75
            + 200_000 * 15.0 * 1.25  # cache create: $3.75
        ) / 1_000_000
        assert cost == pytest.approx(expected)


class TestCacheManagement:
    def test_save_and_load_roundtrip(self, tmp_path, monkeypatch):
        models = {"claude-opus-4": (5.0, 25.0), "gpt-4o": (2.5, 10.0)}
        _save_cache(models)
        loaded = _load_cache()
        assert loaded is not None
        assert loaded["claude-opus-4"] == (5.0, 25.0)
        assert loaded["gpt-4o"] == (2.5, 10.0)

    def test_load_cache_missing_file(self):
        assert _load_cache() is None

    def test_load_cache_corrupt_json(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "pricing_cache.json"
        cache_file.write_text("not valid json {{{")
        assert _load_cache() is None

    def test_load_cache_wrong_version(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "pricing_cache.json"
        cache_file.write_text(json.dumps({"version": 99, "models": {}}))
        assert _load_cache() is None

    def test_cache_is_stale_missing(self):
        assert _cache_is_stale() is True

    def test_cache_is_stale_fresh(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "pricing_cache.json"
        cache_file.write_text(json.dumps({
            "fetched_at": time.time(),
            "version": 1,
            "models": {},
        }))
        assert _cache_is_stale() is False

    def test_cache_is_stale_old(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "pricing_cache.json"
        cache_file.write_text(json.dumps({
            "fetched_at": time.time() - 8 * 24 * 3600,
            "version": 1,
            "models": {},
        }))
        assert _cache_is_stale() is True


class TestRefreshPricing:
    def _mock_openrouter_response(self, models_data):
        """Create a mock urlopen response."""
        response_body = json.dumps({"data": models_data}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None
        return mock_resp

    def test_refresh_success(self, monkeypatch):
        models_data = [
            {
                "id": "anthropic/claude-sonnet-4",
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "openai/gpt-4o",
                "pricing": {"prompt": "0.0000025", "completion": "0.00001"},
            },
        ]
        mock_resp = self._mock_openrouter_response(models_data)
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: mock_resp)

        success = refresh_pricing(quiet=True)
        assert success is True
        # Verify cache was written
        loaded = _load_cache()
        assert loaded is not None
        assert "claude-sonnet-4" in loaded
        assert loaded["claude-sonnet-4"] == pytest.approx((3.0, 15.0), rel=1e-3)
        assert "gpt-4o" in loaded

    def test_refresh_network_failure(self, monkeypatch):
        import urllib.error
        monkeypatch.setattr(
            "urllib.request.urlopen",
            lambda *a, **kw: (_ for _ in ()).throw(urllib.error.URLError("Network down")),
        )
        success = refresh_pricing(quiet=True)
        assert success is False

    def test_refresh_resets_lazy_cache(self, monkeypatch):
        # First, load builtin (15.0 per 1M input for opus)
        cost_before = estimate_cost("claude-opus-4", 1_000_000, 0)
        assert cost_before == pytest.approx(15.0)

        # Now refresh with different pricing (99.0 per 1M input)
        models_data = [
            {"id": "anthropic/claude-opus-4", "pricing": {"prompt": "0.000099", "completion": "0.00005"}},
        ]
        mock_resp = self._mock_openrouter_response(models_data)
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: mock_resp)
        refresh_pricing(quiet=True)

        # After refresh, estimate_cost should use new data
        cost_after = estimate_cost("claude-opus-4", 1_000_000, 0)
        assert cost_after != cost_before
        expected = (1_000_000 * 99.0) / 1_000_000  # 0.000099 * 1M = 99.0 per 1M
        assert cost_after == pytest.approx(expected)


class TestFetchOpenRouter:
    def _mock_urlopen(self, monkeypatch, data):
        response_body = json.dumps({"data": data}).encode()
        mock_resp = MagicMock()
        mock_resp.read.return_value = response_body
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = lambda s, *a: None
        monkeypatch.setattr("urllib.request.urlopen", lambda *a, **kw: mock_resp)

    def test_parses_response(self, monkeypatch):
        data = [
            {"id": "anthropic/claude-opus-4", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
            {"id": "google/gemini-2.5-pro", "pricing": {"prompt": "0.00000125", "completion": "0.00001"}},
        ]
        self._mock_urlopen(monkeypatch, data)
        models = _fetch_openrouter()
        assert "claude-opus-4" in models
        assert models["claude-opus-4"] == pytest.approx((5.0, 25.0), rel=1e-3)
        assert "gemini-2.5-pro" in models
        assert models["gemini-2.5-pro"] == pytest.approx((1.25, 10.0), rel=1e-3)

    def test_skips_free_models(self, monkeypatch):
        data = [
            {"id": "free/model", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "anthropic/claude-opus-4", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
        ]
        self._mock_urlopen(monkeypatch, data)
        models = _fetch_openrouter()
        assert "model" not in models
        assert "claude-opus-4" in models

    def test_skips_missing_pricing(self, monkeypatch):
        data = [
            {"id": "no-pricing/model", "name": "Some Model"},
            {"id": "null-pricing/model", "pricing": None},
            {"id": "anthropic/claude-opus-4", "pricing": {"prompt": "0.000005", "completion": "0.000025"}},
        ]
        self._mock_urlopen(monkeypatch, data)
        models = _fetch_openrouter()
        assert len(models) == 1
        assert "claude-opus-4" in models


class TestNormalizeModelName:
    def test_strips_provider(self):
        assert _normalize_model_name("anthropic/claude-opus-4") == "claude-opus-4"
        assert _normalize_model_name("openai/gpt-4o") == "gpt-4o"

    def test_lowercases(self):
        assert _normalize_model_name("Claude-Opus-4") == "claude-opus-4"

    def test_no_prefix(self):
        assert _normalize_model_name("claude-opus-4") == "claude-opus-4"


class TestFormatCost:
    def test_none(self):
        assert format_cost(None) == ""

    def test_zero(self):
        assert format_cost(0) == "$0.00"

    def test_small(self):
        assert format_cost(0.005) == "$0.0050"

    def test_normal(self):
        assert format_cost(0.32) == "$0.32"

    def test_large(self):
        assert format_cost(5.73) == "$5.73"


class TestModelDowngradeHelpers:
    """cheapest_equivalent_rate / downgrade_savings_ratio over the builtin table."""

    def test_cheapest_in_family(self):
        # opus -> haiku is the cheapest claude-family input rate (1.0/1M).
        assert cheapest_equivalent_rate("claude-opus-4") == (1.0, 5.0)

    def test_already_cheapest_returns_none(self):
        assert cheapest_equivalent_rate("claude-haiku-4") is None

    def test_unknown_model_returns_none(self):
        assert cheapest_equivalent_rate("totally-unknown-model-xyz") is None

    def test_savings_ratio_opus_to_haiku(self):
        # 1 - (1.0 / 15.0) ≈ 0.9333
        ratio = downgrade_savings_ratio("claude-opus-4")
        assert ratio == pytest.approx(1 - 1 / 15)

    def test_savings_ratio_none_when_cheapest(self):
        assert downgrade_savings_ratio("claude-haiku-4") is None

    def test_savings_ratio_none_for_unknown(self):
        assert downgrade_savings_ratio("totally-unknown-model-xyz") is None

    def test_savings_ratio_ignores_effort_via_prefix(self):
        # An "@ effort" suffix still prefix-matches the base model.
        ratio = downgrade_savings_ratio("claude-opus-4 @ xhigh")
        assert ratio == pytest.approx(1 - 1 / 15)
