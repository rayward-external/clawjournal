"""Helpers for manual scoring overrides."""

from __future__ import annotations

import json
from typing import Any


def normalize_failure_evidence(value: Any) -> list[str]:
    """Return non-empty failure-evidence snippets from a string or sequence."""
    if value is None:
        return []
    if isinstance(value, str):
        candidates: list[Any] = [value]
    elif isinstance(value, (list, tuple)):
        candidates = list(value)
    else:
        return []

    snippets: list[str] = []
    seen: set[str] = set()
    for item in candidates:
        if not isinstance(item, str):
            continue
        snippet = item.strip()
        if not snippet or snippet in seen:
            continue
        snippets.append(snippet)
        seen.add(snippet)
    return snippets


def parse_scoring_detail(raw: Any) -> dict[str, Any]:
    """Parse an ``ai_scoring_detail`` payload into a mutable dict."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def failure_evidence_from_detail(raw: Any) -> list[str]:
    """Extract normalized ``ai_failure_evidence`` from scoring detail."""
    detail = parse_scoring_detail(raw)
    return normalize_failure_evidence(detail.get("ai_failure_evidence"))


def merge_failure_evidence(raw: Any, evidence: list[str]) -> str:
    """Merge failure evidence into scoring detail and serialize it."""
    detail = parse_scoring_detail(raw)
    merged = normalize_failure_evidence(
        [*failure_evidence_from_detail(detail), *evidence]
    )
    if merged:
        detail["ai_failure_evidence"] = merged
    return json.dumps(detail, sort_keys=True)


def requires_failure_evidence(score: Any) -> bool:
    """Return whether a failure-value score requires evidence."""
    try:
        return int(score) >= 4
    except (TypeError, ValueError):
        return False
