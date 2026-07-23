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


@pytest.mark.parametrize("output_json", [False, True])
def test_preview_refresh_reports_lock_wait_without_breaking_json(
    monkeypatch, capsys, output_json
):
    def preview(*, refresh, scan_wait_notice):
        assert refresh is True
        scan_wait_notice()
        return {
            "ok": True,
            "selected": [],
            "eligible_count": 0,
            "selected_count": 0,
            "deferred_by_cap": 0,
        }

    monkeypatch.setattr("clawjournal.auto_upload.preview", preview)
    argv = ["clawjournal", "auto-upload", "preview", "--refresh"]
    if output_json:
        argv.append("--json")
    monkeypatch.setattr(sys, "argv", argv)

    main()

    captured = capsys.readouterr()
    if output_json:
        assert json.loads(captured.out)["ok"] is True
        assert captured.err == ""
    else:
        assert "Waiting for another scan to finish" in captured.err


def test_noninteractive_enable_requires_exact_versions(monkeypatch, capsys):
    challenge = {
        "ok": False,
        "status": 409,
        "code": "authorization_required",
        "message": "review",
        "authorization_profile_hash": "profile-sha256",
        "authorization": {"version": "auth-v1", "text": "future uploads"},
        "retention": {"version": "ret-v1", "text": "retention"},
        "ownership_certification": {"version": "own-v1", "text": "ownership"},
        "scope": {
            "sources": ["codex"],
            "projects": ["project"],
            "entries": [["codex", "project"]],
        },
        "ai": {"enabled": False, "backend": None},
        "cap": 5,
        "cadence_days": 1,
        "maximum_bundle_size": 5_000_000,
    }
    monkeypatch.setattr("clawjournal.auto_upload.enable", lambda **kwargs: challenge)
    monkeypatch.setattr(sys, "argv", ["clawjournal", "auto-upload", "enable", "--json"])

    with pytest.raises(SystemExit, match="1"):
        main()

    assert json.loads(capsys.readouterr().out)["code"] == "authorization_required"



class _FakeStdin:
    def isatty(self):
        return True


def test_sanitize_terminal_defangs_ansi_but_keeps_tab_newline():
    from clawjournal.cli_auto_upload import _sanitize_terminal, _sanitize_terminal_line

    assert "\x1b" not in _sanitize_terminal("x\x1b[2Ky")
    assert "\x07" not in _sanitize_terminal("a\x07b")
    assert _sanitize_terminal("a\tb\nc") == "a\tb\nc"
    assert _sanitize_terminal("a\rb") == "ab"
    assert _sanitize_terminal_line("a\tb\nc\u2028d\u2029e") == "a b c d e"


def test_human_output_sanitizes_all_dynamic_single_line_fields(capsys):
    import clawjournal.cli_auto_upload as cli

    attack = "value\x1b[2J\nforged\x07"
    cli._print_human({"ok": False, "code": attack, "message": attack})
    cli._print_human({
        "ok": True,
        "mode": attack,
        "health": attack,
        "overlay": attack,
        "scope": {"sources": [attack], "projects": [attack]},
        "next_due_at": attack,
        "next_retry_at": attack,
        "hooks": [{"agent": attack, "legacy_hook_installed": True}],
        "eligibility": {"eligible_count": 1, "selected_count": 1},
    })
    cli._print_human({
        "ok": True,
        "code": attack,
        "count": 1,
        "receipt_reference": attack,
    })

    captured = capsys.readouterr()
    assert "\x1b" not in captured.out + captured.err
    assert "\x07" not in captured.out + captured.err
    assert "Receipt: value[2J forged" in captured.out


def test_human_status_surfaces_legacy_hook_migration_hint(capsys):
    import clawjournal.cli_auto_upload as cli

    cli._print_human({
        "ok": True,
        "mode": "enabled",
        "health": "ready",
        "hooks": [
            {"agent": "claude", "legacy_hook_installed": False},
            {"agent": "codex", "legacy_hook_installed": True},
        ],
        "eligibility": {},
    })

    out = capsys.readouterr().out
    assert "Legacy pre-release hook installed for: codex" in out
    assert "'clawjournal auto-upload enable'" in out


def test_interactive_challenge_sanitizes_versions_scope_and_backend(
    monkeypatch, capsys
):
    import clawjournal.cli_auto_upload as cli

    attack = "value\x1b[2J\nforged\x07"
    challenge = {
        "authorization_profile_hash": "profile-sha256",
        "authorization": {"version": attack, "text": attack},
        "retention": {"version": attack, "text": attack},
        "ownership_certification": {"version": attack, "text": attack},
        "scope": {
            "sources": [attack],
            "projects": [attack],
            "entries": [[attack, attack]],
        },
        "ai": {"enabled": True, "backend": attack},
        "cap": 5,
        "cadence_days": 1,
        "maximum_bundle_size": 5_000_000,
        "destination_origin": attack,
    }
    prompts = []

    def answer(prompt):
        prompts.append(prompt)
        return attack

    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin())
    monkeypatch.setattr("builtins.input", answer)

    assert cli._interactive_accept(None, challenge) == (
        attack,
        attack,
        attack,
        "profile-sha256",
    )
    output = capsys.readouterr().out
    assert "\x1b" not in output + "".join(prompts)
    assert "\x07" not in output + "".join(prompts)
    assert "Exact authorized source/project pairs:" in output
    assert "cadence: 1 day" in output
    assert "cadence: 1 days" not in output
    assert all("\n" not in prompt for prompt in prompts)


def test_fresh_email_verification_reports_clean_error(monkeypatch, capsys):
    import clawjournal.cli_auto_upload as cli

    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin())
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: "student@uni.edu")

    def bad_email(_email):
        raise ValueError("Enter a valid academic email address.")

    monkeypatch.setattr(
        "clawjournal.workbench.daemon.request_email_verification", bad_email
    )

    assert cli._fresh_email_verification() is False
    assert "Verification did not complete" in capsys.readouterr().err


def test_interactive_enable_replays_exact_profile_after_email_verification(
    monkeypatch, capsys
):
    challenge = {
        "ok": False,
        "status": 409,
        "code": "authorization_required",
        "message": "review",
        "authorization_profile_hash": "profile-sha256",
        "authorization": {"version": "auth-v1", "text": "future uploads"},
        "retention": {"version": "ret-v1", "text": "retention"},
        "ownership_certification": {"version": "own-v1", "text": "ownership"},
        "scope": {
            "sources": ["codex"],
            "projects": ["project"],
            "entries": [["codex", "project"]],
        },
        "ai": {"enabled": False, "backend": None},
        "cap": 5,
        "cadence_days": 1,
        "maximum_bundle_size": 5_000_000,
    }
    calls = []

    def enable(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return challenge
        if len(calls) == 2:
            return {
                "ok": False,
                "code": "email_verification_required",
                "message": "verify",
            }
        return {"ok": True, "mode": "enabled", "health": "ready"}

    answers = iter(["auth-v1", "ret-v1", "own-v1", "student@uni.edu"])
    monkeypatch.setattr("clawjournal.auto_upload.enable", enable)
    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    monkeypatch.setattr("getpass.getpass", lambda *_a, **_k: "123456")
    monkeypatch.setattr(
        "clawjournal.workbench.daemon.request_email_verification",
        lambda _email: None,
    )
    monkeypatch.setattr(
        "clawjournal.workbench.daemon.confirm_pending_email_verification",
        lambda _code: None,
    )
    monkeypatch.setattr(sys, "argv", ["clawjournal", "auto-upload", "enable"])

    main()

    for call in calls:
        assert callable(call.pop("scan_progress"))
        assert callable(call.pop("scan_wait_notice"))
    accepted = {
        "agent": "auto",
        "accepted_authorization_version": "auth-v1",
        "accepted_retention_version": "ret-v1",
        "accepted_ownership_certification_version": "own-v1",
        "accepted_authorization_profile_hash": "profile-sha256",
    }
    assert calls == [
        {
            "agent": "auto",
            "accepted_authorization_version": None,
            "accepted_retention_version": None,
            "accepted_ownership_certification_version": None,
            "accepted_authorization_profile_hash": None,
        },
        accepted,
        accepted,
    ]
    assert "Automatic upload: enabled / ready" in capsys.readouterr().out


def test_interactive_enable_reprompts_once_when_the_refresh_changes_scope(
    monkeypatch, capsys
):
    """The accepting call's refresh can reveal a new project; the CLI must
    re-display the refreshed challenge for one more acceptance round instead
    of failing the enrollment."""

    def challenge(profile_hash: str, projects: list[str]) -> dict:
        return {
            "ok": False,
            "status": 409,
            "code": "authorization_required",
            "message": "review",
            "authorization_profile_hash": profile_hash,
            "authorization": {"version": "auth-v1", "text": "future uploads"},
            "retention": {"version": "ret-v1", "text": "retention"},
            "ownership_certification": {"version": "own-v1", "text": "ownership"},
            "scope": {
                "sources": ["codex"],
                "projects": projects,
                "entries": [["codex", project] for project in projects],
            },
            "ai": {"enabled": False, "backend": None},
            "cap": 5,
            "cadence_days": 1,
            "maximum_bundle_size": 5_000_000,
        }

    calls = []

    def enable(**kwargs):
        kwargs.pop("scan_progress", None)
        kwargs.pop("scan_wait_notice", None)
        calls.append(kwargs)
        if len(calls) == 1:
            return challenge("profile-one", ["project"])
        if len(calls) == 2:
            return challenge("profile-two", ["project", "project-two"])
        return {"ok": True, "mode": "enabled", "health": "ready"}

    answers = iter(
        ["auth-v1", "ret-v1", "own-v1", "auth-v1", "ret-v1", "own-v1"]
    )
    monkeypatch.setattr("clawjournal.auto_upload.enable", enable)
    monkeypatch.setattr(sys, "stdin", _FakeStdin())
    monkeypatch.setattr("builtins.input", lambda *_a, **_k: next(answers))
    monkeypatch.setattr(sys, "argv", ["clawjournal", "auto-upload", "enable"])

    main()

    assert [
        call["accepted_authorization_profile_hash"] for call in calls
    ] == [None, "profile-one", "profile-two"]
    out = capsys.readouterr().out
    assert "project-two" in out
    assert "Automatic upload: enabled / ready" in out


def test_interactive_accept_handles_keyboard_interrupt(monkeypatch):
    import clawjournal.cli_auto_upload as cli

    challenge = {
        "authorization_profile_hash": "profile-sha256",
        "authorization": {"version": "auth-v1", "text": "future uploads"},
        "retention": {"version": "ret-v1", "text": "retention"},
        "ownership_certification": {"version": "own-v1", "text": "ownership"},
        "scope": {
            "sources": ["codex"],
            "projects": ["project"],
            "entries": [["codex", "project"]],
        },
        "ai": {"enabled": False, "backend": None},
        "cap": 5,
        "cadence_days": 1,
        "maximum_bundle_size": 5_000_000,
    }
    monkeypatch.setattr(cli.sys, "stdin", _FakeStdin())
    monkeypatch.setattr(
        "builtins.input",
        lambda *_a, **_k: (_ for _ in ()).throw(KeyboardInterrupt()),
    )

    assert cli._interactive_accept(None, challenge) is None
