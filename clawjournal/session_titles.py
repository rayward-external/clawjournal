"""Shared session-title resolution helpers.

The workbench stores its heuristic ``display_title`` with a fork label so
plain-title consumers (including FTS and the web UI) can distinguish Codex
Desktop fork rollouts.  AI titles are kept as the scorer produced them, so
consumers that prefer ``ai_display_title`` must add the same label at display
time.  Keeping that rule here prevents the two title paths from drifting.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .redaction.secrets import redact_text

_FORK_TITLE_SUFFIX_RE = re.compile(r"(?: · fork: [^\r\n]*)+$")


def fork_title_suffix(session: Mapping[str, Any]) -> str:
    """Return the stable display suffix for a fork, or ``""`` otherwise."""
    if not session.get("fork_of"):
        return ""

    nickname = session.get("fork_nickname")
    if isinstance(nickname, str) and nickname.strip():
        label = " ".join(nickname.split())
    else:
        session_id = str(session.get("session_id") or "")
        fork_id = session_id.split("_seg-", 1)[0]
        label = fork_id[-4:] or "fork"
    label, _, _ = redact_text(label)
    return f" · fork: {label}"


def append_fork_title_suffix(title: str, session: Mapping[str, Any]) -> str:
    """Append the current fork label, replacing an older generated label."""
    clean_title = title.strip()
    suffix = fork_title_suffix(session)
    if not clean_title or not suffix or clean_title.endswith(suffix):
        return clean_title
    base_title = _FORK_TITLE_SUFFIX_RE.sub("", clean_title).rstrip()
    return f"{base_title}{suffix}" if base_title else suffix.strip()


def resolve_session_title(
    session: Mapping[str, Any],
    *,
    prefer_ai: bool = True,
    fallback: str = "Untitled",
) -> str:
    """Choose the effective title and consistently label fork rollouts.

    ``prefer_ai=False`` deliberately ignores ``ai_display_title`` for callers
    whose contract is to show the ingest-derived title.
    """
    title = session.get("ai_display_title") if prefer_ai else None
    if not isinstance(title, str) or not title.strip():
        title = session.get("display_title")
    if not isinstance(title, str) or not title.strip():
        title = fallback
    return append_fork_title_suffix(str(title), session)
