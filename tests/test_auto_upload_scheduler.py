import json
import subprocess

from clawjournal import auto_upload_scheduler as scheduler


def test_install_preserves_unrelated_hooks_and_uses_pascal_case(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude" / "settings.json"
    codex = home / ".codex" / "hooks.json"
    claude.parent.mkdir(parents=True)
    codex.parent.mkdir(parents=True)
    existing = {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "keep"}]}]}}
    claude.write_text(json.dumps(existing), encoding="utf-8")
    codex.write_text(json.dumps(existing), encoding="utf-8")

    result = scheduler.install(home=home)
    assert result["scheduler"] == "SessionStart"
    for path in (claude, codex):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["hooks"]["Stop"] == existing["hooks"]["Stop"]
        handlers = payload["hooks"]["SessionStart"][0]["hooks"]
        assert handlers[0]["clawjournalProfile"] == scheduler.HOOK_MARKER


def test_remove_deletes_only_automatic_hook(tmp_path):
    home = tmp_path / "home"
    scheduler.install(home=home)
    path = home / ".codex" / "hooks.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["hooks"]["SessionStart"].append(
        {"hooks": [{"type": "command", "command": "keep-session-start"}]}
    )
    path.write_text(json.dumps(payload), encoding="utf-8")
    scheduler.remove(home=home)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["hooks"]["SessionStart"] == [
        {"hooks": [{"type": "command", "command": "keep-session-start"}]}
    ]


def test_hook_spawns_detached_worker_only_when_due(monkeypatch):
    class Connection:
        def close(self):
            pass

    calls = []
    monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: Connection())
    monkeypatch.setattr("clawjournal.auto_upload.get_enrollment", lambda conn: {"state": "enabled"})
    monkeypatch.setattr("clawjournal.auto_upload.is_due_local", lambda conn: True)
    monkeypatch.setattr(
        scheduler.subprocess, "Popen",
        lambda command, **kwargs: calls.append((command, kwargs)) or subprocess.CompletedProcess(command, 0),
    )
    result = scheduler.spawn_if_due("codex")
    assert result["spawned"] is True
    assert calls[0][0][-2:] == ["run", "--scheduled"]
    assert calls[0][1]["stdout"] is subprocess.DEVNULL
