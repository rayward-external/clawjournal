"""OpenRefinery enrollment hooks for Claude Code and Codex.

The hook itself does not package or upload traces. It only nudges an enrolled
participant up to a small daily cap and launches the existing local Share
workflow on request, so the normal source/project confirmation, redaction,
hold-state, and TruffleHog gates remain authoritative.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import webbrowser
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Literal

from . import config as config_module

PROFILE_NAME = "openrefinery-failures"
PROFILE_DISPLAY_NAME = "OpenRefinery Agent Failure Sharing"
SUPPORTED_AGENTS = ("claude", "codex")
SUPPORTED_UI_MODES = ("auto", "web", "cli")
DEFAULT_PORT = 8384
DEFAULT_SNOOZE_DAYS = 30
# Production cadence: a single daily nudge for an already-enrolled participant.
DEFAULT_MAX_PROMPTS_PER_DAY = 1
# Test cadence: opt-in via OPENREFINERY_SHARE_HOOK_TEST=1 so it can never ship as
# the default. Lets a developer see the reminder fire repeatedly in one day.
TEST_MAX_PROMPTS_PER_DAY = 10
STATE_VERSION = 1

AgentName = Literal["claude", "codex"]
UiMode = Literal["auto", "web", "cli"]


class HookError(RuntimeError):
    """Raised for user-actionable hook setup problems."""


@dataclass(frozen=True)
class HookRunResult:
    should_prompt: bool
    reason: str
    message: str | None = None
    state_path: Path | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today(now: datetime | None = None) -> date:
    return (now or _now()).astimezone().date()


def _state_path() -> Path:
    return config_module.CONFIG_DIR / "hooks" / f"{PROFILE_NAME}.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open(encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError as exc:
        raise HookError(f"Could not parse {path}: {exc}") from exc
    except OSError as exc:
        raise HookError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HookError(f"{path} must contain a JSON object.")
    return data


def _write_json_file(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_state() -> dict[str, Any]:
    state = _read_json_file(_state_path())
    if not state:
        state = {
            "version": STATE_VERSION,
            "profile": PROFILE_NAME,
            "display_name": PROFILE_DISPLAY_NAME,
            "enabled": False,
            "ui": "auto",
            "cadence": "daily",
            "max_prompts_per_day": DEFAULT_MAX_PROMPTS_PER_DAY,
            "prompt_counts_by_date": {},
            "installed_agents": [],
            "created_at": _now().isoformat(),
        }
    state.setdefault("version", STATE_VERSION)
    state.setdefault("profile", PROFILE_NAME)
    state.setdefault("display_name", PROFILE_DISPLAY_NAME)
    state.setdefault("enabled", False)
    state.setdefault("ui", "auto")
    state.setdefault("cadence", "daily")
    state.setdefault("max_prompts_per_day", DEFAULT_MAX_PROMPTS_PER_DAY)
    state.setdefault("installed_agents", [])
    counts = state.get("prompt_counts_by_date")
    if not isinstance(counts, dict):
        counts = {}
    legacy_last_prompt = state.get("last_prompt_date")
    if legacy_last_prompt and str(legacy_last_prompt) not in counts:
        counts[str(legacy_last_prompt)] = 1
    state["prompt_counts_by_date"] = counts
    return state


def save_state(state: dict[str, Any]) -> Path:
    state["version"] = STATE_VERSION
    state["profile"] = PROFILE_NAME
    state["display_name"] = PROFILE_DISPLAY_NAME
    state["updated_at"] = _now().isoformat()
    path = _state_path()
    _write_json_file(path, state)
    return path


def _normalize_profile(profile: str) -> str:
    if profile != PROFILE_NAME:
        raise HookError(f"Unknown hook profile: {profile}. Expected {PROFILE_NAME}.")
    return profile


def _normalize_agents(agent: str | None) -> list[AgentName]:
    value = (agent or "all").strip().lower()
    if value in ("all", "both"):
        return ["claude", "codex"]
    if value not in SUPPORTED_AGENTS:
        allowed = ", ".join((*SUPPORTED_AGENTS, "all"))
        raise HookError(f"Unsupported agent: {agent}. Expected one of: {allowed}.")
    return [value]  # type: ignore[list-item]


def _normalize_ui(ui: str | None) -> UiMode:
    value = (ui or "auto").strip().lower()
    if value not in SUPPORTED_UI_MODES:
        raise HookError(
            f"Unsupported UI mode: {ui}. Expected one of: {', '.join(SUPPORTED_UI_MODES)}."
        )
    return value  # type: ignore[return-value]


def _home_dir(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser()


def _claude_settings_path(home: Path | None = None) -> Path:
    override = os.environ.get("CLAUDE_CONFIG_DIR")
    if override:
        return Path(override).expanduser() / "settings.json"
    return _home_dir(home) / ".claude" / "settings.json"


def _codex_hooks_path(home: Path | None = None) -> Path:
    override = os.environ.get("CODEX_HOME")
    if override:
        return Path(override).expanduser() / "hooks.json"
    return _home_dir(home) / ".codex" / "hooks.json"


def _hook_command(client: AgentName) -> str:
    pieces = [
        shlex.quote(sys.executable),
        "-m",
        "clawjournal.cli",
        "hooks",
        "run",
        PROFILE_NAME,
        "--client",
        client,
    ]
    return " ".join(pieces)


def _handler_for(client: AgentName) -> dict[str, Any]:
    return {
        "type": "command",
        "command": _hook_command(client),
        "timeout": 30,
        "statusMessage": "Checking OpenRefinery failure-sharing reminder",
    }


def _handler_is_ours(handler: Any) -> bool:
    return (
        isinstance(handler, dict)
        and handler.get("type") == "command"
        and PROFILE_NAME in str(handler.get("command", ""))
        and "hooks run" in str(handler.get("command", ""))
    )


def _upsert_stop_hook(document: dict[str, Any], client: AgentName) -> bool:
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise HookError("The existing hooks field must be a JSON object.")
    groups = hooks.setdefault("Stop", [])
    if not isinstance(groups, list):
        raise HookError("The existing Stop hooks field must be a JSON array.")

    desired = _handler_for(client)
    for group in groups:
        if not isinstance(group, dict):
            continue
        handlers = group.setdefault("hooks", [])
        if not isinstance(handlers, list):
            continue
        for idx, handler in enumerate(handlers):
            if _handler_is_ours(handler):
                if handler == desired:
                    return False
                handlers[idx] = desired
                return True

    groups.append({"hooks": [desired]})
    return True


def _remove_stop_hook(document: dict[str, Any]) -> bool:
    hooks = document.get("hooks")
    if not isinstance(hooks, dict):
        return False
    groups = hooks.get("Stop")
    if not isinstance(groups, list):
        return False
    changed = False
    kept_groups = []
    for group in groups:
        if not isinstance(group, dict):
            kept_groups.append(group)
            continue
        handlers = group.get("hooks")
        if not isinstance(handlers, list):
            kept_groups.append(group)
            continue
        kept_handlers = [handler for handler in handlers if not _handler_is_ours(handler)]
        if len(kept_handlers) != len(handlers):
            changed = True
        if kept_handlers:
            group = dict(group)
            group["hooks"] = kept_handlers
            kept_groups.append(group)
    if changed:
        hooks["Stop"] = kept_groups
    return changed


def install_agent_hook(agent: AgentName, *, home: Path | None = None) -> dict[str, Any]:
    path = _claude_settings_path(home) if agent == "claude" else _codex_hooks_path(home)
    document = _read_json_file(path)
    changed = _upsert_stop_hook(document, agent)
    if changed or not path.exists():
        _write_json_file(path, document)
    return {
        "agent": agent,
        "path": str(path),
        "changed": changed,
        "command": _hook_command(agent),
    }


def uninstall_agent_hook(agent: AgentName, *, home: Path | None = None) -> dict[str, Any]:
    path = _claude_settings_path(home) if agent == "claude" else _codex_hooks_path(home)
    if not path.exists():
        return {"agent": agent, "path": str(path), "changed": False}
    document = _read_json_file(path)
    changed = _remove_stop_hook(document)
    if changed:
        _write_json_file(path, document)
    return {"agent": agent, "path": str(path), "changed": changed}


def install_profile(
    *,
    profile: str = PROFILE_NAME,
    agent: str | None = "all",
    ui: str | None = "auto",
    source_scope: str | None = "both",
    home: Path | None = None,
) -> dict[str, Any]:
    _normalize_profile(profile)
    agents = _normalize_agents(agent)
    ui_mode = _normalize_ui(ui)

    installed = [install_agent_hook(agent_name, home=home) for agent_name in agents]
    state = load_state()
    known_agents = set(state.get("installed_agents") or [])
    known_agents.update(agents)
    state.update(
        {
            "enabled": True,
            "ui": ui_mode,
            "cadence": "daily",
            # Refresh the cap to the current default so an explicit re-install
            # adopts a changed cadence instead of keeping a value frozen in the
            # state file from an earlier version.
            "max_prompts_per_day": DEFAULT_MAX_PROMPTS_PER_DAY,
            "installed_agents": sorted(known_agents),
            "source_scope": source_scope or "both",
        }
    )
    state_path = save_state(state)

    if source_scope:
        config = config_module.load_config()
        config_module.set_source_scope(config, source_scope)
        config_module.save_config(config)

    return {
        "profile": PROFILE_NAME,
        "enabled": True,
        "ui": ui_mode,
        "cadence": "daily",
        "state_path": str(state_path),
        "installed": installed,
        "source_scope": source_scope,
        "projects_confirmed": bool(config_module.load_config().get("projects_confirmed", False)),
    }


def disable_profile(*, profile: str = PROFILE_NAME) -> dict[str, Any]:
    _normalize_profile(profile)
    state = load_state()
    state["enabled"] = False
    path = save_state(state)
    return {"profile": PROFILE_NAME, "enabled": False, "state_path": str(path)}


def snooze_profile(
    *,
    profile: str = PROFILE_NAME,
    days: int = DEFAULT_SNOOZE_DAYS,
    now: datetime | None = None,
) -> dict[str, Any]:
    _normalize_profile(profile)
    if days < 1:
        raise HookError("--days must be at least 1.")
    state = load_state()
    until = _today(now) + timedelta(days=days)
    state["snooze_until"] = until.isoformat()
    path = save_state(state)
    return {
        "profile": PROFILE_NAME,
        "snooze_until": state["snooze_until"],
        "state_path": str(path),
    }


def uninstall_profile(
    *,
    profile: str = PROFILE_NAME,
    agent: str | None = "all",
    home: Path | None = None,
) -> dict[str, Any]:
    _normalize_profile(profile)
    agents = _normalize_agents(agent)
    removed = [uninstall_agent_hook(agent_name, home=home) for agent_name in agents]
    state = load_state()
    installed = set(state.get("installed_agents") or [])
    installed.difference_update(agents)
    state["installed_agents"] = sorted(installed)
    if not installed:
        state["enabled"] = False
    path = save_state(state)
    return {
        "profile": PROFILE_NAME,
        "enabled": bool(state.get("enabled")),
        "state_path": str(path),
        "removed": removed,
    }


def status(*, profile: str = PROFILE_NAME, now: datetime | None = None) -> dict[str, Any]:
    _normalize_profile(profile)
    state = load_state()
    today = _today(now)
    today_key = today.isoformat()
    prompt_counts = _prompt_counts_by_date(state)
    max_prompts = _max_prompts_per_day(state)
    prompts_today = prompt_counts.get(today_key, 0)
    next_prompt = _next_prompt_date(state, today, prompts_today, max_prompts)
    readiness = share_readiness()
    return {
        "profile": PROFILE_NAME,
        "display_name": PROFILE_DISPLAY_NAME,
        "enabled": bool(state.get("enabled", False)),
        "ui": state.get("ui", "auto"),
        "cadence": state.get("cadence", "daily"),
        "max_prompts_per_day": max_prompts,
        "prompts_today": prompts_today,
        "prompts_remaining_today": max(0, max_prompts - prompts_today),
        "installed_agents": list(state.get("installed_agents") or []),
        "last_prompt_date": state.get("last_prompt_date"),
        "snooze_until": state.get("snooze_until"),
        "snoozed": _is_snoozed(state, today),
        "snooze_days_remaining": _snooze_days_remaining(state, today),
        "next_prompt": next_prompt,
        "eligible_now": next_prompt == today_key,
        "state_path": str(_state_path()),
        "source_scope": state.get("source_scope"),
        "source_confirmed": readiness["source_confirmed"],
        "projects_confirmed": readiness["projects_confirmed"],
        "share_ready": readiness["ready"],
    }


def _is_snoozed(state: dict[str, Any], today: date) -> bool:
    raw = state.get("snooze_until")
    if not raw:
        return False
    try:
        return date.fromisoformat(str(raw)) >= today
    except ValueError:
        return False


def _hook_disabled_by_env() -> bool:
    return (
        os.environ.get("CLAWJOURNAL_DISABLE_SHARE_NUDGE") == "1"
        or os.environ.get("OPENREFINERY_SHARE_HOOK_DISABLE") == "1"
    )


def _test_mode() -> bool:
    """Opt-in testing cadence so the reminder can fire repeatedly in one day."""
    return os.environ.get("OPENREFINERY_SHARE_HOOK_TEST") == "1"


def _max_prompts_per_day(state: dict[str, Any]) -> int:
    if _test_mode():
        return TEST_MAX_PROMPTS_PER_DAY
    try:
        value = int(state.get("max_prompts_per_day", DEFAULT_MAX_PROMPTS_PER_DAY))
    except (TypeError, ValueError):
        return DEFAULT_MAX_PROMPTS_PER_DAY
    return value if value > 0 else DEFAULT_MAX_PROMPTS_PER_DAY


def _prompt_counts_by_date(state: dict[str, Any]) -> dict[str, int]:
    raw_counts = state.get("prompt_counts_by_date")
    if not isinstance(raw_counts, dict):
        return {}
    counts: dict[str, int] = {}
    for raw_day, raw_count in raw_counts.items():
        try:
            day = date.fromisoformat(str(raw_day)).isoformat()
            count = int(raw_count)
        except (TypeError, ValueError):
            continue
        if count > 0:
            counts[day] = count
    return counts


def _trim_prompt_counts(counts: dict[str, int], today: date) -> dict[str, int]:
    earliest = today - timedelta(days=31)
    trimmed: dict[str, int] = {}
    for raw_day, count in counts.items():
        try:
            day = date.fromisoformat(raw_day)
        except ValueError:
            continue
        if day >= earliest:
            trimmed[day.isoformat()] = count
    return trimmed


def share_readiness(config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Report whether the existing Share gates are satisfied.

    The hook only nudges; the actual upload still requires an explicit source
    scope and a confirmed project list. Surfacing this lets `status`/`launch`
    tell the participant what is still missing instead of sending them into a
    Share flow that will block.
    """
    cfg = config if config is not None else config_module.load_config()
    source = cfg.get("source")
    source_confirmed = bool(source) and str(source).strip().lower() != "auto"
    projects_confirmed = bool(cfg.get("projects_confirmed", False))
    return {
        "source": source,
        "source_confirmed": source_confirmed,
        "projects_confirmed": projects_confirmed,
        "ready": source_confirmed and projects_confirmed,
    }


def _snooze_days_remaining(state: dict[str, Any], today: date) -> int:
    raw = state.get("snooze_until")
    if not raw:
        return 0
    try:
        until = date.fromisoformat(str(raw))
    except ValueError:
        return 0
    return (until - today).days + 1 if until >= today else 0


def _next_prompt_date(
    state: dict[str, Any], today: date, prompts_today: int, max_prompts: int
) -> str | None:
    """ISO date the next reminder is eligible, or None if reminders are off."""
    if not state.get("enabled", False):
        return None
    raw = state.get("snooze_until")
    if raw:
        try:
            until = date.fromisoformat(str(raw))
            if until >= today:
                return (until + timedelta(days=1)).isoformat()
        except ValueError:
            pass
    if prompts_today >= max_prompts:
        return (today + timedelta(days=1)).isoformat()
    return today.isoformat()


def _prompt_message(launch_command: str) -> str:
    # Claude Code prints this verbatim as "Stop hook feedback" and Codex as a
    # blocked-stop reason, so keep it a tight directive — not a wall of agent-only
    # instructions. The privacy reassurance is baked in so the agent surfaces it
    # every time, not only when it happens to.
    return (
        "OpenRefinery daily reminder — ask the user (y/n) whether to open the "
        "local review of recent agent-failure sessions to consider sharing "
        "(nothing's uploaded until they approve). "
        f"On yes, run `{launch_command}`. On no, drop it."
    )


def run_hook(
    *,
    profile: str = PROFILE_NAME,
    client: str = "codex",
    force: bool = False,
    dry_run: bool = False,
    stop_hook_active: bool = False,
    now: datetime | None = None,
) -> HookRunResult:
    _normalize_profile(profile)
    today = _today(now)
    state = load_state()
    if _hook_disabled_by_env():
        return HookRunResult(False, "disabled-by-env", state_path=_state_path())
    if not state.get("enabled", False):
        return HookRunResult(False, "disabled", state_path=_state_path())
    # `stop_hook_active` means this Stop fired because a previous reminder kept the
    # turn going; re-prompting now would loop and burn the daily budget at once.
    if stop_hook_active and not force:
        return HookRunResult(False, "stop-hook-active", state_path=_state_path())
    if not force and _is_snoozed(state, today):
        return HookRunResult(False, "snoozed", state_path=_state_path())
    today_key = today.isoformat()
    prompt_counts = _prompt_counts_by_date(state)
    prompt_count = prompt_counts.get(today_key, 0)
    max_prompts = _max_prompts_per_day(state)
    if not force and prompt_count >= max_prompts:
        return HookRunResult(False, "daily-prompt-limit-reached", state_path=_state_path())

    launch_command = f"clawjournal hooks launch {PROFILE_NAME}"
    message = _prompt_message(launch_command)
    # A preview must never consume a daily slot or mutate state.
    if dry_run:
        return HookRunResult(True, "dry-run", message, state_path=_state_path())

    prompt_counts[today_key] = prompt_count + 1
    state["prompt_counts_by_date"] = _trim_prompt_counts(prompt_counts, today)
    state["last_prompt_date"] = today_key
    state["last_prompt_client"] = client
    path = save_state(state)
    return HookRunResult(True, "prompt", message, state_path=path)


def render_hook_response(result: HookRunResult, *, client: str, output_json: bool = False) -> str:
    if output_json:
        return json.dumps(
            {
                "should_prompt": result.should_prompt,
                "reason": result.reason,
                "message": result.message,
                "state_path": str(result.state_path) if result.state_path else None,
            },
            indent=2,
        )
    if not result.should_prompt or not result.message:
        return ""
    if client == "claude":
        payload = {
            "hookSpecificOutput": {
                "hookEventName": "Stop",
                "additionalContext": result.message,
            },
        }
    else:
        payload = {"decision": "block", "reason": result.message}
    return json.dumps(payload)


def _workbench_responding(port: int, *, timeout: float = 1.0) -> bool:
    """True only if *our* ClawJournal workbench answers on the port.

    A bare TCP connect can't tell our daemon apart from an unrelated process
    holding the same port. Trusting it would let us open the wrong
    ``localhost:<port>`` page, or treat a foreign listener as "already running"
    and then spawn a daemon that silently rebinds to an ephemeral port (a leaked,
    unreachable process). So fetch the SPA shell and look for a ClawJournal
    signature: the ``clawjournal_token`` cookie the daemon sets on HTML
    responses, or the ``<title>ClawJournal</title>`` marker in the served index.
    """
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            cookie = resp.headers.get("Set-Cookie", "") or ""
            body = resp.read(4096).decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError):
        return False
    return "clawjournal_token" in cookie or "ClawJournal" in body


def _open_browser(url: str) -> bool:
    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False


def _frontend_available() -> bool:
    try:
        from .workbench.daemon import FRONTEND_DIST
    except Exception:
        return False
    return (FRONTEND_DIST / "index.html").exists()


def _start_detached_server(port: int) -> subprocess.Popen[Any]:
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name != "nt":
        kwargs["start_new_session"] = True
    else:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP  # type: ignore[attr-defined]
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "clawjournal.cli",
            "serve",
            "--port",
            str(port),
            "--no-browser",
        ],
        **kwargs,
    )


def _terminate_proc(proc: subprocess.Popen[Any] | None) -> None:
    """Best-effort reap of a server we started but can no longer reach."""
    if proc is None:
        return
    try:
        proc.terminate()
    except Exception:
        pass


def _wait_for_workbench(port: int, *, timeout_seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _workbench_responding(port, timeout=0.5):
            return True
        time.sleep(0.25)
    return _workbench_responding(port, timeout=0.5)


def launch_share_flow(
    *,
    profile: str = PROFILE_NAME,
    ui: str | None = None,
    open_browser: bool = True,
    port: int | None = None,
) -> dict[str, Any]:
    _normalize_profile(profile)
    state = load_state()
    ui_mode = _normalize_ui(ui or str(state.get("ui") or "auto"))
    config = config_module.load_config()
    resolved_port = int(port or config.get("daemon_port") or DEFAULT_PORT)
    url = f"http://localhost:{resolved_port}/share"

    if ui_mode in ("auto", "web"):
        if not _frontend_available():
            if ui_mode == "web":
                raise HookError(
                    "The ClawJournal browser workbench is not built. Re-run "
                    "`./scripts/install.sh --with-frontend` or use `--ui cli`."
                )
            return _cli_launch_result()
        # Only treat the port as serving if *our* workbench actually answers — a
        # foreign listener must not be opened or counted as already running.
        already_running = _workbench_responding(resolved_port)
        started = False
        pid: int | None = None
        proc: subprocess.Popen[Any] | None = None
        if not already_running:
            try:
                proc = _start_detached_server(resolved_port)
                started = True
                pid = proc.pid
            except OSError as exc:
                if ui_mode == "web":
                    raise HookError(f"Could not start the ClawJournal workbench: {exc}") from exc
        # A freshly started server can need more than a few seconds to bind on a
        # cold boot (imports + SQLite/FTS init). Only the first launch pays this,
        # since later ones short-circuit on `already_running`; without the longer
        # wait a slow boot silently downgrades to the CLI (the bug that made the
        # first "y" in a session land on the CLI while a later one opened the UI).
        if already_running or _wait_for_workbench(
            resolved_port, timeout_seconds=10.0 if started else 3.0
        ):
            opened = _open_browser(url) if open_browser else False
            return {
                "mode": "web",
                "url": url,
                "port": resolved_port,
                "already_running": already_running,
                "started": started,
                "pid": pid,
                "browser_opened": opened,
            }
        # We started a server but our workbench never answered on resolved_port —
        # e.g. the port was taken by something else and the daemon rebound to an
        # ephemeral port. Reap the orphan rather than leaking it, and never open
        # the foreign service that's holding the port.
        _terminate_proc(proc)
        if ui_mode == "web":
            raise HookError("Started the ClawJournal workbench, but it did not become reachable.")

    return _cli_launch_result()


def _cli_launch_result() -> dict[str, Any]:
    command = "clawjournal share --interactive --weekly"
    return {
        "mode": "cli",
        "command": command,
        "message": f"Run `{command}` to review, redact, package, and submit recent traces.",
    }


def enroll_openrefinery(
    *,
    agent: str | None = "all",
    ui: str | None = "auto",
    skip_selfupdate: bool = False,
    home: Path | None = None,
) -> dict[str, Any]:
    update_result: dict[str, Any] | None = None
    if not skip_selfupdate:
        from .selfupdate import selfupdate_sync

        update_result = dict(selfupdate_sync())
    install_result = install_profile(
        profile=PROFILE_NAME,
        agent=agent,
        ui=ui,
        source_scope="both",
        home=home,
    )
    return {
        "profile": PROFILE_NAME,
        "update": update_result,
        "install": install_result,
        "next": {
            "review": f"clawjournal hooks launch {PROFILE_NAME}",
            "status": f"clawjournal hooks status {PROFILE_NAME}",
            "project_confirmation": (
                "Open the Share workflow or run `clawjournal list --source both`, "
                "then `clawjournal config --confirm-projects` after review."
            ),
        },
    }
