from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from clawjournal import config as config_module
from clawjournal import openrefinery_hooks as hooks


@pytest.fixture
def isolated_hook_env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".clawjournal"
    monkeypatch.setattr(config_module, "CONFIG_DIR", cfg_dir)
    monkeypatch.setattr(config_module, "CONFIG_FILE", cfg_dir / "config.json")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAWJOURNAL_DISABLE_SHARE_NUDGE", raising=False)
    monkeypatch.delenv("OPENREFINERY_SHARE_HOOK_DISABLE", raising=False)
    return tmp_path


def _read(path: Path) -> dict:
    return json.loads(path.read_text())


def test_install_profile_writes_claude_and_codex_hooks(isolated_hook_env):
    home = isolated_hook_env / "home"
    claude_settings = home / ".claude" / "settings.json"
    codex_hooks = home / ".codex" / "hooks.json"
    claude_settings.parent.mkdir(parents=True)
    claude_settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "echo existing",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )

    result = hooks.install_profile(agent="all", ui="web", home=home)

    assert result["enabled"] is True
    assert result["source_scope"] == "both"
    assert result["projects_confirmed"] is False
    assert config_module.load_config()["source"] == "both"

    claude_doc = _read(claude_settings)
    claude_handlers = [
        handler
        for group in claude_doc["hooks"]["Stop"]
        for handler in group.get("hooks", [])
    ]
    assert any(handler["command"] == "echo existing" for handler in claude_handlers)
    assert any(
        "hooks run openrefinery-failures --client claude" in handler["command"]
        for handler in claude_handlers
    )

    codex_doc = _read(codex_hooks)
    codex_handlers = [
        handler
        for group in codex_doc["hooks"]["Stop"]
        for handler in group.get("hooks", [])
    ]
    assert any(
        "hooks run openrefinery-failures --client codex" in handler["command"]
        for handler in codex_handlers
    )


def test_install_profile_updates_existing_openrefinery_hook(isolated_hook_env, monkeypatch):
    home = isolated_hook_env / "home"
    codex_hooks = home / ".codex" / "hooks.json"
    codex_hooks.parent.mkdir(parents=True)
    codex_hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "old-python -m clawjournal.cli hooks run openrefinery-failures --client codex",
                                }
                            ]
                        }
                    ]
                }
            }
        )
    )
    monkeypatch.setattr(hooks.sys, "executable", "/tmp/new-python")

    result = hooks.install_profile(agent="codex", ui="auto", home=home)

    assert result["installed"][0]["changed"] is True
    doc = _read(codex_hooks)
    handlers = [
        handler
        for group in doc["hooks"]["Stop"]
        for handler in group.get("hooks", [])
    ]
    commands = [
        handler["command"]
        for handler in handlers
        if "openrefinery-failures" in handler["command"]
    ]
    assert commands == [
        "/tmp/new-python -m clawjournal.cli hooks run openrefinery-failures --client codex"
    ]


def test_daily_hook_prompts_until_daily_limit(isolated_hook_env, monkeypatch):
    hooks.install_profile(agent="codex", ui="cli", home=isolated_hook_env / "home")
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    cap = hooks.DEFAULT_MAX_PROMPTS_PER_DAY

    results = [hooks.run_hook(client="codex", now=now) for _ in range(cap + 1)]
    first = results[0]
    over_limit = results[-1]

    assert first.should_prompt is True
    assert first.reason == "prompt"
    rendered = hooks.render_hook_response(first, client="codex")
    payload = json.loads(rendered)
    assert payload["decision"] == "block"
    assert "Open local ClawJournal review" in payload["reason"]
    assert [result.should_prompt for result in results[:cap]] == [True] * cap
    assert over_limit.should_prompt is False
    assert over_limit.reason == "daily-prompt-limit-reached"

    monkeypatch.setattr(
        hooks,
        "_today",
        lambda now=None: datetime(2026, 6, 21, tzinfo=timezone.utc).date(),
    )
    status = hooks.status()
    assert status["max_prompts_per_day"] == cap
    assert status["prompts_today"] == cap


def test_reinstall_refreshes_stale_prompt_cap(isolated_hook_env):
    home = isolated_hook_env / "home"
    hooks.install_profile(agent="claude", home=home)
    # Simulate a state file written by an older version with a higher cap.
    state = hooks.load_state()
    state["max_prompts_per_day"] = 10
    hooks.save_state(state)
    assert hooks.status()["max_prompts_per_day"] == 10

    hooks.install_profile(agent="claude", home=home)

    assert hooks.status()["max_prompts_per_day"] == hooks.DEFAULT_MAX_PROMPTS_PER_DAY


def test_dry_run_previews_without_consuming(isolated_hook_env):
    hooks.install_profile(agent="claude", ui="cli", home=isolated_hook_env / "home")
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    preview = hooks.run_hook(client="claude", dry_run=True, now=now)

    assert preview.should_prompt is True
    assert preview.reason == "dry-run"
    assert "OpenRefinery Agent Failure Sharing" in (preview.message or "")
    # A preview must not consume a slot: a real run still treats today as fresh.
    assert hooks.status(now=now)["prompts_today"] == 0
    follow = hooks.run_hook(client="claude", now=now)
    assert follow.should_prompt is True
    assert follow.reason == "prompt"


def test_stop_hook_active_suppresses_without_consuming(isolated_hook_env):
    hooks.install_profile(agent="claude", ui="cli", home=isolated_hook_env / "home")
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    guarded = hooks.run_hook(client="claude", stop_hook_active=True, now=now)
    assert guarded.should_prompt is False
    assert guarded.reason == "stop-hook-active"
    assert hooks.status(now=now)["prompts_today"] == 0

    # --force overrides the loop guard.
    forced = hooks.run_hook(client="claude", stop_hook_active=True, force=True, now=now)
    assert forced.should_prompt is True


def test_status_reports_readiness_and_next_prompt(isolated_hook_env):
    hooks.install_profile(agent="claude", ui="cli", home=isolated_hook_env / "home")
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)

    st = hooks.status(now=now)
    assert st["eligible_now"] is True
    assert st["next_prompt"] == "2026-06-21"
    assert st["source_confirmed"] is True  # install set source=both
    assert st["projects_confirmed"] is False
    assert st["share_ready"] is False

    hooks.snooze_profile(days=3, now=now)
    st2 = hooks.status(now=now)
    assert st2["snoozed"] is True
    assert st2["snooze_days_remaining"] == 4  # today + 3 days, inclusive of today
    assert st2["next_prompt"] == "2026-06-25"  # snooze_until (06-24) + 1 day
    assert st2["eligible_now"] is False


def test_share_readiness_tracks_config():
    assert hooks.share_readiness({"source": "auto"})["source_confirmed"] is False
    assert hooks.share_readiness({"source": "both"})["source_confirmed"] is True
    ready = hooks.share_readiness({"source": "both", "projects_confirmed": True})
    assert ready["ready"] is True
    assert hooks.share_readiness({})["ready"] is False


def test_snooze_suppresses_daily_hook(isolated_hook_env):
    hooks.install_profile(agent="claude", ui="cli", home=isolated_hook_env / "home")
    now = datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc)
    hooks.snooze_profile(days=3, now=now)

    result = hooks.run_hook(client="claude", now=now)

    assert result.should_prompt is False
    assert result.reason == "snoozed"


def test_claude_hook_response_includes_additional_context(isolated_hook_env):
    hooks.install_profile(agent="claude", ui="cli", home=isolated_hook_env / "home")
    result = hooks.run_hook(
        client="claude",
        now=datetime(2026, 6, 21, 12, 0, tzinfo=timezone.utc),
    )

    payload = json.loads(hooks.render_hook_response(result, client="claude"))

    assert "decision" not in payload
    assert payload["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert "OpenRefinery Agent Failure Sharing" in payload["hookSpecificOutput"]["additionalContext"]


def test_launch_share_flow_falls_back_to_cli_when_auto_web_unavailable(
    isolated_hook_env, monkeypatch
):
    hooks.install_profile(agent="codex", ui="auto", home=isolated_hook_env / "home")
    monkeypatch.setattr(hooks, "_frontend_available", lambda: True)
    monkeypatch.setattr(hooks, "_port_is_open", lambda port: False)
    monkeypatch.setattr(
        hooks,
        "_start_detached_server",
        lambda port: (_ for _ in ()).throw(OSError("no server")),
    )

    result = hooks.launch_share_flow(open_browser=False)

    assert result["mode"] == "cli"
    assert result["command"] == "clawjournal share --interactive --weekly"


def test_launch_share_flow_starts_server_and_opens_browser(isolated_hook_env, monkeypatch):
    hooks.install_profile(agent="codex", ui="web", home=isolated_hook_env / "home")

    class Proc:
        pid = 1234

    calls: list[str] = []
    monkeypatch.setattr(hooks, "_frontend_available", lambda: True)
    monkeypatch.setattr(hooks, "_port_is_open", lambda port: False)
    monkeypatch.setattr(hooks, "_start_detached_server", lambda port: Proc())
    monkeypatch.setattr(hooks, "_wait_for_port", lambda port: True)
    monkeypatch.setattr(hooks, "_open_browser", lambda url: calls.append(url) or True)

    result = hooks.launch_share_flow(open_browser=True, port=8484)

    assert result["mode"] == "web"
    assert result["started"] is True
    assert result["pid"] == 1234
    assert result["url"] == "http://localhost:8484/share"
    assert calls == ["http://localhost:8484/share"]


def test_launch_share_flow_falls_back_when_frontend_missing(isolated_hook_env, monkeypatch):
    hooks.install_profile(agent="codex", ui="auto", home=isolated_hook_env / "home")
    monkeypatch.setattr(hooks, "_frontend_available", lambda: False)

    result = hooks.launch_share_flow(open_browser=False)

    assert result["mode"] == "cli"


def test_uninstall_profile_removes_only_openrefinery_hook(isolated_hook_env):
    home = isolated_hook_env / "home"
    hooks.install_profile(agent="codex", ui="auto", home=home)
    codex_hooks = home / ".codex" / "hooks.json"
    doc = _read(codex_hooks)
    doc["hooks"]["Stop"].append(
        {"hooks": [{"type": "command", "command": "echo keep-me"}]}
    )
    codex_hooks.write_text(json.dumps(doc))

    hooks.uninstall_profile(agent="codex", home=home)

    doc = _read(codex_hooks)
    handlers = [
        handler
        for group in doc["hooks"]["Stop"]
        for handler in group.get("hooks", [])
    ]
    assert any(handler["command"] == "echo keep-me" for handler in handlers)
    assert not any("openrefinery-failures" in handler["command"] for handler in handlers)
