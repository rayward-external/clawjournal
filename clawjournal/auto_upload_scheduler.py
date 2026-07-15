"""Claude Code and Codex SessionStart hooks for recurring sharing."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

HOOK_MARKER = "clawjournal-auto-upload-v1"
SUPPORTED_AGENTS = ("claude", "codex")


def _home(home: Path | None = None) -> Path:
    return (home or Path.home()).expanduser()


def _path(agent: str, home: Path | None = None) -> Path:
    if agent == "claude":
        override = os.environ.get("CLAUDE_CONFIG_DIR")
        return (Path(override).expanduser() if override else _home(home) / ".claude") / "settings.json"
    override = os.environ.get("CODEX_HOME")
    return (Path(override).expanduser() if override else _home(home) / ".codex") / "hooks.json"


def _argv(client: str, *, worker: bool = False) -> list[str]:
    args = [sys.executable, "-m", "clawjournal.cli", "auto-upload"]
    return args + (["run", "--scheduled"] if worker else ["hook", "--client", client])


def _handler(client: str) -> dict[str, Any]:
    handler: dict[str, Any] = {
        "type": "command",
        "command": shlex.join(_argv(client)),
        "timeout": 5,
        "statusMessage": "Checking automatic sharing schedule",
        "clawjournalProfile": HOOK_MARKER,
    }
    if client == "codex":
        handler["commandWindows"] = subprocess.list2cmdline(_argv(client))
    return handler


def _read(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Hook config must contain a JSON object: {path}")
    return payload


def _write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _ours(value: Any) -> bool:
    return isinstance(value, dict) and value.get("clawjournalProfile") == HOOK_MARKER


def _upsert(payload: dict[str, Any], client: str) -> bool:
    hooks = payload.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks must be a JSON object.")
    groups = hooks.setdefault("SessionStart", [])
    if not isinstance(groups, list):
        raise ValueError("Existing SessionStart hooks must be a JSON array.")
    desired = _handler(client)
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            continue
        for index, handler in enumerate(group["hooks"]):
            if _ours(handler):
                if handler == desired:
                    return False
                group["hooks"][index] = desired
                return True
    groups.append({"hooks": [desired]})
    return True


def _remove(payload: dict[str, Any]) -> bool:
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict) or not isinstance(hooks.get("SessionStart"), list):
        return False
    changed = False
    groups = []
    for group in hooks["SessionStart"]:
        if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
            groups.append(group)
            continue
        handlers = [handler for handler in group["hooks"] if not _ours(handler)]
        changed |= len(handlers) != len(group["hooks"])
        if handlers:
            groups.append({**group, "hooks": handlers})
    if changed:
        hooks["SessionStart"] = groups
    return changed


def install(*, home: Path | None = None) -> dict[str, Any]:
    installed = []
    for agent in SUPPORTED_AGENTS:
        path = _path(agent, home)
        payload = _read(path)
        changed = _upsert(payload, agent)
        if changed or not path.exists():
            _write(path, payload)
        installed.append({"agent": agent, "path": str(path), "changed": changed})
    return {"scheduler": "SessionStart", "state": "installed", "agents": installed}


def remove(*, home: Path | None = None) -> dict[str, Any]:
    removed = []
    for agent in SUPPORTED_AGENTS:
        path = _path(agent, home)
        changed = False
        if path.exists():
            payload = _read(path)
            changed = _remove(payload)
            if changed:
                _write(path, payload)
        removed.append({"agent": agent, "path": str(path), "changed": changed})
    return {"scheduler": "SessionStart", "state": "removed", "agents": removed}


def hook_status(*, home: Path | None = None) -> dict[str, bool]:
    result = {}
    for agent in SUPPORTED_AGENTS:
        payload = _read(_path(agent, home))
        groups = payload.get("hooks", {}).get("SessionStart", []) if isinstance(payload.get("hooks"), dict) else []
        result[agent] = any(
            _ours(handler)
            for group in groups if isinstance(group, dict)
            for handler in group.get("hooks", []) if isinstance(group.get("hooks"), list)
        )
    return result


def spawn_if_due(client: str) -> dict[str, Any]:
    from .auto_upload import get_enrollment, is_due_local
    from .workbench.index import open_index

    conn = open_index()
    try:
        enrollment = get_enrollment(conn)
        due = is_due_local(conn)
    finally:
        conn.close()
    if enrollment is None or enrollment.get("state") != "enabled" or not due:
        return {"spawned": False, "reason": "not_due"}
    kwargs: dict[str, Any] = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    subprocess.Popen(_argv(client, worker=True), **kwargs)
    return {"spawned": True, "client": client}
