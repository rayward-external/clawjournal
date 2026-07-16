from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clawjournal import agent_hooks as hooks


@pytest.fixture
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return tmp_path / "home"


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def _handlers(document: dict, event: str) -> list[dict]:
    return [
        handler
        for group in document.get("hooks", {}).get(event, [])
        if isinstance(group, dict)
        for handler in group.get("hooks", [])
        if isinstance(handler, dict)
    ]


def test_explicit_home_wins_over_environment_overrides(tmp_path, monkeypatch):
    """A passed home= must never be redirected into a real config by env vars.

    Without this precedence, running the suite with CLAUDE_CONFIG_DIR or
    CODEX_HOME set would write test hooks into the developer's real settings.
    """
    env_dir = tmp_path / "env-config"
    home = tmp_path / "home"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_dir / "claude"))
    monkeypatch.setenv("CODEX_HOME", str(env_dir / "codex"))

    results = hooks.install_hooks(agent="all", home=home)

    assert [result["agent"] for result in results] == ["claude", "codex"]
    assert (home / ".claude" / "settings.json").exists()
    assert (home / ".codex" / "hooks.json").exists()
    assert not env_dir.exists()

    # Without home=, the env overrides still direct real installs.
    assert hooks._claude_settings_path() == env_dir / "claude" / "settings.json"
    assert hooks._codex_hooks_path() == env_dir / "codex" / "hooks.json"


def test_install_all_uses_session_start_and_preserves_existing_hooks(isolated_home):
    claude_path = isolated_home / ".claude" / "settings.json"
    claude_path.parent.mkdir(parents=True)
    existing = {
        "permissions": {"allow": ["Bash(pytest:*)"]},
        "hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": "echo keep"}]}],
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": (
                                "python -m clawjournal.cli hooks run "
                                "openrefinery-failures --client claude"
                            ),
                        }
                    ]
                }
            ],
        },
    }
    claude_path.write_text(json.dumps(existing))

    results = hooks.install_hooks(agent="all", home=isolated_home)

    assert [result["agent"] for result in results] == ["claude", "codex"]
    claude = _read(claude_path)
    assert claude["permissions"] == existing["permissions"]
    assert any(handler["command"] == "echo keep" for handler in _handlers(claude, "SessionStart"))
    assert any("openrefinery-failures" in handler["command"] for handler in _handlers(claude, "Stop"))
    own = [handler for handler in _handlers(claude, "SessionStart") if hooks._handler_is_ours(handler)]
    assert own == [hooks._handler_for("claude")]

    codex_path = isolated_home / ".codex" / "hooks.json"
    codex = _read(codex_path)
    own = [handler for handler in _handlers(codex, "SessionStart") if hooks._handler_is_ours(handler)]
    assert own == [hooks._handler_for("codex")]
    assert "commandWindows" in own[0]
    assert "Stop" not in codex["hooks"]


def test_install_is_idempotent_and_collapses_stale_duplicates(isolated_home, monkeypatch):
    codex_path = isolated_home / ".codex" / "hooks.json"
    codex_path.parent.mkdir(parents=True)
    stale = {
        "type": "command",
        "command": "old-python -m clawjournal.agent_hooks run --client codex",
    }
    codex_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {"hooks": [stale, {"type": "command", "command": "echo keep"}]},
                        {"hooks": [stale]},
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(hooks.sys, "executable", "/tmp/current-python")

    first = hooks.install_agent_hook("codex", home=isolated_home)
    first_bytes = codex_path.read_bytes()
    second = hooks.install_agent_hook("codex", home=isolated_home)

    assert first["changed"] is True
    assert second["changed"] is False
    assert codex_path.read_bytes() == first_bytes
    document = _read(codex_path)
    assert sum(hooks._handler_is_ours(handler) for handler in _handlers(document, "SessionStart")) == 1
    assert any(handler.get("command") == "echo keep" for handler in _handlers(document, "SessionStart"))


def test_uninstall_removes_only_auto_upload_hook_and_is_idempotent(isolated_home):
    path = isolated_home / ".codex" / "hooks.json"
    hooks.install_agent_hook("codex", home=isolated_home)
    document = _read(path)
    document["hooks"]["SessionStart"].append(
        {"matcher": "startup", "hooks": [{"type": "command", "command": "echo keep"}]}
    )
    document["hooks"]["Stop"] = [
        {"hooks": [{"type": "command", "command": "openrefinery-failures"}]}
    ]
    path.write_text(json.dumps(document))

    first = hooks.uninstall_agent_hook("codex", home=isolated_home)
    second = hooks.uninstall_agent_hook("codex", home=isolated_home)

    assert first["changed"] is True
    assert second["changed"] is False
    document = _read(path)
    assert not any(hooks._handler_is_ours(handler) for handler in _handlers(document, "SessionStart"))
    assert any(handler.get("command") == "echo keep" for handler in _handlers(document, "SessionStart"))
    assert _handlers(document, "Stop")[0]["command"] == "openrefinery-failures"


def test_malformed_hook_shape_is_rejected_without_rewrite(isolated_home):
    path = isolated_home / ".claude" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks": {"SessionStart": {"bad": true}}}\n')
    before = path.read_bytes()

    with pytest.raises(hooks.AgentHookError, match="must be a JSON array"):
        hooks.install_agent_hook("claude", home=isolated_home)

    assert path.read_bytes() == before


def test_multi_agent_install_rolls_back_first_file_when_second_is_invalid(isolated_home):
    claude_path = isolated_home / ".claude" / "settings.json"
    codex_path = isolated_home / ".codex" / "hooks.json"
    claude_path.parent.mkdir(parents=True)
    codex_path.parent.mkdir(parents=True)
    claude_path.write_text('{"custom": true}\n')
    codex_path.write_text('{"hooks": {"SessionStart": {"bad": true}}}\n')
    claude_before = claude_path.read_bytes()
    codex_before = codex_path.read_bytes()

    with pytest.raises(hooks.AgentHookError, match="must be a JSON array"):
        hooks.install_hooks(agent="all", home=isolated_home)

    assert claude_path.read_bytes() == claude_before
    assert codex_path.read_bytes() == codex_before


def test_diagnostics_distinguishes_installed_from_current_and_reports_observation(
    isolated_home, monkeypatch
):
    path = isolated_home / ".codex" / "hooks.json"
    hooks.install_agent_hook("codex", home=isolated_home)

    configured = hooks.hook_diagnostics("codex", home=isolated_home)
    assert configured["configured"] is True
    assert "awaiting hook trust" in configured["diagnostic"]

    observed_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    observed = hooks.hook_diagnostics(
        "codex", home=isolated_home, last_observed_at=observed_at
    )
    assert observed["last_observed_at"] == observed_at.isoformat()
    assert "diagnostic" not in observed

    monkeypatch.setattr(hooks.sys, "executable", "/new/python")
    stale = hooks.hook_diagnostics("codex", home=isolated_home)
    assert stale["installed"] is True
    assert stale["configured"] is False
    assert path.exists()


def test_session_start_records_observation_and_spawns_only_when_due():
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    calls: list[tuple[str, str]] = []

    not_due = hooks.run_session_start(
        client="claude",
        now=now,
        record_observed=lambda client, at: calls.append(("observed", client)),
        due_check=lambda client, at: hooks.DueDecision(False, "not-due"),
        spawn_runner=lambda client: calls.append(("spawned", client)) or True,
    )
    assert not_due.reason == "not-due"
    assert not_due.observation_recorded is True
    assert calls == [("observed", "claude")]

    due = hooks.run_session_start(
        client="codex",
        now=now,
        due_check=lambda client, at: hooks.DueDecision(True, "due"),
        spawn_runner=lambda client: calls.append(("spawned", client)) or True,
    )
    assert due.reason == "runner-spawned"
    assert due.spawned is True
    assert calls[-1] == ("spawned", "codex")


def test_session_start_adapter_failures_never_escape():
    def fail(*args):
        raise RuntimeError("boom")

    due_error = hooks.run_session_start(client="claude", due_check=fail)
    assert due_error.reason == "due-check-error"

    invalid_result = hooks.run_session_start(
        client="claude", due_check=lambda client, at: True
    )
    assert invalid_result.reason == "due-check-error"

    spawn_error = hooks.run_session_start(
        client="claude",
        record_observed=fail,
        due_check=lambda client, at: hooks.DueDecision(True, "due"),
        spawn_runner=fail,
    )
    assert spawn_error.reason == "runner-spawn-error"
    assert spawn_error.observation_recorded is False


def test_module_entrypoint_is_quiet_and_inert_by_default(capsys):
    assert hooks.main(
        ["run", "--client", "codex"], due_check=hooks._runner_not_configured
    ) == 0
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == ""


def test_detached_process_options_do_not_inherit_stdio():
    posix = hooks.detached_process_kwargs(platform="posix")
    assert posix["start_new_session"] is True
    assert "creationflags" not in posix
    assert posix["stdin"] is hooks.subprocess.DEVNULL

    windows = hooks.detached_process_kwargs(platform="nt")
    assert windows["creationflags"] == 0x00000200 | 0x00000008
    assert "start_new_session" not in windows
