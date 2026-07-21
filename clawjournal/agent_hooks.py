"""Claude Code and Codex hooks for the automatic-upload scheduler.

This module is deliberately independent of :mod:`clawjournal.cli`.  A
``SessionStart`` hook must be cheap and must not run the normal CLI update or
prompt path.  The automatic-upload state and runner live elsewhere; this file
only owns agent configuration and the small, injectable hook dispatch seam.

The module entry point binds the local due check and detached runner lazily;
injected adapters keep that boundary directly testable. The hook process never
packages or uploads a trace itself.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal, Sequence

from .paths import atomic_write_text

HOOK_EVENT = "SessionStart"
HOOK_MODULE = "clawjournal.agent_hooks"
LEGACY_HOOK_PROFILE = "clawjournal-auto-upload-v1"
SUPPORTED_AGENTS = ("claude", "codex")
HOOK_TIMEOUT_SECONDS = 5

AgentName = Literal["claude", "codex"]
DueCheck = Callable[[AgentName, datetime], "DueDecision"]
RunnerSpawner = Callable[[AgentName], bool]
ObservationRecorder = Callable[[AgentName, datetime], None]


class AgentHookError(RuntimeError):
    """Raised when an agent hook configuration cannot be changed safely."""


@dataclass(frozen=True)
class DueDecision:
    """The bounded scheduler decision made during ``SessionStart``."""

    due: bool
    reason: str


@dataclass(frozen=True)
class SessionStartResult:
    """Internal hook result; nothing is printed into the agent session."""

    observed_at: datetime
    reason: str
    due: bool = False
    spawned: bool = False
    observation_recorded: bool = False


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_agents(agent: str | None) -> list[AgentName]:
    value = (agent or "all").strip().lower()
    if value in ("all", "both"):
        return ["claude", "codex"]
    if value not in SUPPORTED_AGENTS:
        allowed = ", ".join((*SUPPORTED_AGENTS, "all"))
        raise AgentHookError(f"Unsupported agent: {agent}. Expected one of: {allowed}.")
    return [value]  # type: ignore[list-item]


def _home_dir(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser()


def _claude_settings_path(home: Path | None = None) -> Path:
    # An explicit home= always wins so callers (tests especially) can never be
    # redirected into the real config by an inherited environment override.
    if home is not None:
        return _home_dir(home) / ".claude" / "settings.json"
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "settings.json"
    return _home_dir(None) / ".claude" / "settings.json"


def _codex_hooks_path(home: Path | None = None) -> Path:
    if home is not None:
        return _home_dir(home) / ".codex" / "hooks.json"
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser() / "hooks.json"
    return _home_dir(None) / ".codex" / "hooks.json"


def _hook_path(agent: AgentName, home: Path | None = None) -> Path:
    if agent == "claude":
        return _claude_settings_path(home)
    return _codex_hooks_path(home)


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as file:
            document = json.load(file)
    except json.JSONDecodeError as exc:
        raise AgentHookError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise AgentHookError(f"Could not read {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise AgentHookError(f"{path} must contain a JSON object.")
    return document


def _write_json_file(path: Path, document: dict[str, Any]) -> None:
    try:
        atomic_write_text(
            path,
            json.dumps(document, indent=2) + "\n",
            parents=True,
        )
    except OSError as exc:
        raise AgentHookError(f"Could not write {path}: {exc}") from exc


def _hook_argv(client: AgentName) -> list[str]:
    return [
        sys.executable,
        "-m",
        HOOK_MODULE,
        "run",
        "--client",
        client,
    ]


def _hook_command(client: AgentName) -> str:
    return shlex.join(_hook_argv(client))


def _hook_command_windows(client: AgentName) -> str:
    return subprocess.list2cmdline(_hook_argv(client))


def _handler_for(client: AgentName) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": _hook_command(client),
        "timeout": HOOK_TIMEOUT_SECONDS,
        "statusMessage": "Checking ClawJournal automatic upload schedule",
    }
    if client == "codex":
        handler["commandWindows"] = _hook_command_windows(client)
    return handler


def _handler_is_current(handler: Any) -> bool:
    if not isinstance(handler, dict) or handler.get("type") != "command":
        return False
    command = str(handler.get("command", ""))
    return f"-m {HOOK_MODULE}" in command and " run " in f" {command} "


def _handler_is_legacy(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and handler.get("clawjournalProfile") == LEGACY_HOOK_PROFILE
    )


def _handler_is_ours(handler: Any) -> bool:
    return _handler_is_current(handler) or _handler_is_legacy(handler)


def _upsert_session_start_hook(document: dict[str, Any], client: AgentName) -> bool:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise AgentHookError("The existing hooks field must be a JSON object.")
    groups = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise AgentHookError(
            f"The existing {HOOK_EVENT} hooks field must be a JSON array."
        )

    desired = _handler_for(client)
    found = False
    changed = False
    kept_groups: list[Any] = []

    for original_group in groups:
        if not isinstance(original_group, dict):
            kept_groups.append(original_group)
            continue
        handlers = original_group.get("hooks")
        if not isinstance(handlers, list):
            kept_groups.append(original_group)
            continue

        kept_handlers: list[Any] = []
        group_changed = False
        for handler in handlers:
            if not _handler_is_ours(handler):
                kept_handlers.append(handler)
                continue
            if not found:
                found = True
                kept_handlers.append(desired)
                if handler != desired:
                    group_changed = True
            else:
                # Collapse stale duplicates from interrupted or older installs.
                group_changed = True

        if group_changed:
            changed = True
            if kept_handlers:
                updated_group = dict(original_group)
                updated_group["hooks"] = kept_handlers
                kept_groups.append(updated_group)
        else:
            kept_groups.append(original_group)

    if not found:
        kept_groups.append({"hooks": [desired]})
        changed = True

    if changed:
        hooks[HOOK_EVENT] = kept_groups
    return changed


def _remove_session_start_hook(document: dict[str, Any]) -> bool:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get(HOOK_EVENT)
    if not isinstance(groups, list):
        return False

    changed = False
    kept_groups: list[Any] = []
    for original_group in groups:
        if not isinstance(original_group, dict):
            kept_groups.append(original_group)
            continue
        handlers = original_group.get("hooks")
        if not isinstance(handlers, list):
            kept_groups.append(original_group)
            continue
        kept_handlers = [handler for handler in handlers if not _handler_is_ours(handler)]
        if len(kept_handlers) == len(handlers):
            kept_groups.append(original_group)
            continue
        changed = True
        if kept_handlers:
            updated_group = dict(original_group)
            updated_group["hooks"] = kept_handlers
            kept_groups.append(updated_group)

    if changed:
        hooks[HOOK_EVENT] = kept_groups
    return changed


def install_agent_hook(agent: AgentName, *, home: Path | None = None) -> dict[str, Any]:
    """Install or refresh one agent's ``SessionStart`` entry."""

    path = _hook_path(agent, home)
    document = _read_json_file(path)
    changed = _upsert_session_start_hook(document, agent)
    if changed or not path.exists():
        _write_json_file(path, document)
    return {
        "agent": agent,
        "path": str(path),
        "configured": True,
        "changed": changed,
        "command": _hook_command(agent),
    }


def install_hooks(
    *, agent: str | None = "all", home: Path | None = None
) -> list[dict[str, Any]]:
    """Install the selected hooks, rolling back partial multi-agent setup."""

    agents = _normalize_agents(agent)
    snapshots: dict[Path, str | None] = {}
    try:
        for name in agents:
            path = _hook_path(name, home)
            snapshots[path] = path.read_text(encoding="utf-8") if path.exists() else None
        return [install_agent_hook(name, home=home) for name in agents]
    except Exception as exc:
        rollback_errors: list[str] = []
        for path, previous in snapshots.items():
            try:
                if previous is None:
                    path.unlink(missing_ok=True)
                else:
                    atomic_write_text(path, previous, parents=True)
            except OSError as rollback_exc:
                rollback_errors.append(f"{path}: {rollback_exc}")
        if rollback_errors:
            detail = "; ".join(rollback_errors)
            raise AgentHookError(
                f"Hook setup failed and rollback was incomplete: {detail}"
            ) from exc
        raise


def uninstall_agent_hook(
    agent: AgentName, *, home: Path | None = None
) -> dict[str, Any]:
    """Remove only ClawJournal's automatic-upload hook for one agent."""

    path = _hook_path(agent, home)
    if not path.exists():
        return {
            "agent": agent,
            "path": str(path),
            "configured": False,
            "changed": False,
        }
    document = _read_json_file(path)
    changed = _remove_session_start_hook(document)
    if changed:
        _write_json_file(path, document)
    return {
        "agent": agent,
        "path": str(path),
        "configured": False,
        "changed": changed,
    }


def uninstall_hooks(
    *, agent: str | None = "all", home: Path | None = None
) -> list[dict[str, Any]]:
    return [uninstall_agent_hook(name, home=home) for name in _normalize_agents(agent)]


def hook_diagnostics(
    agent: AgentName,
    *,
    home: Path | None = None,
    last_observed_at: datetime | str | None = None,
) -> dict[str, Any]:
    """Inspect configuration and combine it with SQLite-owned observation state."""

    path = _hook_path(agent, home)
    document = _read_json_file(path)
    hooks = document.get("hooks")
    groups = hooks.get(HOOK_EVENT) if isinstance(hooks, dict) else None
    installed_handlers: list[Any] = []
    if isinstance(groups, list):
        for group in groups:
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                installed_handlers.extend(
                    handler for handler in group["hooks"] if _handler_is_ours(handler)
                )
    desired = _handler_for(agent)
    current_handlers = [
        handler for handler in installed_handlers if _handler_is_current(handler)
    ]
    # A leftover legacy handler is migrated only by the next install or
    # uninstall; until then it must not flip `configured`, because the runner
    # blocks scheduled cycles on any selected hook that is not configured.
    configured = len(current_handlers) == 1 and current_handlers[0] == desired
    legacy_installed = len(current_handlers) != len(installed_handlers)
    observed = (
        last_observed_at.isoformat()
        if isinstance(last_observed_at, datetime)
        else last_observed_at
    )
    result: dict[str, Any] = {
        "agent": agent,
        "path": str(path),
        "configured": configured,
        "installed": bool(installed_handlers),
        "legacy_hook_installed": legacy_installed,
        "last_observed_at": observed,
    }
    if agent == "codex" and configured and not observed:
        result["diagnostic"] = (
            "Hook is configured but has not been observed; Codex may still be "
            "awaiting hook trust."
        )
    return result


def _runner_not_configured(_client: AgentName, _now: datetime) -> DueDecision:
    return DueDecision(False, "runner-not-configured")


def detached_process_kwargs(*, platform: str | None = None) -> dict[str, Any]:
    """Return non-blocking subprocess options for the eventual runner adapter."""

    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if (platform or os.name) == "nt":
        kwargs["creationflags"] = 0x00000200 | 0x00000008
    else:
        kwargs["start_new_session"] = True
    return kwargs


def spawn_detached(argv: Sequence[str]) -> subprocess.Popen[Any]:
    """Spawn an already-decided runner without inheriting the hook process."""

    return subprocess.Popen(list(argv), **detached_process_kwargs())


def run_session_start(
    *,
    client: AgentName,
    now: datetime | None = None,
    due_check: DueCheck | None = None,
    spawn_runner: RunnerSpawner | None = None,
    record_observed: ObservationRecorder | None = None,
) -> SessionStartResult:
    """Perform the bounded hook check and fail open on every adapter error."""

    observed_at = now or _now()
    observation_recorded = False
    if record_observed is not None:
        try:
            record_observed(client, observed_at)
            observation_recorded = True
        except Exception:
            # Diagnostics must never make an agent session fail to start.
            pass

    check = due_check or _runner_not_configured
    try:
        decision = check(client, observed_at)
        if not isinstance(decision, DueDecision):
            raise TypeError("due check must return DueDecision")
    except Exception:
        return SessionStartResult(
            observed_at,
            "due-check-error",
            observation_recorded=observation_recorded,
        )
    if not decision.due:
        return SessionStartResult(
            observed_at,
            decision.reason,
            observation_recorded=observation_recorded,
        )
    if spawn_runner is None:
        return SessionStartResult(
            observed_at,
            "runner-not-configured",
            due=True,
            observation_recorded=observation_recorded,
        )
    try:
        spawned = bool(spawn_runner(client))
    except Exception:
        return SessionStartResult(
            observed_at,
            "runner-spawn-error",
            due=True,
            observation_recorded=observation_recorded,
        )
    return SessionStartResult(
        observed_at,
        "runner-spawned" if spawned else "runner-not-spawned",
        due=True,
        spawned=spawned,
        observation_recorded=observation_recorded,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    due_check: DueCheck | None = None,
    spawn_runner: RunnerSpawner | None = None,
    record_observed: ObservationRecorder | None = None,
) -> int:
    """Lightweight module entrypoint used by agent hook configuration."""

    parser = argparse.ArgumentParser(prog=f"python -m {HOOK_MODULE}")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--client", choices=SUPPORTED_AGENTS, required=True)
    args = parser.parse_args(argv)
    if args.command == "run":
        if due_check is None and spawn_runner is None and record_observed is None:
            # Lazy import keeps hook configuration and diagnostics independent
            # of the heavier scanner/share stack.  The installed module entry
            # point binds the real SQLite adapters only at execution time.
            from .auto_upload import (
                hook_session_start_check,
                spawn_scheduled_runner,
            )

            # Observation and the due/retry read share one short transaction.
            # If the DB is absent, stale, or writer-locked, the adapter returns
            # not-due and the agent session continues without spawning.
            due_check = hook_session_start_check
            spawn_runner = spawn_scheduled_runner
        run_session_start(
            client=args.client,
            due_check=due_check,
            spawn_runner=spawn_runner,
            record_observed=record_observed,
        )
    # No output: SessionStart must never inject scheduler text into a trace.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through main()
    raise SystemExit(main())
