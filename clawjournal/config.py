"""Persistent config for ClawJournal — stored at ~/.clawjournal/config.json"""

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import TypedDict, cast

CONFIG_DIR = Path.home() / ".clawjournal"
CONFIG_FILE = CONFIG_DIR / "config.json"


class ClawJournalConfig(TypedDict, total=False):
    """Expected shape of the config dict."""

    repo: str | None
    source: str | None  # "claude" | "codex" | "gemini" | "all"
    excluded_projects: list[str]
    redact_strings: list[str]
    redact_usernames: list[str]
    allowlist_entries: list[dict]  # [{type, text/regex/match_type, scope, reason, added}]
    last_export: dict
    stage: str | None  # "auth" | "configure" | "review" | "confirmed" | "done"
    projects_confirmed: bool  # True once user has addressed folder exclusions
    review_attestations: dict
    review_verification: dict
    last_confirm: dict
    publish_attestation: str
    daemon_port: int | None
    verified_email: str | None
    verified_email_token: str | None
    verified_email_token_expires_at: str | int | float | None
    pending_verification_id: str | None
    pending_verification_email: str | None
    pending_verification_expires_at: str | int | None
    ai_pii_review_enabled: bool
    scorer_backend: str | None
    scorer_backend_confirmed_at: str | None
    benchmark_tab_enabled: bool  # show/hide the Benchmark tab in the workbench UI (default on)
    scoring_warmup_declined: bool  # user declined the background auto-scorer (suppresses prompt + server-side auto-start)


DEFAULT_CONFIG: ClawJournalConfig = {
    "repo": None,
    "source": None,
    "excluded_projects": [],
    "redact_strings": [],
    "allowlist_entries": [],
    "benchmark_tab_enabled": True,
}


_KNOWN_PREFIXES = ("claude:", "codex:", "gemini:", "opencode:", "openclaw:", "kimi:", "cline:", "custom:")


def load_config() -> ClawJournalConfig:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                stored = json.load(f)
            config = cast(ClawJournalConfig, {**DEFAULT_CONFIG, **stored})
            changed = _migrate_excluded_projects(config)
            changed |= _migrate_remove_device_credentials(config)
            if changed:
                save_config(config)
            return config
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: could not read {CONFIG_FILE}: {e}", file=sys.stderr)
    return cast(ClawJournalConfig, dict(DEFAULT_CONFIG))


def normalize_excluded_project_names(names: list[str]) -> list[str]:
    """Normalize excluded project names to the stored display-name format."""
    return [_normalize_excluded_project_name(name) for name in names]


def _normalize_excluded_project_name(name: str) -> str:
    if name.startswith(_KNOWN_PREFIXES):
        return name
    return f"claude:{name}"


def _migrate_excluded_projects(config: ClawJournalConfig) -> bool:
    """Add ``claude:`` prefix to excluded projects that have no source prefix.

    Returns True if any entries were migrated (caller should persist).
    """
    excluded = config.get("excluded_projects", [])
    if not excluded:
        return False
    normalized = normalize_excluded_project_names(excluded)
    if normalized == excluded:
        return False
    excluded[:] = normalized
    return True


def _migrate_remove_device_credentials(config: ClawJournalConfig) -> bool:
    """Remove device_id and device_token from config (no longer used)."""
    changed = False
    for key in ("device_id", "device_token"):
        if key in config:
            del config[key]  # type: ignore[misc]
            changed = True
    return changed


def save_config(config: ClawJournalConfig) -> None:
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(config, f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
        except BaseException:
            os.unlink(tmp_path)
            raise
    except OSError as e:
        print(f"Warning: could not save {CONFIG_FILE}: {e}", file=sys.stderr)
