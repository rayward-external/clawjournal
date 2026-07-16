from __future__ import annotations

import json
import sys

import pytest

from clawjournal.cli import main


def test_auto_upload_status_json_is_pure_stdout(monkeypatch, capsys):
    monkeypatch.setattr(
        "clawjournal.auto_upload.status",
        lambda: {"ok": True, "mode": "off", "health": "ready", "overlay": None},
    )
    monkeypatch.setattr(sys, "argv", ["clawjournal", "auto-upload", "status", "--json"])

    main()

    captured = capsys.readouterr()
    assert json.loads(captured.out)["mode"] == "off"
    assert captured.err == ""


def test_noninteractive_enable_requires_exact_versions(monkeypatch, capsys):
    challenge = {
        "ok": False,
        "status": 409,
        "code": "authorization_required",
        "message": "review",
        "authorization": {"version": "auth-v1", "text": "future uploads"},
        "retention": {"version": "ret-v1", "text": "retention"},
        "scope": {"sources": ["codex"], "projects": ["project"]},
        "ai": {"enabled": False, "backend": None},
        "cap": 5,
        "cadence_days": 7,
    }
    monkeypatch.setattr("clawjournal.auto_upload.enable", lambda **kwargs: challenge)
    monkeypatch.setattr(sys, "argv", ["clawjournal", "auto-upload", "enable", "--json"])

    with pytest.raises(SystemExit, match="1"):
        main()

    assert json.loads(capsys.readouterr().out)["code"] == "authorization_required"
