"""Persistent config for ClawJournal — stored at ~/.clawjournal/config.json"""

import json
import os
import sqlite3
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Mapping, TypedDict, cast

CONFIG_DIR = Path.home() / ".clawjournal"
CONFIG_FILE = CONFIG_DIR / "config.json"
AUTO_UPLOAD_EGRESS_LOCK_FILENAME = "auto-upload-egress.lock"


@contextmanager
def auto_upload_egress_lock() -> Iterator[None]:
    """Serialize short profile mutations with the submitting transition."""

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    path = CONFIG_DIR / AUTO_UPLOAD_EGRESS_LOCK_FILENAME
    file = path.open("a+b")
    try:
        if os.name == "nt":
            import msvcrt

            file.seek(0, os.SEEK_END)
            if file.tell() == 0:
                file.write(b"0")
                file.flush()
            file.seek(0)
            msvcrt.locking(file.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                file.seek(0)
                msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(file.fileno(), fcntl.LOCK_UN)
    finally:
        file.close()


_AUTO_UPLOAD_PROFILE_CONFIG_KEYS = (
    "source",
    "projects_confirmed",
    "excluded_projects",
    "redact_strings",
    "redact_usernames",
    "allowlist_entries",
    "ai_pii_review_enabled",
)


def _auto_upload_profile_projection(config: Mapping[str, object]) -> dict[str, object]:
    projection = {
        key: config.get(key)
        for key in _AUTO_UPLOAD_PROFILE_CONFIG_KEYS
    }
    projection["scorer_backend"] = (
        config.get("scorer_backend")
        if config.get("ai_pii_review_enabled") is True
        else None
    )
    return projection


def mark_auto_upload_profile_changed(conn: sqlite3.Connection) -> bool:
    """Pause an active enrollment in the caller's current DB transaction."""

    try:
        cursor = conn.execute(
            "UPDATE auto_upload_enrollment SET mode = 'paused', "
            "health = 'action_required', generation = generation + 1, "
            "next_retry_at = NULL, last_result_code = 'profile_changed', "
            "updated_at = ? WHERE singleton_id = 1 "
            "AND mode IN ('enabled', 'paused')",
            (datetime_now_iso(),),
        )
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return False
        raise
    return cursor.rowcount == 1


def datetime_now_iso() -> str:
    # Kept local to avoid importing the workbench/index layer from config.
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


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
    auto_upload_capability_available: bool  # non-authoritative UI offer cache; Enable revalidates live
    auto_upload_ui_enabled: bool  # internal rollout flag; default hidden


DEFAULT_CONFIG: ClawJournalConfig = {
    "repo": None,
    "source": None,
    "excluded_projects": [],
    "redact_strings": [],
    "allowlist_entries": [],
    "benchmark_tab_enabled": True,
    "auto_upload_ui_enabled": False,
}


_KNOWN_PREFIXES = ("claude:", "claude-science:", "codex:", "gemini:", "opencode:", "openclaw:", "kimi:", "cline:", "workbuddy:", "custom:")
_BOTH_SOURCES = ("claude", "codex")


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


def set_source_scope(config: ClawJournalConfig, source: str) -> None:
    """Set export source and invalidate project confirmation when scope changes."""
    if config.get("source") != source:
        config["projects_confirmed"] = False
    config["source"] = source


def source_scope_sources(source: str | None) -> tuple[str, ...] | None:
    """Return allowed session sources for a confirmed source scope.

    ``None`` means unrestricted/all sources. Legacy ``both`` means the
    original Claude+Codex pair, while ``all`` intentionally means every
    supported indexed source.
    """
    normalized = (source or "").strip().lower()
    if normalized in ("", "auto", "all"):
        return None
    if normalized == "both":
        return _BOTH_SOURCES
    return (normalized,)


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


def save_config(config: ClawJournalConfig) -> bool:
    """Persist config atomically; return False only when persistence failed."""

    try:
        with auto_upload_egress_lock():
            previous: dict[str, object] = dict(DEFAULT_CONFIG)
            try:
                stored = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
                if isinstance(stored, dict):
                    previous.update(stored)
            except (OSError, json.JSONDecodeError):
                pass
            profile_changed = _auto_upload_profile_projection(
                previous
            ) != _auto_upload_profile_projection(config)

            fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
            try:
                with os.fdopen(fd, "w") as f:
                    json.dump(config, f, indent=2)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp_path, CONFIG_FILE)
                tmp_path = ""
                if hasattr(os, "O_DIRECTORY"):
                    dir_fd = os.open(CONFIG_DIR, os.O_RDONLY | os.O_DIRECTORY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
            except BaseException:
                if tmp_path:
                    os.unlink(tmp_path)
                raise

            if profile_changed:
                # Best-effort pause stamp on a short-lived connection of its
                # own. The config file is already durably written above, and
                # the runner re-derives the egress profile hash from config at
                # selection, AI, and submit time, so a busy index (for example
                # a caller holding its own write transaction while saving
                # config) must not stall for long or fail the config write.
                index_path = CONFIG_DIR / "index.db"
                if index_path.exists():
                    try:
                        conn = sqlite3.connect(index_path, timeout=5)
                        try:
                            conn.execute("BEGIN IMMEDIATE")
                            mark_auto_upload_profile_changed(conn)
                            conn.commit()
                        except Exception:
                            conn.rollback()
                            raise
                        finally:
                            conn.close()
                    except sqlite3.Error as e:
                        print(
                            "Warning: could not pause automatic upload after a "
                            f"profile change: {e}",
                            file=sys.stderr,
                        )
    except (OSError, sqlite3.Error) as e:
        print(f"Warning: could not save {CONFIG_FILE}: {e}", file=sys.stderr)
        return False
    return True
