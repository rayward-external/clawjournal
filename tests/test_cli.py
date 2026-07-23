"""Tests for clawjournal.cli — CLI commands and helpers."""

import json
import shutil
import sys
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clawjournal.cli import (
    _build_status_next_steps,
    _collect_review_attestations,
    _format_size,
    _format_token_count,
    _has_session_sources,
    _merge_config_list,
    _normalize_score_source_filter,
    _parse_csv_arg,
    _scan_for_text_occurrences,
    _scan_high_entropy_strings,
    _scan_pii,
    _share_pii_status,
    _share_preview,
    configure,
    export_to_jsonl,
    list_projects,
    main,
)


# --- _format_size ---


class TestFormatSize:
    def test_bytes(self):
        assert _format_size(500) == "500 B"

    def test_kilobytes(self):
        result = _format_size(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = _format_size(5 * 1024 * 1024)
        assert "MB" in result

    def test_gigabytes(self):
        result = _format_size(2 * 1024 * 1024 * 1024)
        assert "GB" in result

    def test_zero(self):
        assert _format_size(0) == "0 B"

    def test_exactly_1024(self):
        result = _format_size(1024)
        assert "KB" in result


# --- _format_token_count ---


class TestFormatTokenCount:
    def test_plain(self):
        assert _format_token_count(500) == "500"

    def test_thousands(self):
        result = _format_token_count(5000)
        assert result == "5K"

    def test_millions(self):
        result = _format_token_count(2_500_000)
        assert "M" in result

    def test_billions(self):
        result = _format_token_count(1_500_000_000)
        assert "B" in result

    def test_zero(self):
        assert _format_token_count(0) == "0"


# --- attestation helpers ---


class TestAttestationHelpers:
    def test_collect_review_attestations_valid(self):
        attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name=(
                "I asked Jane Doe for their full name and scanned the export for Jane Doe."
            ),
            attest_asked_sensitive=(
                "I asked about company, client, and internal names plus URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name="Jane Doe",
        )
        assert not errors
        assert manual_count == 20
        assert "Jane Doe" in attestations["asked_full_name"]

    def test_collect_review_attestations_invalid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name="scanned quickly",
            attest_asked_sensitive="checked stuff",
            attest_manual_scan="manual scan of 5 sessions",
            full_name="Jane Doe",
        )
        assert errors
        assert "asked_full_name" in errors
        assert "asked_sensitive_entities" in errors
        assert "manual_scan_done" in errors
        assert manual_count == 5

    def test_collect_review_attestations_skip_full_name_valid(self):
        _attestations, errors, manual_count = _collect_review_attestations(
            attest_asked_full_name=(
                "User declined to share full name; skipped exact-name scan."
            ),
            attest_asked_sensitive=(
                "I asked about company/client/internal names and private URLs; "
                "none were sensitive and no extra redactions were needed."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name=None,
            skip_full_name_scan=True,
        )
        assert not errors
        assert manual_count == 20

    def test_collect_review_attestations_skip_full_name_invalid(self):
        _attestations, errors, _manual_count = _collect_review_attestations(
            attest_asked_full_name="Asked user and scanned it.",
            attest_asked_sensitive=(
                "I asked about company/client/internal names and private URLs; none found."
            ),
            attest_manual_scan=(
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end."
            ),
            full_name=None,
            skip_full_name_scan=True,
        )
        assert "asked_full_name" in errors

    def test_scan_for_text_occurrences(self, tmp_path):
        f = tmp_path / "sample.jsonl"
        f.write_text('{"message":"Jane Doe says hi"}\n{"message":"nothing here"}\n')
        result = _scan_for_text_occurrences(f, "Jane Doe")
        assert result["match_count"] == 1


# --- _parse_csv_arg ---


class TestParseCsvArg:
    def test_none(self):
        assert _parse_csv_arg(None) is None

    def test_empty(self):
        assert _parse_csv_arg("") is None

    def test_single(self):
        assert _parse_csv_arg("foo") == ["foo"]

    def test_comma_separated(self):
        assert _parse_csv_arg("foo, bar, baz") == ["foo", "bar", "baz"]

    def test_strips_whitespace(self):
        assert _parse_csv_arg("  a ,  b  ") == ["a", "b"]

    def test_empty_items_filtered(self):
        assert _parse_csv_arg("a,,b,") == ["a", "b"]


# --- _merge_config_list ---


class TestMergeConfigList:
    def test_merge_new_values(self):
        config = {"items": ["a", "b"]}
        _merge_config_list(config, "items", ["c", "d"])
        assert sorted(config["items"]) == ["a", "b", "c", "d"]

    def test_deduplicate(self):
        config = {"items": ["a", "b"]}
        _merge_config_list(config, "items", ["b", "c"])
        assert sorted(config["items"]) == ["a", "b", "c"]

    def test_sorted(self):
        config = {"items": ["z"]}
        _merge_config_list(config, "items", ["a", "m"])
        assert config["items"] == ["a", "m", "z"]

    def test_missing_key(self):
        config = {}
        _merge_config_list(config, "items", ["a"])
        assert config["items"] == ["a"]


# --- export_to_jsonl ---


class TestExportToJsonl:
    def test_writes_jsonl(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "claude-sonnet-4-20250514",
            "git_branch": "main",
            "start_time": "2025-01-01T00:00:00",
            "end_time": "2025-01-01T01:00:00",
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {"input_tokens": 100, "output_tokens": 50},
            "project": "test",
        }]
        monkeypatch.setattr(
            "clawjournal.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )

        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)

        assert output.exists()
        lines = output.read_text().strip().split("\n")
        assert len(lines) == 1
        assert meta["sessions"] == 1

    def test_skips_synthetic_model(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "<synthetic>",
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {},
        }]
        monkeypatch.setattr(
            "clawjournal.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1

    def test_counts_redactions(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": "claude-sonnet-4-20250514",
            "messages": [{"role": "user", "content": "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"}],
            "stats": {"input_tokens": 10, "output_tokens": 5},
        }]
        monkeypatch.setattr(
            "clawjournal.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "test", "display_name": "test"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["redactions"] >= 1

    def test_skips_none_model(self, tmp_path, mock_anonymizer, monkeypatch):
        output = tmp_path / "out.jsonl"
        session_data = [{
            "session_id": "s1",
            "model": None,
            "messages": [{"role": "user", "content": "hi"}],
            "stats": {},
        }]
        monkeypatch.setattr(
            "clawjournal.cli.parse_project_sessions",
            lambda *a, **kw: session_data,
        )
        projects = [{"dir_name": "t", "display_name": "t"}]
        meta = export_to_jsonl(projects, output, mock_anonymizer)
        assert meta["sessions"] == 0
        assert meta["skipped"] == 1


# --- configure ---


class TestConfigure:
    def test_does_not_report_success_when_persistence_fails(
        self, tmp_config, monkeypatch, capsys
    ):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr(
            "clawjournal.cli.load_config",
            lambda: {"source": "claude", "projects_confirmed": True},
        )
        monkeypatch.setattr("clawjournal.cli.save_config", lambda _config: False)

        with pytest.raises(RuntimeError, match="persistence could not be confirmed"):
            configure(source="codex")

        assert "Config saved" not in capsys.readouterr().out

    def test_sets_repo(self, tmp_config, monkeypatch, capsys):
        # Also monkeypatch the cli module's references
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None, "excluded_projects": [], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(repo="alice/my-repo")
        assert saved["repo"] == "alice/my-repo"

    def test_merges_exclude(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"excluded_projects": ["a"], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(exclude=["b", "c"])
        assert sorted(saved["excluded_projects"]) == ["claude:a", "claude:b", "claude:c"]

    def test_preserves_prefixed_exclude(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"excluded_projects": [], "redact_strings": []})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(exclude=["codex:repo", "myapp"])
        assert sorted(saved["excluded_projects"]) == ["claude:myapp", "codex:repo"]

    def test_sets_source(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None, "source": None})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert saved["source"] == "codex"

    def test_changing_source_resets_project_confirmation(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr(
            "clawjournal.cli.load_config",
            lambda: {"repo": None, "source": "codex", "projects_confirmed": True},
        )
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(source="both")
        assert saved["source"] == "both"
        assert saved["projects_confirmed"] is False

    def test_same_source_preserves_project_confirmation(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr(
            "clawjournal.cli.load_config",
            lambda: {"repo": None, "source": "codex", "projects_confirmed": True},
        )
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert saved["source"] == "codex"
        assert saved["projects_confirmed"] is True

    def test_sets_ai_pii_review_flag(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(ai_pii_review=True)
        assert saved["ai_pii_review_enabled"] is True

        configure(ai_pii_review=False)
        assert saved["ai_pii_review_enabled"] is False

    def test_ai_pii_review_unset_leaves_config_untouched(self, tmp_config, monkeypatch, capsys):
        # The tri-state default (None) must not write the key when the flag is omitted.
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert "ai_pii_review_enabled" not in saved

    def test_sets_benchmark_tab_flag(self, tmp_config, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(benchmark_tab_enabled=False)
        assert saved["benchmark_tab_enabled"] is False

        configure(benchmark_tab_enabled=True)
        assert saved["benchmark_tab_enabled"] is True

    def test_benchmark_tab_unset_leaves_config_untouched(self, tmp_config, monkeypatch, capsys):
        # Tri-state default (None): omitting the flag must not write the key.
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"repo": None})
        saved = {}
        monkeypatch.setattr("clawjournal.cli.save_config", lambda c: saved.update(c))

        configure(source="codex")
        assert "benchmark_tab_enabled" not in saved

    def test_scoring_warmup_decline_roundtrip(self, tmp_config, monkeypatch, capsys):
        # Real file round-trip: declining sets the key, re-enabling pops it.
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)
        from clawjournal.config import load_config

        configure(scoring_warmup_declined=True)
        assert load_config()["scoring_warmup_declined"] is True

        configure(scoring_warmup_declined=False)
        assert "scoring_warmup_declined" not in load_config()

    def test_handle_config_inverts_scoring_warmup_flag(self, tmp_config, monkeypatch, capsys):
        # The user-facing flag is positive; the stored key is the inverse
        # "declined" boolean. --no-scoring-warmup => declined True.
        import argparse
        from clawjournal.cli import _handle_config
        from clawjournal.config import load_config
        monkeypatch.setattr("clawjournal.cli.CONFIG_FILE", tmp_config)

        def _args(**kw):
            ns = argparse.Namespace(
                repo=None, source=None, scorer_backend=None, exclude=None,
                redact=None, redact_usernames=None, confirm_projects=False,
                ai_pii_review=None, benchmark_tab_enabled=None, scoring_warmup=None,
            )
            for k, v in kw.items():
                setattr(ns, k, v)
            return ns

        _handle_config(_args(scoring_warmup=False))   # --no-scoring-warmup
        assert load_config()["scoring_warmup_declined"] is True
        _handle_config(_args(scoring_warmup=True))     # --scoring-warmup
        assert "scoring_warmup_declined" not in load_config()


class TestOpenRefineryCliDispatch:
    def _skip_auto_update(self, monkeypatch):
        monkeypatch.setattr("clawjournal.cli._should_auto_update", lambda argv=None: False)

    def test_enroll_openrefinery_dispatches_to_hook_installer(self, monkeypatch, capsys):
        from clawjournal import openrefinery_hooks

        self._skip_auto_update(monkeypatch)
        seen = {}

        def fake_enroll_openrefinery(**kwargs):
            seen.update(kwargs)
            return {
                "profile": "openrefinery-failures",
                "update": {"status": "skipped"},
                "install": {
                    "installed": [{"agent": "codex"}],
                    "state_path": "/tmp/state.json",
                    "projects_confirmed": False,
                },
                "next": {
                    "review": "clawjournal hooks launch openrefinery-failures",
                    "project_confirmation": "confirm projects",
                },
            }

        monkeypatch.setattr(openrefinery_hooks, "enroll_openrefinery", fake_enroll_openrefinery)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "clawjournal",
                "enroll",
                "openrefinery",
                "--agent",
                "codex",
                "--ui",
                "cli",
                "--skip-selfupdate",
                "--json",
            ],
        )

        main()

        assert seen == {"agent": "codex", "ui": "cli", "skip_selfupdate": True}
        payload = json.loads(capsys.readouterr().out)
        assert payload["profile"] == "openrefinery-failures"

    @pytest.mark.parametrize(
        ("argv_tail", "function_name", "result", "expected_kwargs"),
        [
            (
                ["install", "openrefinery-failures", "--agent", "claude", "--ui", "web"],
                "install_profile",
                {"installed": [], "state_path": "/tmp/state.json"},
                {"profile": "openrefinery-failures", "agent": "claude", "ui": "web"},
            ),
            (
                ["status", "openrefinery-failures"],
                "status",
                {"enabled": True},
                {"profile": "openrefinery-failures"},
            ),
            (
                ["disable", "openrefinery-failures"],
                "disable_profile",
                {"enabled": False},
                {"profile": "openrefinery-failures"},
            ),
            (
                ["uninstall", "openrefinery-failures", "--agent", "codex"],
                "uninstall_profile",
                {"removed": [{"agent": "codex"}]},
                {"profile": "openrefinery-failures", "agent": "codex"},
            ),
            (
                ["snooze", "openrefinery-failures", "--days", "7"],
                "snooze_profile",
                {"snooze_until": "2026-07-01"},
                {"profile": "openrefinery-failures", "days": 7},
            ),
            (
                ["launch", "openrefinery-failures", "--ui", "cli", "--port", "9999", "--no-browser"],
                "launch_share_flow",
                {"mode": "cli", "message": "Run share"},
                {"profile": "openrefinery-failures", "ui": "cli", "open_browser": False, "port": 9999},
            ),
        ],
    )
    def test_hooks_subcommands_dispatch_json(
        self,
        monkeypatch,
        capsys,
        argv_tail,
        function_name,
        result,
        expected_kwargs,
    ):
        from clawjournal import openrefinery_hooks

        self._skip_auto_update(monkeypatch)
        seen = {}

        def fake_command(**kwargs):
            seen.update(kwargs)
            return result

        monkeypatch.setattr(openrefinery_hooks, function_name, fake_command)
        monkeypatch.setattr(sys, "argv", ["clawjournal", "hooks", *argv_tail, "--json"])

        main()

        assert seen == expected_kwargs
        assert json.loads(capsys.readouterr().out) == result

    def test_hooks_run_dispatches_and_renders(self, monkeypatch, capsys):
        from clawjournal import openrefinery_hooks

        self._skip_auto_update(monkeypatch)
        seen = {}

        def fake_run_hook(**kwargs):
            seen.update(kwargs)
            return openrefinery_hooks.HookRunResult(True, "dry-run", "message")

        def fake_render_hook_response(result, *, client, output_json):
            assert result.reason == "dry-run"
            assert client == "codex"
            assert output_json is True
            return '{"rendered": true}'

        monkeypatch.setattr(openrefinery_hooks, "run_hook", fake_run_hook)
        monkeypatch.setattr(openrefinery_hooks, "render_hook_response", fake_render_hook_response)
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "clawjournal",
                "hooks",
                "run",
                "openrefinery-failures",
                "--client",
                "codex",
                "--force",
                "--dry-run",
                "--json",
            ],
        )

        main()

        assert seen == {
            "profile": "openrefinery-failures",
            "client": "codex",
            "force": True,
            "dry_run": True,
            "stop_hook_active": False,
        }
        assert json.loads(capsys.readouterr().out) == {"rendered": True}


# --- list_projects ---


class TestListProjects:
    def test_with_projects(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: [{"display_name": "proj1", "session_count": 5, "total_size_bytes": 1024}],
        )
        monkeypatch.setattr(
            "clawjournal.cli.load_config",
            lambda: {"excluded_projects": []},
        )
        list_projects()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "proj1"

    def test_no_projects(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.discover_projects", lambda source_filter=None: [])
        list_projects()
        captured = capsys.readouterr()
        assert "No Claude Code, Claude Science, Codex, Cursor, Copilot CLI, Aider, WorkBuddy, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, or Custom sessions" in captured.out

    def test_source_filter_codex(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: (
                [{"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"}]
                if source_filter == "codex"
                else [
                {"display_name": "claude:proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"},
                {"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"},
                ]
            ),
        )
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"excluded_projects": []})
        list_projects(source_filter="codex")
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "codex:proj2"
        assert data[0]["source"] == "codex"

    def test_marks_legacy_bare_exclusion_as_excluded(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: [{"display_name": "claude:proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"}],
        )
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"excluded_projects": ["proj1"]})
        list_projects()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data[0]["excluded"] is True

    def test_no_projects_for_selected_source(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: [] if source_filter == "codex" else [{"display_name": "claude:proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"}],
        )
        list_projects(source_filter="codex")
        captured = capsys.readouterr()
        assert "No Codex sessions found." in captured.out

    def test_main_list_uses_configured_source_when_auto(self, monkeypatch, capsys):
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: (
                [{"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"}]
                if source_filter == "codex"
                else [
                {"display_name": "claude:proj1", "session_count": 5, "total_size_bytes": 1024, "source": "claude"},
                {"display_name": "codex:proj2", "session_count": 3, "total_size_bytes": 512, "source": "codex"},
                ]
            ),
        )
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"source": "codex", "excluded_projects": []})
        monkeypatch.setattr("sys.argv", ["clawjournal", "list"])
        main()
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data) == 1
        assert data[0]["name"] == "codex:proj2"

    def test_main_status_dispatch_is_not_shadowed(self, monkeypatch):
        called = []
        monkeypatch.setattr("clawjournal.cli.status", lambda: called.append(True))
        monkeypatch.setattr("sys.argv", ["clawjournal", "status"])
        main()
        assert called == [True]


class TestWorkflowGateMessages:
    @staticmethod
    def _extract_json(stdout: str) -> dict:
        start = stdout.find("{")
        assert start >= 0, f"No JSON payload found in output: {stdout!r}"
        return json.loads(stdout[start:])

    def test_confirm_without_export_shows_step_process(self, tmp_path, monkeypatch, capsys):
        missing = tmp_path / "missing.jsonl"
        monkeypatch.setattr(
            "sys.argv",
            ["clawjournal", "confirm", "--file", str(missing)],
        )
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "No export file found."
        assert payload["blocked_on_step"] == "Step 1/2"
        assert len(payload["process_steps"]) == 2
        assert "export --output" in payload["process_steps"][0]

    def test_confirm_missing_full_name_explains_purpose_and_skip(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text('{"project":"p","model":"m","messages":[]}\n')
        monkeypatch.setattr(
            "sys.argv",
            [
                "clawjournal",
                "confirm",
                "--file",
                str(export_file),
                "--attest-full-name",
                "Asked for full name and scanned export.",
                "--attest-sensitive",
                "Asked about company/client/internal names and private URLs; none found.",
                "--attest-manual-scan",
                "Manually scanned 20 sessions across beginning/middle/end and reviewed findings.",
            ],
        )
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "Missing required --full-name for verification scan."
        assert "--skip-full-name-scan" in payload["hint"]
        assert payload["blocked_on_step"] == "Step 2/2"
        assert len(payload["process_steps"]) == 2

    def test_confirm_skip_full_name_scan_succeeds(self, tmp_path, monkeypatch, capsys):
        export_file = tmp_path / "export.jsonl"
        export_file.write_text('{"project":"p","model":"m","messages":[]}\n')
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.cli.save_config", lambda _c: None)
        monkeypatch.setattr(
            "sys.argv",
            [
                "clawjournal",
                "confirm",
                "--file",
                str(export_file),
                "--skip-full-name-scan",
                "--attest-full-name",
                "User declined to share full name; skipped exact-name scan.",
                "--attest-sensitive",
                "I asked about company/client/internal names and private URLs; none found.",
                "--attest-manual-scan",
                "I performed a manual scan and reviewed 20 sessions across beginning, middle, and end.",
            ],
        )
        main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["stage"] == "confirmed"
        assert payload["full_name_scan"]["skipped"] is True

    def test_export_requires_project_confirmation_with_full_flow(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli._has_session_sources", lambda _src: True)
        monkeypatch.setattr(
            "clawjournal.cli.discover_projects",
            lambda source_filter=None: [
                {
                    "display_name": "proj1",
                    "session_count": 2,
                    "total_size_bytes": 1024,
                    "source": "claude",
                }
            ],
        )
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"source": "all"})
        monkeypatch.setattr("sys.argv", ["clawjournal", "export"])
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "Project selection is not confirmed yet."
        assert payload["blocked_on_step"] == "Step 3/5"
        assert len(payload["process_steps"]) == 5
        assert "prep && clawjournal list" in payload["process_steps"][0]
        assert payload["required_action"].startswith("Send the full project/folder list")
        assert "in a message" in payload["required_action"]
        assert isinstance(payload["projects"], list)
        assert payload["projects"][0]["name"] == "proj1"
        assert payload["projects"][0]["sessions"] == 2

    def test_export_requires_explicit_source_selection(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        monkeypatch.setattr("sys.argv", ["clawjournal", "export"])
        with pytest.raises(SystemExit):
            main()
        payload = self._extract_json(capsys.readouterr().out)
        assert payload["error"] == "Source scope is not confirmed yet."
        assert payload["blocked_on_step"] == "Step 2/5"
        assert len(payload["process_steps"]) == 5
        assert payload["allowed_sources"] == ["aider", "all", "both", "claude", "claude-science", "codex", "copilot", "cursor", "custom", "gemini", "kimi", "openclaw", "opencode", "workbuddy"]
        assert payload["next_command"] == "clawjournal config --source all"

    def test_configure_next_steps_require_full_folder_presentation(self):
        steps, _next = _build_status_next_steps(
            "configure",
            {"projects_confirmed": False},
            "alice",
            "alice/my-personal-codex-data",
        )
        assert any("clawjournal list" in step for step in steps)
        assert any("FULL project/folder list" in step for step in steps)
        assert any("in your next message" in step for step in steps)
        assert any("source scope" in step.lower() for step in steps)

    def test_review_next_steps_explain_full_name_purpose_and_skip_option(self):
        steps, _next = _build_status_next_steps(
            "review",
            {},
            "alice",
            "alice/my-personal-codex-data",
        )
        assert any("exact-name privacy check" in step for step in steps)
        assert any("--skip-full-name-scan" in step for step in steps)


# --- _scan_high_entropy_strings ---


class TestScanHighEntropyStrings:
    def test_detects_real_secret(self):
        # A realistic API key-like string with high entropy and mixed chars
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        content = f"some config here token {secret} and more text"
        results = _scan_high_entropy_strings(content)
        assert len(results) >= 1
        assert any(r["match"] == secret for r in results)
        # Entropy should be >= 4.0
        for r in results:
            if r["match"] == secret:
                assert r["entropy"] >= 4.0

    def test_filters_uuid(self):
        content = "id=550e8400e29b41d4a716446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in r["match"] for r in results)

    def test_filters_uuid_with_hyphens(self):
        # UUID with hyphens won't match the 20+ contiguous regex, but without hyphens should be filtered
        content = "id=550e8400-e29b-41d4-a716-446655440000 done"
        results = _scan_high_entropy_strings(content)
        assert not any("550e8400" in r["match"] for r in results)

    def test_filters_hex_hash(self):
        content = f"commit=abcdef1234567890abcdef1234567890abcdef12 done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdef1234567890" in r["match"] for r in results)

    def test_filters_known_prefix_eyj(self):
        content = "token=eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9 done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("eyJ") for r in results)

    def test_filters_known_prefix_ghp(self):
        content = "token=ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345 done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("ghp_") for r in results)

    def test_filters_file_extension_path(self):
        content = "import=some_long_module_name_thing.py done"
        results = _scan_high_entropy_strings(content)
        assert not any(".py" in r["match"] for r in results)

    def test_filters_path_like(self):
        content = "path=src/components/authentication/LoginForm done"
        results = _scan_high_entropy_strings(content)
        assert not any("src/components" in r["match"] for r in results)

    def test_filters_low_entropy(self):
        # Repetitive string with mixed chars but low entropy
        content = "val=aaaaaaBBBBBB111111aaaaaaBBBBBB111111 done"
        results = _scan_high_entropy_strings(content)
        assert not any("aaaaaa" in r["match"] for r in results)

    def test_filters_no_mixed_chars(self):
        # All lowercase - no mixed char types
        content = "val=abcdefghijklmnopqrstuvwxyz done"
        results = _scan_high_entropy_strings(content)
        assert not any("abcdefghijklmnop" in r["match"] for r in results)

    def test_context_snippet(self):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        prefix = "before_context "
        suffix = " after_context"
        content = prefix + secret + suffix
        results = _scan_high_entropy_strings(content)
        matched = [r for r in results if r["match"] == secret]
        assert len(matched) == 1
        assert "before_context" in matched[0]["context"]
        assert "after_context" in matched[0]["context"]

    def test_results_capped_at_max(self):
        # Generate many distinct high-entropy strings
        import string
        import random
        rng = random.Random(42)
        chars = string.ascii_letters + string.digits
        secrets = []
        for _ in range(25):
            s = "".join(rng.choices(chars, k=30))
            secrets.append(s)
        content = " ".join(f"key={s}" for s in secrets)
        results = _scan_high_entropy_strings(content, max_results=15)
        assert len(results) <= 15

    def test_empty_content(self):
        assert _scan_high_entropy_strings("") == []

    def test_sorted_by_entropy_descending(self):
        secret1 = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        secret2 = "Zx9Yw8Xv7Wu6Ts5Rq4Po3Nm2Lk1Jh0G"
        content = f"a={secret1} b={secret2}"
        results = _scan_high_entropy_strings(content)
        if len(results) >= 2:
            assert results[0]["entropy"] >= results[1]["entropy"]

    def test_filters_benign_prefix_https(self):
        content = "url=https://example.com/some/long/path/here done"
        results = _scan_high_entropy_strings(content)
        assert not any(r["match"].startswith("https://") for r in results)

    def test_filters_three_dots(self):
        content = "ver=com.example.app.module.v1.2.3 done"
        results = _scan_high_entropy_strings(content)
        assert not any("com.example.app" in r["match"] for r in results)

    def test_filters_node_modules(self):
        content = "path=some_long_node_modules_path_thing done"
        results = _scan_high_entropy_strings(content)
        assert not any("node_modules" in r["match"] for r in results)


# --- _scan_pii integration with high_entropy_strings ---


class TestScanPiiHighEntropy:
    def test_includes_high_entropy_when_present(self, tmp_path):
        secret = "aB3dE6gH9jK2mN5pQ8rS1tU4wX7yZ0c"
        f = tmp_path / "export.jsonl"
        f.write_text(f'{{"message": "config token {secret} end"}}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" in results
        assert any(r["match"] == secret for r in results["high_entropy_strings"])

    def test_excludes_high_entropy_when_clean(self, tmp_path):
        f = tmp_path / "export.jsonl"
        f.write_text('{"message": "nothing suspicious here at all"}\n')
        results = _scan_pii(f)
        assert "high_entropy_strings" not in results


# --- Bundle CLI commands ---


@pytest.fixture
def bundle_index(tmp_path, monkeypatch):
    """Set up an index DB with sessions for bundle testing."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")
    monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})

    from clawjournal.redaction import betterleaks, trufflehog
    monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(trufflehog, "is_available", lambda: True)
    monkeypatch.setattr(
        trufflehog,
        "scan_file_with_raws",
        lambda path, *, results="verified,unknown,unverified": (
            trufflehog.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
            ),
            [],
        ),
    )
    monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", lambda text: [])
    monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
    monkeypatch.setattr(betterleaks, "is_available", lambda: True)
    monkeypatch.setattr(
        betterleaks,
        "scan_file_with_raws",
        lambda path: (
            betterleaks.BetterleaksReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
            ),
            [],
        ),
    )
    monkeypatch.setattr(betterleaks, "_scan_text_for_raw_matches", lambda text: [])
    monkeypatch.setattr(
        "clawjournal.redaction.pii.review_session_pii_hybrid",
        lambda session, **kw: ([], "full") if kw.get("return_coverage") else [],
    )

    from clawjournal.workbench.index import open_index, upsert_sessions

    conn = open_index()
    sessions = [
        {
            "session_id": f"sess-{i}",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": f"Task {i}"},
                {"role": "assistant", "content": "Done."},
            ],
            "stats": {"user_messages": 1, "assistant_messages": 1,
                       "tool_uses": 0, "input_tokens": 100, "output_tokens": 50},
        }
        for i in range(3)
    ]
    upsert_sessions(conn, sessions)

    # Approve sessions for bundle testing
    from clawjournal.workbench.index import update_session
    for i in range(3):
        update_session(conn, f"sess-{i}", status="approved")
    conn.close()
    return tmp_path


class TestBundleCreate:
    def test_create_by_ids(self, bundle_index, capsys):
        from clawjournal.cli import _run_bundle_create
        args = MagicMock(session_ids=["sess-0", "sess-1"], status=None,
                         note="test", attestation=None, json=True)
        _run_bundle_create(args)
        output = json.loads(capsys.readouterr().out)
        assert output["session_count"] == 2
        assert output["status"] == "draft"
        assert "share_id" in output
        assert output["bundle_id"] == output["share_id"]

    def test_create_by_status(self, bundle_index, capsys):
        from clawjournal.cli import _run_bundle_create
        args = MagicMock(session_ids=[], status="approved", note=None, attestation=None, json=True)
        _run_bundle_create(args)
        output = json.loads(capsys.readouterr().out)
        assert output["session_count"] == 3

    def test_create_no_sessions_exits(self, bundle_index):
        from clawjournal.cli import _run_bundle_create
        args = MagicMock(session_ids=[], status=None, note=None, attestation=None)
        with pytest.raises(SystemExit):
            _run_bundle_create(args)

    def test_create_by_status_filters_already_shared_revision(
        self, bundle_index, capsys
    ):
        from clawjournal.cli import _run_bundle_create
        from clawjournal.workbench.index import create_share, open_index

        conn = open_index()
        try:
            shared_id = create_share(conn, ["sess-0"])
            conn.execute(
                "UPDATE shares SET status='shared', shared_at=? WHERE share_id=?",
                ("2026-07-01T00:00:00+00:00", shared_id),
            )
            conn.commit()
        finally:
            conn.close()

        args = MagicMock(
            session_ids=[], status="approved", note=None, attestation=None, json=True
        )
        _run_bundle_create(args)

        output = json.loads(capsys.readouterr().out)
        assert output["session_count"] == 2

    def test_create_explicit_missing_id_exits_cleanly(self, bundle_index, capsys):
        from clawjournal.cli import _run_bundle_create

        args = MagicMock(
            session_ids=["missing"], status=None, note=None,
            attestation=None, json=False,
        )
        with pytest.raises(SystemExit):
            _run_bundle_create(args)

        assert "not eligible to bundle" in capsys.readouterr().out


class TestBundleList:
    def test_list_empty(self, bundle_index, capsys):
        from clawjournal.cli import _run_bundle_list
        _run_bundle_list(MagicMock(json=True))
        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 0
        assert output["shares"] == []
        assert output["bundles"] == []

    def test_list_after_create(self, bundle_index, capsys):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        create_share(conn, ["sess-0"], note="test")
        conn.close()

        from clawjournal.cli import _run_bundle_list
        _run_bundle_list(MagicMock(json=True))
        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 1
        assert output["shares"][0]["session_count"] == 1
        assert output["bundles"][0]["bundle_id"] == output["shares"][0]["share_id"]

    def test_list_human(self, bundle_index, capsys):
        from clawjournal.cli import _run_bundle_list
        _run_bundle_list(MagicMock(json=False))
        out = capsys.readouterr().out
        assert "No shares" in out


class TestBundleView:
    def test_view(self, bundle_index, capsys):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0", "sess-1"])
        conn.close()

        from clawjournal.cli import _run_bundle_view
        args = MagicMock(share_id=bundle_id, json=True)
        _run_bundle_view(args)
        output = json.loads(capsys.readouterr().out)
        assert output["share_id"] == bundle_id
        assert output["bundle_id"] == bundle_id
        assert len(output["sessions"]) == 2

    def test_view_prefix(self, bundle_index, capsys):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0"])
        conn.close()

        from clawjournal.cli import _run_bundle_view
        args = MagicMock(share_id=bundle_id[:8], json=True)
        _run_bundle_view(args)
        output = json.loads(capsys.readouterr().out)
        assert output["share_id"] == bundle_id
        assert output["bundle_id"] == bundle_id

    def test_view_not_found(self, bundle_index):
        from clawjournal.cli import _run_bundle_view
        args = MagicMock(share_id="nonexistent")
        with pytest.raises(SystemExit):
            _run_bundle_view(args)


class TestBundleExport:
    def test_export(self, bundle_index, capsys):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0", "sess-1"])
        conn.close()

        from clawjournal.cli import _run_bundle_export
        args = MagicMock(share_id=bundle_id, output=None, json=True)
        _run_bundle_export(args)
        output = json.loads(capsys.readouterr().out)
        assert output["session_count"] == 2
        assert "sessions.jsonl" in output["files"]
        assert Path(output["export_path"]).exists()
        manifest = json.loads((Path(output["export_path"]) / "manifest.json").read_text())
        assert manifest["bundle_id"] == bundle_id
        assert manifest["share_id"] == bundle_id
        assert "redaction_summary" in output

    def test_export_zip_writes_uploadable_archive(self, bundle_index, capsys, monkeypatch):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0", "sess-1"])
        conn.close()

        def fake_review(session, **kw):
            assert kw.get("return_coverage") is True
            if session["session_id"] != "sess-0":
                return ([], "full")
            return ([{
                "session_id": "sess-0",
                "message_index": 0,
                "field": "content",
                "entity_text": "Task 0",
                "entity_type": "person_name",
                "confidence": 0.95,
                "reason": "test",
                "replacement": "[REDACTED_NAME]",
                "source": "test",
            }], "full")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fake_review)

        from clawjournal.cli import _run_bundle_export
        args = MagicMock(
            share_id=bundle_id,
            output=None,
            json=True,
            zip=True,
            ai_pii_review=True,
            training_format=False,
        )
        _run_bundle_export(args)
        output = json.loads(capsys.readouterr().out)

        zip_path = Path(output["zip_path"])
        assert zip_path.exists()
        with zipfile.ZipFile(zip_path) as archive:
            assert set(archive.namelist()) >= {
                "manifest.json",
                "sessions.jsonl",
                "trufflehog.json",
                "trufflehog.post-pii.json",
            }
            sessions_content = archive.read("sessions.jsonl").decode("utf-8")
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))

        assert "Task 0" not in sessions_content
        assert "[REDACTED_NAME]" in sessions_content
        assert manifest["redaction_summary"]["pii_review"]["finding_count"] == 1
        assert manifest["redaction_summary"]["coverage"] == {"full": 2, "rules_only": 0}

    def test_export_ai_pii_review_without_zip_warns(self, bundle_index, capsys, monkeypatch):
        """--ai-pii-review is a no-op without --zip; the CLI must say so rather
        than silently ignore it."""
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0", "sess-1"])
        conn.close()

        def fail_hybrid(*_a, **_k):
            raise AssertionError("AI PII review must not run without --zip")

        monkeypatch.setattr("clawjournal.redaction.pii.review_session_pii_hybrid", fail_hybrid)

        from clawjournal.cli import _run_bundle_export
        args = MagicMock(
            share_id=bundle_id, output=None, json=True,
            zip=False, ai_pii_review=True, training_format=False,
        )
        _run_bundle_export(args)
        captured = capsys.readouterr()
        assert "--ai-pii-review only applies when building the uploadable zip" in captured.err

    def test_export_zip_appends_suffix_to_output_directory(self, bundle_index, tmp_path, capsys):
        from clawjournal.workbench.index import create_share, open_index
        conn = open_index()
        bundle_id = create_share(conn, ["sess-0"])
        conn.close()

        output_dir = Path("/tmp") / f"clawjournal-manual-export-{bundle_id}.zip"
        from clawjournal.cli import _run_bundle_export
        args = MagicMock(
            share_id=bundle_id,
            output=str(output_dir),
            json=True,
            zip=True,
            training_format=False,
        )
        try:
            _run_bundle_export(args)
            output = json.loads(capsys.readouterr().out)

            resolved_output_dir = output_dir.resolve()
            assert Path(output["export_path"]) == resolved_output_dir
            assert Path(output["zip_path"]) == resolved_output_dir.with_name(f"{resolved_output_dir.name}.zip")
            assert Path(output["zip_path"]).is_file()
        finally:
            shutil.rmtree(output_dir, ignore_errors=True)
            output_dir.with_name(f"{output_dir.name}.zip").unlink(missing_ok=True)

    def test_export_redacts_custom_strings(self, tmp_path, monkeypatch, capsys):
        """Bundle export applies redact_strings from config."""
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")

        from clawjournal.workbench.index import create_share, open_index, upsert_sessions

        conn = open_index()
        sessions = [
            {
                "session_id": "redact-test",
                "project": "MySecretName-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "messages": [
                    {"role": "user", "content": "Hello MySecretName, check sk-ant-api03-AAAAAAAAAAAAAAAAAAAAAAAAA"},
                    {"role": "assistant", "content": "Sure MySecretName, done."},
                ],
                "stats": {"user_messages": 1, "assistant_messages": 1,
                           "tool_uses": 0, "input_tokens": 100, "output_tokens": 50},
            },
        ]
        upsert_sessions(conn, sessions)

        from clawjournal.workbench.index import update_session
        update_session(conn, "redact-test", status="approved")
        bundle_id = create_share(conn, ["redact-test"])
        conn.close()

        # Configure redact_strings — patch in cli's namespace where load_config is bound
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"redact_strings": ["MySecretName"]})

        from clawjournal.cli import _run_bundle_export
        args = MagicMock(share_id=bundle_id, output=None, json=True)
        _run_bundle_export(args)
        output = json.loads(capsys.readouterr().out)

        # Read the exported JSONL and verify redaction
        sessions_file = Path(output["export_path"]) / "sessions.jsonl"
        content = sessions_file.read_text()
        assert "MySecretName" not in content, "Custom redact_string was not redacted"
        assert "sk-ant-api03" not in content, "API key was not redacted"

        manifest_content = (Path(output["export_path"]) / "manifest.json").read_text()
        assert "MySecretName" not in manifest_content, "manifest metadata was not redacted"

    def test_export_applies_policy_rules(self, tmp_path, monkeypatch, capsys):
        """Bundle export applies workbench policy rules in addition to config."""
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")

        from clawjournal.workbench.index import add_policy, create_share, open_index, upsert_sessions

        conn = open_index()
        upsert_sessions(conn, [{
            "session_id": "policy-test",
            "project": "test-project",
            "source": "claude",
            "model": "claude-sonnet-4",
            "messages": [
                {"role": "user", "content": "MySecretName contacted PartnerDev at api.foo.internal"},
                {"role": "assistant", "content": "PartnerDev confirmed api.foo.internal is reachable."},
            ],
            "stats": {
                "user_messages": 1,
                "assistant_messages": 1,
                "tool_uses": 0,
                "input_tokens": 100,
                "output_tokens": 50,
            },
        }])

        from clawjournal.workbench.index import update_session
        update_session(conn, "policy-test", status="approved")
        add_policy(conn, "redact_string", "MySecretName")
        add_policy(conn, "redact_username", "PartnerDev")
        add_policy(conn, "block_domain", "*.internal")
        share_id = create_share(conn, ["policy-test"])
        conn.close()

        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})

        from clawjournal.cli import _run_bundle_export
        args = MagicMock(share_id=share_id, output=None, json=True)
        _run_bundle_export(args)
        output = json.loads(capsys.readouterr().out)

        sessions_file = Path(output["export_path"]) / "sessions.jsonl"
        content = sessions_file.read_text()
        assert "MySecretName" not in content
        assert "PartnerDev" not in content
        assert "foo.internal" not in content


class TestSearch:
    def test_search_json(self, bundle_index, capsys):
        from clawjournal.cli import _run_search
        args = MagicMock(query="Task", limit=20, source=None, json=True)
        _run_search(args)
        output = json.loads(capsys.readouterr().out)
        assert output["query"] == "Task"
        # FTS might not be built for this test; just check structure
        assert "results" in output

    def test_search_table(self, bundle_index, capsys):
        from clawjournal.cli import _run_search
        args = MagicMock(query="zzzznonexistent", limit=20, source=None, json=False)
        _run_search(args)
        out = capsys.readouterr().out
        assert "No results" in out


class TestScanOutput:
    @staticmethod
    def _patch_scan_dependencies(monkeypatch, scanner_cls):
        class FakeConnection:
            def close(self):
                pass

        monkeypatch.setattr("clawjournal.pricing.ensure_pricing_fresh", lambda: None)
        monkeypatch.setattr("clawjournal.workbench.daemon.Scanner", scanner_cls)
        monkeypatch.setattr(
            "clawjournal.workbench.index.open_index", lambda: FakeConnection()
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.get_stats",
            lambda conn: {"total": 27, "by_status": {}, "by_source": {}},
        )

    def test_reports_updated_trace_when_no_new_session(self, monkeypatch, capsys):
        class FakeScanner:
            last_updated_count = 1
            last_updated_by_source = {"codex": 1}
            last_linked_count = 0

            def __init__(self, source_filter=None):
                pass

            def scan_once(self):
                return {"codex": 0}

        self._patch_scan_dependencies(monkeypatch, FakeScanner)

        from clawjournal.cli import _run_scan

        _run_scan()
        output = capsys.readouterr().out

        assert "Indexed 0 new sessions; updated 1 existing trace:" in output
        assert "codex: 1 updated trace" in output
        assert "No new sessions found" not in output

    def test_reports_no_changes_only_when_new_and_updated_are_zero(
        self, monkeypatch, capsys
    ):
        class FakeScanner:
            last_updated_count = 0
            last_updated_by_source = {}
            last_linked_count = 0

            def __init__(self, source_filter=None):
                pass

            def scan_once(self):
                return {"codex": 0}

        self._patch_scan_dependencies(monkeypatch, FakeScanner)

        from clawjournal.cli import _run_scan

        _run_scan()
        output = capsys.readouterr().out

        assert "No new or updated sessions found." in output

    def test_reports_busy_scan_without_a_traceback(self, monkeypatch, capsys):
        from clawjournal.workbench.daemon import ScanBusyError

        class FakeScanner:
            def __init__(self, source_filter=None):
                pass

            def scan_once(self):
                raise ScanBusyError("Another scan is still refreshing the index.")

        self._patch_scan_dependencies(monkeypatch, FakeScanner)

        from clawjournal.cli import _run_scan

        with pytest.raises(SystemExit) as exc_info:
            _run_scan()

        assert exc_info.value.code == 1
        assert (
            "Scan not run: Another scan is still refreshing the index."
            in capsys.readouterr().err
        )


class TestWorkbenchSourceChoices:
    def test_scan_accepts_cursor_source(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("clawjournal.cli._run_scan", lambda source_filter=None: seen.setdefault("source", source_filter))
        monkeypatch.setattr(sys, "argv", ["clawjournal", "scan", "--source", "cursor"])

        main()

        assert seen["source"] == "cursor"

    def test_serve_accepts_aider_source(self, monkeypatch):
        seen = {}
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.run_server",
            lambda **kwargs: seen.update(kwargs),
        )
        monkeypatch.setattr(sys, "argv", ["clawjournal", "serve", "--source", "aider", "--no-browser"])

        main()

        assert seen["source_filter"] == "aider"

    def test_recent_accepts_copilot_source(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("clawjournal.cli._run_recent", lambda args: seen.setdefault("source", args.source))
        monkeypatch.setattr(sys, "argv", ["clawjournal", "recent", "--source", "copilot"])

        main()

        assert seen["source"] == "copilot"

    def test_search_accepts_aider_source(self, monkeypatch):
        seen = {}
        monkeypatch.setattr("clawjournal.cli._run_search", lambda args: seen.setdefault("source", args.source))
        monkeypatch.setattr(sys, "argv", ["clawjournal", "search", "fix", "--source", "aider"])

        main()

        assert seen["source"] == "aider"


class TestRecentOutput:
    def test_recent_prints_failure_value_as_visible_score(self, monkeypatch, capsys):
        from clawjournal.cli import _run_recent

        conn = MagicMock()

        def fake_query_sessions(conn, **kwargs):
            if kwargs.get("sort") == "indexed_at":
                return [{"indexed_at": "2999-01-01T00:00:00+00:00"}]
            return [{
                "session_id": "sess-1",
                "display_title": "Investigate flaky parser",
                "duration_seconds": 90,
                "outcome_badge": "failed",
                "ai_failure_value_score": 4,
                "ai_quality_score": 1,
            }]

        monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: conn)
        monkeypatch.setattr("clawjournal.workbench.index.query_sessions", fake_query_sessions)

        _run_recent(MagicMock(source=None, since=None, limit=5, json=False))

        out = capsys.readouterr().out
        assert "FV 4/5" in out
        assert "1/5" not in out

    def test_recent_shows_current_index_when_refresh_is_busy(
        self, monkeypatch, capsys
    ):
        from clawjournal.cli import _run_recent
        from clawjournal.workbench.daemon import ScanBusyError

        conn = MagicMock()

        class BusyScanner:
            def __init__(self, source_filter=None):
                pass

            def scan_once(self):
                raise ScanBusyError("busy")

        monkeypatch.setattr(
            "clawjournal.workbench.daemon.Scanner", BusyScanner
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.open_index", lambda: conn
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.query_sessions",
            lambda _conn, **_kwargs: [],
        )

        _run_recent(MagicMock(source=None, since=None, limit=5, json=False))

        assert (
            "Another scan is refreshing the index; showing current results."
            in capsys.readouterr().err
        )


class TestEventsCLI:
    def test_events_capabilities_outputs_json(self, capsys):
        with patch.object(sys, "argv", ["clawjournal", "events", "capabilities"]):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["claude"]["tool_call"]["supported"] is True
        assert payload["codex"]["stdout_chunk"]["supported"] is False

    def test_events_ingest_outputs_summary_json(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "config")
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / "config")

        from clawjournal.parsing import parser

        monkeypatch.setattr(parser, "PROJECTS_DIR", tmp_path / "claude" / "projects")
        monkeypatch.setattr(parser, "CODEX_SESSIONS_DIR", tmp_path / "codex" / "sessions")
        monkeypatch.setattr(parser, "CODEX_ARCHIVED_DIR", tmp_path / "codex" / "archived_sessions")
        monkeypatch.setattr(parser, "LOCAL_AGENT_DIR", tmp_path / "local_agent")
        monkeypatch.setattr(parser, "OPENCLAW_AGENTS_DIR", tmp_path / "openclaw" / "agents")

        session_file = tmp_path / "claude" / "projects" / "demo-project" / "cli.jsonl"
        session_file.parent.mkdir(parents=True)
        session_file.write_text(
            json.dumps(
                {
                    "type": "user",
                    "timestamp": "2026-04-20T10:00:00.000Z",
                    "message": {"content": "Hello from CLI"},
                }
            )
            + "\n"
        )

        with patch.object(
            sys,
            "argv",
            ["clawjournal", "events", "ingest", "--source", "claude", "--json"],
        ):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["files_scanned"] == 1
        assert payload["files_with_changes"] == 1
        assert payload["event_rows"] == 1


class TestEventsInspectCLI:
    def _setup_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db"
        )
        monkeypatch.setattr(
            "clawjournal.workbench.index.CONFIG_DIR", tmp_path / "config"
        )
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", tmp_path / "config")

    def _ingest_one_user_line(self, tmp_path, monkeypatch, vendor_line_payload=None):
        """Drive real ingest to populate events + event_sessions from a
        fabricated vendor JSONL file. Returns the ingested events.id."""
        from clawjournal.parsing import parser

        monkeypatch.setattr(parser, "PROJECTS_DIR", tmp_path / "claude" / "projects")
        monkeypatch.setattr(parser, "CODEX_SESSIONS_DIR", tmp_path / "codex" / "sessions")
        monkeypatch.setattr(
            parser, "CODEX_ARCHIVED_DIR", tmp_path / "codex" / "archived_sessions"
        )
        monkeypatch.setattr(parser, "LOCAL_AGENT_DIR", tmp_path / "local_agent")
        monkeypatch.setattr(parser, "OPENCLAW_AGENTS_DIR", tmp_path / "openclaw" / "agents")

        session_file = tmp_path / "claude" / "projects" / "demo-proj" / "sess.jsonl"
        session_file.parent.mkdir(parents=True)
        payload = vendor_line_payload or {
            "type": "user",
            "timestamp": "2026-04-20T10:00:00Z",
            "message": {"content": "hi"},
        }
        session_file.write_text(json.dumps(payload) + "\n", encoding="utf-8")

        from clawjournal.events import ingest_pending
        from clawjournal.workbench.index import open_index

        conn = open_index()
        try:
            ingest_pending(conn, source_filter="claude")
            event_id = conn.execute("SELECT id FROM events LIMIT 1").fetchone()[0]
            session_key = conn.execute(
                "SELECT session_key FROM event_sessions LIMIT 1"
            ).fetchone()[0]
        finally:
            conn.close()
        return event_id, session_key, session_file

    def _seed_override_case(self, tmp_path, monkeypatch):
        self._setup_db(tmp_path, monkeypatch)

        vendor_file = tmp_path / "vendor.jsonl"
        vendor_file.write_text('{"vendor":"line"}\n', encoding="utf-8")

        from clawjournal.events import ensure_view_schema, write_hook_override
        from clawjournal.events.schema import ensure_schema as ensure_events_schema
        from clawjournal.workbench.index import open_index

        session_key = "claude:demo-proj:sess-override"
        conn = open_index()
        try:
            ensure_events_schema(conn)
            ensure_view_schema(conn)
            session_id = conn.execute(
                """
                INSERT INTO event_sessions (session_key, client, status)
                VALUES (?, ?, ?)
                """,
                (session_key, "claude", "active"),
            ).lastrowid
            event_id = conn.execute(
                """
                INSERT INTO events (
                    session_id, type, event_key, event_at, ingested_at, source,
                    source_path, source_offset, seq, client, confidence, lossiness,
                    raw_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    "tool_call",
                    "tool_call:override",
                    "2026-04-20T10:00:00Z",
                    "2026-04-20T10:00:01Z",
                    "claude-jsonl",
                    str(vendor_file),
                    0,
                    0,
                    "claude",
                    "low",
                    "none",
                    '{"base":true}',
                ),
            ).lastrowid
            conn.commit()
            write_hook_override(
                conn,
                session_key=session_key,
                event_key="tool_call:override",
                event_type="tool_call",
                source="hook",
                confidence="high",
                lossiness="none",
                event_at="2026-04-20T10:00:02Z",
                payload_json='{"override":true}',
                origin="hook:test:v1",
            )
        finally:
            conn.close()

        return int(event_id), session_key, vendor_file

    def test_inspect_by_event_id_emits_json(self, tmp_path, monkeypatch, capsys):
        self._setup_db(tmp_path, monkeypatch)
        event_id, _, _ = self._ingest_one_user_line(tmp_path, monkeypatch)

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", str(event_id), "--json"]
        ):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == event_id
        assert payload["type"] == "user_message"
        assert payload["client"] == "claude"
        assert payload["override"] is None
        assert payload["raw_ref"][0].endswith("sess.jsonl")
        assert payload["vendor_line"] is not None

    def test_inspect_by_event_id_prefers_canonical_override_fields(
        self, tmp_path, monkeypatch, capsys
    ):
        event_id, _, _ = self._seed_override_case(tmp_path, monkeypatch)

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", str(event_id), "--json"]
        ):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == event_id
        assert payload["type"] == "tool_call"
        assert payload["source"] == "hook"
        assert payload["confidence"] == "high"
        assert payload["event_at"] == "2026-04-20T10:00:02Z"
        assert payload["raw_json"] == '{"base":true}'
        assert payload["override"]["payload_json"] == '{"override":true}'
        assert payload["vendor_line"] == '{"vendor":"line"}'

    def test_inspect_missing_vendor_file_falls_back_to_placeholder(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        event_id, _, session_file = self._ingest_one_user_line(tmp_path, monkeypatch)
        session_file.unlink()

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", str(event_id)]
        ):
            main()

        out = capsys.readouterr().out
        assert "(source file no longer on disk)" in out

    def test_inspect_by_session_and_event_key_hook_only(
        self, tmp_path, monkeypatch, capsys
    ):
        """A hook-only event (override with no matching base) must be
        reachable via --session --event-key and marked hook_only."""
        self._setup_db(tmp_path, monkeypatch)
        _, session_key, _ = self._ingest_one_user_line(tmp_path, monkeypatch)

        from clawjournal.events import ensure_view_schema, write_hook_override
        from clawjournal.workbench.index import open_index

        conn = open_index()
        try:
            ensure_view_schema(conn)
            write_hook_override(
                conn,
                session_key=session_key,
                event_key="tool_call:hook-only",
                event_type="tool_call",
                source="hook",
                confidence="high",
                lossiness="none",
                event_at="2026-04-20T10:00:05Z",
                payload_json='{"hook":true}',
                origin="hook:test:v1",
            )
        finally:
            conn.close()

        with patch.object(
            sys,
            "argv",
            [
                "clawjournal", "events", "inspect",
                "--session", session_key,
                "--event-key", "tool_call:hook-only",
                "--json",
            ],
        ):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] is None
        assert payload["hook_only"] is True
        assert payload["raw_json"] is None
        assert payload["raw_ref"] is None
        assert payload["override"]["origin"] == "hook:test:v1"

    def test_inspect_by_session_and_event_key_prefers_canonical_override_fields(
        self, tmp_path, monkeypatch, capsys
    ):
        event_id, session_key, vendor_file = self._seed_override_case(
            tmp_path, monkeypatch
        )

        with patch.object(
            sys,
            "argv",
            [
                "clawjournal", "events", "inspect",
                "--session", session_key,
                "--event-key", "tool_call:override",
                "--json",
            ],
        ):
            main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["id"] == event_id
        assert payload["type"] == "tool_call"
        assert payload["source"] == "hook"
        assert payload["confidence"] == "high"
        assert payload["event_at"] == "2026-04-20T10:00:02Z"
        assert payload["raw_ref"][0] == str(vendor_file)
        assert payload["override"]["origin"] == "hook:test:v1"

    def test_inspect_human_output_includes_raw_and_override_payloads(
        self, tmp_path, monkeypatch, capsys
    ):
        event_id, _, _ = self._seed_override_case(tmp_path, monkeypatch)

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", str(event_id)]
        ):
            main()

        out = capsys.readouterr().out
        assert "raw_json:" in out
        assert '{"base":true}' in out
        assert "override payload:" in out
        assert '{"override":true}' in out

    def test_inspect_rejects_nonexistent_event_id(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        self._ingest_one_user_line(tmp_path, monkeypatch)

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", "999999"]
        ):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 1
        err = capsys.readouterr().err
        assert "event not found" in err

    def test_inspect_requires_id_xor_session_event_key(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        with patch.object(sys, "argv", ["clawjournal", "events", "inspect"]):
            with pytest.raises(SystemExit) as exc_info:
                main()
            assert exc_info.value.code == 2
        err = capsys.readouterr().err
        assert "requires either" in err

    def test_inspect_truncate_flag_shortens_raw_json(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        payload = {
            "type": "user",
            "timestamp": "2026-04-20T10:00:00Z",
            "message": {"content": "x" * 2000},
        }
        event_id, _, _ = self._ingest_one_user_line(
            tmp_path, monkeypatch, vendor_line_payload=payload
        )

        with patch.object(
            sys,
            "argv",
            [
                "clawjournal", "events", "inspect", str(event_id),
                "--json", "--truncate", "64",
            ],
        ):
            main()

        out = json.loads(capsys.readouterr().out)
        assert out["raw_json"].endswith("chars]")
        assert "truncated at 64" in out["raw_json"]

    def test_inspect_truncate_zero_disables_truncation(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        payload = {
            "type": "user",
            "timestamp": "2026-04-20T10:00:00Z",
            "message": {"content": "x" * 3000},
        }
        event_id, _, _ = self._ingest_one_user_line(
            tmp_path, monkeypatch, vendor_line_payload=payload
        )

        with patch.object(
            sys,
            "argv",
            [
                "clawjournal", "events", "inspect", str(event_id),
                "--json", "--truncate", "0",
            ],
        ):
            main()

        out = json.loads(capsys.readouterr().out)
        assert "truncated at" not in out["raw_json"]
        assert len(out["raw_json"]) > 2000

    def test_inspect_human_mode_default_truncates(
        self, tmp_path, monkeypatch, capsys
    ):
        self._setup_db(tmp_path, monkeypatch)
        payload = {
            "type": "user",
            "timestamp": "2026-04-20T10:00:00Z",
            "message": {"content": "x" * 3000},
        }
        event_id, _, _ = self._ingest_one_user_line(
            tmp_path, monkeypatch, vendor_line_payload=payload
        )

        with patch.object(
            sys, "argv", ["clawjournal", "events", "inspect", str(event_id)]
        ):
            main()
        out = capsys.readouterr().out
        assert "truncated at 1024" in out


class TestEventsCostCLI:
    def test_ingest_rebuild_forwards_flag(self, monkeypatch, capsys):
        summary = MagicMock()
        summary.to_dict.return_value = {
            "events_scanned": 3,
            "token_rows_written": 1,
            "anomalies_written": 0,
            "sessions_touched": 1,
        }
        captured: dict[str, object] = {}

        def fake_ingest(conn, *, rebuild=False):
            captured["conn"] = conn
            captured["rebuild"] = rebuild
            return summary

        conn = MagicMock()
        monkeypatch.setattr("clawjournal.events.cost.ingest_cost_pending", fake_ingest)
        monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: conn)

        with patch.object(
            sys,
            "argv",
            ["clawjournal", "events", "cost", "ingest", "--rebuild", "--json"],
        ):
            main()

        assert captured["conn"] is conn
        assert captured["rebuild"] is True
        conn.close.assert_called_once()
        payload = json.loads(capsys.readouterr().out)
        assert payload["events_scanned"] == 3


class TestShareHelpers:
    def test_share_preview_json_returns_payload(self):
        payload = _share_preview([{"session_id": "s1", "display_title": "hello", "risk_badges": "[]"}], output_json=True)
        assert payload is not None
        assert payload["total"] == 1
        assert payload["sessions"][0]["session_id"] == "s1"

    def test_share_pii_status_warns_without_export(self, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        status = _share_pii_status()
        assert status["level"] == "warn"
        assert "No recent export record" in status["message"]

    def test_share_pii_status_info_with_sanitized(self, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {
            "last_export": {
                "output_file": "/tmp/a.jsonl",
                "pii_review": {"finding_count": 3},
                "pii_apply": {"output": "/tmp/a.sanitized.jsonl"},
            }
        })
        status = _share_pii_status()
        assert status["level"] == "info"
        assert "/tmp/a.sanitized.jsonl" in status["message"]


class TestShare:
    def test_share_approved(self, bundle_index, capsys, monkeypatch):
        """share --status approved packages locally and points to the workbench submit step."""
        from clawjournal.cli import _run_share

        monkeypatch.setattr(
            "clawjournal.workbench.daemon._prepare_share_export_for_upload",
            lambda conn, share_id, share, settings, reuse_finalized=False, **_kw: (
                Path("/tmp/clawjournal-share"),
                {
                    "sessions": [{}, {}, {}],
                    "redaction_summary": {"total_redactions": 2, "by_type": {"jwt": 1, "email": 1}},
                },
                None,
            ),
        )
        monkeypatch.setattr("clawjournal.cli._collect_pii_findings", lambda *args, **kwargs: [])

        args = MagicMock(session_ids=[], status="approved", note="test",
                         force=False, json=False, preview=False)
        _run_share(args)
        out = capsys.readouterr().out
        assert "Packaged 3 sessions" in out
        assert "ready for hosted submission in the workbench" in out
        assert "Privacy:" in out
        assert "2 redactions applied" in out

    def test_share_preview(self, bundle_index, capsys):
        """share --preview shows session list without uploading."""
        from clawjournal.cli import _run_share
        args = MagicMock(session_ids=[], status="approved", note=None,
                         force=False, json=False, preview=True)
        _run_share(args)
        out = capsys.readouterr().out
        assert "sessions ready to share" in out

    def test_share_json_includes_bundle_id(self, bundle_index, capsys, monkeypatch):
        """share --json restores the legacy bundle_id alias."""
        from clawjournal.cli import _run_share

        monkeypatch.setattr(
            "clawjournal.workbench.daemon._prepare_share_export_for_upload",
            lambda conn, share_id, share, settings, reuse_finalized=False, **_kw: (
                Path("/tmp/clawjournal-share"),
                {"sessions": [{}, {}, {}], "redaction_summary": {"total_redactions": 0, "by_type": {}}},
                None,
            ),
        )

        args = MagicMock(session_ids=[], status="approved", note="test",
                         force=False, json=True, preview=False)
        _run_share(args)
        output = json.loads(capsys.readouterr().out)
        assert output["share_id"]
        assert output["bundle_id"] == output["share_id"]
        assert output["next_step"] == "submit_in_workbench"

    def test_share_honors_config_ai_pii_default(self, bundle_index, capsys, monkeypatch):
        """With no --ai-pii-review flag, the persisted config default opts in."""
        from clawjournal.cli import _run_share

        # bundle_index points cli.load_config at {}; opt in via the config default.
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"ai_pii_review_enabled": True})

        captured = {}

        def _stub(conn, share_id, share, settings, reuse_finalized=False, **kw):
            captured.update(kw)
            return (
                Path("/tmp/clawjournal-share"),
                {"sessions": [{}], "redaction_summary": {"total_redactions": 0, "by_type": {}}},
                None,
            )

        monkeypatch.setattr(
            "clawjournal.workbench.daemon._prepare_share_export_for_upload", _stub)

        # No ai_pii_review on the namespace → the flag is absent.
        args = MagicMock(session_ids=[], status="approved", note="test",
                         force=False, json=True, preview=False)
        del args.ai_pii_review
        _run_share(args)

        assert captured.get("ai_pii_review_enabled") is True
        output = json.loads(capsys.readouterr().out)
        assert output["workbench_url"].endswith("&ai_pii=1")

    def test_share_revision_race_exits_cleanly(
        self, bundle_index, capsys, monkeypatch
    ):
        from clawjournal.cli import _run_share
        from clawjournal.workbench.index import RevisionConflictError

        monkeypatch.setattr(
            "clawjournal.workbench.index.create_share",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RevisionConflictError([{"session_id": "sess-0"}])
            ),
        )
        args = MagicMock(
            session_ids=[], status="approved", note=None,
            force=False, json=False, preview=False,
        )

        with pytest.raises(SystemExit):
            _run_share(args)

        assert "changed after review" in capsys.readouterr().out


class TestVerifyEmail:
    def test_verify_email_status_requires_refresh_when_token_missing(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {"verified_email": "test@university.edu"})
        monkeypatch.setattr("sys.argv", ["clawjournal", "verify-email"])
        with pytest.raises(SystemExit):
            main()
        payload = json.loads(capsys.readouterr().out)
        assert "No active upload token" in payload["error"]

    def test_verify_email_status_accepts_iso_expiry(self, monkeypatch, capsys):
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {
            "verified_email": "test@university.edu",
            "verified_email_token": "token",
            "verified_email_token_expires_at": expires_at,
        })
        monkeypatch.setattr("sys.argv", ["clawjournal", "verify-email"])

        main()

        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "verified"

    def test_verify_email_request_outputs_next_command(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon.request_email_verification", lambda email: {"ok": True})
        monkeypatch.setattr("sys.argv", ["clawjournal", "verify-email", "test@university.edu"])
        main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["status"] == "verification_sent"
        assert payload["next_command"] == "clawjournal verify-email test@university.edu --code <CODE>"

    def test_verify_email_request_suppresses_dev_code_on_prod(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon._is_edu_email", lambda email: True)
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.request_email_verification",
            lambda email: {"status": "ok", "dev_code": "424242"},
        )
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_is_local_dev", lambda: False)
        monkeypatch.setattr("sys.argv", ["clawjournal", "verify-email", "test@university.edu"])
        main()
        payload = json.loads(capsys.readouterr().out)
        assert "dev_code" not in payload

    def test_verify_email_request_surfaces_dev_code_on_local(self, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.cli.load_config", lambda: {})
        monkeypatch.setattr("clawjournal.workbench.daemon._is_edu_email", lambda email: True)
        monkeypatch.setattr(
            "clawjournal.workbench.daemon.request_email_verification",
            lambda email: {"status": "ok", "dev_code": "424242"},
        )
        monkeypatch.setattr("clawjournal.workbench.daemon._hosted_is_local_dev", lambda: True)
        monkeypatch.setattr("sys.argv", ["clawjournal", "verify-email", "test@university.edu"])
        main()
        payload = json.loads(capsys.readouterr().out)
        assert payload["dev_code"] == "424242"


class TestScore:
    def test_set_score_can_override_failure_value(self, monkeypatch, capsys):
        from clawjournal.cli import _run_set_score

        conn = MagicMock()
        calls = []

        monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: conn)
        monkeypatch.setattr(
            "clawjournal.workbench.index.get_session_detail",
            lambda conn_arg, sid: {"ai_scoring_detail": "{}"},
        )

        def fake_update_session(conn_arg, session_id, **updates):
            calls.append((conn_arg, session_id, updates))
            return True

        monkeypatch.setattr("clawjournal.workbench.index.update_session", fake_update_session)

        args = MagicMock(
            session_ids=["sess-1"],
            quality=None,
            failure_value=4,
            failure_evidence=["Trace-backed failure evidence."],
            reason="Trace-backed failure pattern.",
        )
        _run_set_score(args)

        output = json.loads(capsys.readouterr().out)
        assert output["failure_value"] == 4
        assert output["failure_evidence"] == ["Trace-backed failure evidence."]
        assert output["results"][0]["ai_failure_value_score"] == 4
        assert len(calls) == 1
        assert calls[0][0] is conn
        assert calls[0][1] == "sess-1"
        assert calls[0][2]["ai_failure_value_score"] == 4
        assert calls[0][2]["ai_score_reason"] == "Trace-backed failure pattern."
        assert json.loads(calls[0][2]["ai_scoring_detail"]) == {
            "ai_failure_evidence": ["Trace-backed failure evidence."],
        }
        conn.close.assert_called_once()

    def test_set_score_requires_evidence_for_high_failure_value(self, monkeypatch, capsys):
        from clawjournal.cli import _run_set_score

        conn = MagicMock()
        monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: conn)
        monkeypatch.setattr(
            "clawjournal.workbench.index.get_session_detail",
            lambda conn_arg, sid: {"ai_scoring_detail": "{}"},
        )

        args = MagicMock(
            session_ids=["sess-1"],
            quality=None,
            failure_value=4,
            failure_evidence=[],
            reason="Needs promotion.",
        )

        with pytest.raises(SystemExit):
            _run_set_score(args)

        output = json.loads(capsys.readouterr().out)
        assert "require --failure-evidence" in output["error"]
        assert output["session_ids"] == ["sess-1"]
        conn.close.assert_called_once()

    def test_set_score_requires_at_least_one_score(self, monkeypatch, capsys):
        from clawjournal.cli import _run_set_score

        args = MagicMock(
            session_ids=["sess-1"],
            quality=None,
            failure_value=None,
            failure_evidence=[],
            reason=None,
        )

        with pytest.raises(SystemExit):
            _run_set_score(args)

        output = json.loads(capsys.readouterr().out)
        assert "Provide --failure-value" in output["error"]

    def test_score_view_prints_failure_value_first(self, monkeypatch, capsys):
        from clawjournal.cli import _run_score_view

        conn = MagicMock()
        monkeypatch.setattr("clawjournal.workbench.index.open_index", lambda: conn)
        monkeypatch.setattr(
            "clawjournal.workbench.index.get_session_detail",
            lambda conn_arg, sid: {
                "session_id": sid,
                "source": "codex",
                "model": "gpt-5",
                "project": "demo",
                "duration_seconds": 120,
                "input_tokens": 1000,
                "output_tokens": 500,
                "user_messages": 1,
                "assistant_messages": 1,
                "task_type": "debugging",
                "outcome_badge": "resolved",
                "sensitivity_score": 0.0,
                "value_badges": [],
                "risk_badges": [],
                "ai_failure_value_score": 4,
                "ai_quality_score": 2,
                "ai_failure_modes": ["reasoning_fabrication"],
                "ai_recovery_labels": ["user_corrected_recovery"],
                "ai_failure_attribution": "agent_caused",
                "ai_learning_summary": "The user corrected a fabricated API call.",
                "ai_scoring_detail": json.dumps({
                    "ai_failure_evidence": ["User corrected the nonexistent call."],
                }),
                "messages": [],
                "files_touched": [],
                "commands_run": [],
            },
        )

        _run_score_view(MagicMock(batch=False, session_ids=["sess-1"]))

        output = capsys.readouterr().out
        assert "Failure value: 4/5 | Productivity: 2/5" in output
        assert "Failure modes: reasoning_fabrication" in output
        assert "Evidence:" in output
        assert "User corrected the nonexistent call." in output
        conn.close.assert_called_once()

    def test_auto_triage_only_blocks_low_failure_value_productivity_noise(self):
        from clawjournal.cli import _is_auto_triage_noise

        assert _is_auto_triage_noise({
            "ai_quality_score": 1,
            "ai_failure_value_score": 1,
        })
        assert _is_auto_triage_noise({
            "ai_quality_score": 1,
            "ai_failure_value_score": 2,
        })
        assert not _is_auto_triage_noise({
            "ai_quality_score": 1,
            "ai_failure_value_score": 3,
        })
        assert not _is_auto_triage_noise({
            "ai_quality_score": 1,
            "ai_failure_value_score": 4,
        })
        assert not _is_auto_triage_noise({
            "ai_quality_score": 1,
            "ai_failure_value_score": None,
        })

    def test_score_single_session_returns_error_on_judge_failure(self, monkeypatch):
        from clawjournal.cli import _score_single_session

        monkeypatch.setattr(
            "clawjournal.scoring.scoring.score_session",
            lambda conn, session_id, model=None, backend="auto": (_ for _ in ()).throw(RuntimeError("backend auth failed")),
        )

        result = _score_single_session(object(), "sess-1")
        assert result["session_id"] == "sess-1"
        assert "Judge failed: backend auth failed" in result["error"]

    def test_score_help_includes_default_limit_20(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["clawjournal", "score", "--help"])
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 0
        out = capsys.readouterr().out
        assert "Max sessions for batch mode (default: 20)" in out
        assert "failure-corpus" in out

    def test_failure_corpus_alias_matches_legacy_failure_v1(self):
        assert _normalize_score_source_filter("failure-corpus") == _normalize_score_source_filter("failure-v1")

    def test_score_batch_window_filters_recent_sessions(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path / "clawjournal_config")

        from clawjournal.cli import _run_score_batch
        from clawjournal.workbench.index import open_index, upsert_sessions

        now = datetime.now(timezone.utc)
        conn = open_index()
        upsert_sessions(conn, [
            {
                "session_id": "old-sess",
                "project": "test-project",
                "source": "claude",
                "model": "claude-sonnet-4",
                "start_time": (now - timedelta(days=30)).isoformat(),
                "messages": [{"role": "user", "content": "Old"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            },
            {
                "session_id": "recent-sess",
                "project": "test-project",
                "source": "codex",
                "model": "gpt-5",
                "start_time": (now - timedelta(days=1)).isoformat(),
                "messages": [{"role": "user", "content": "Recent"}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0},
            },
        ])
        conn.close()

        _run_score_batch(MagicMock(limit=50, source="failure-corpus", window=7))

        output = json.loads(capsys.readouterr().out)
        assert [row["session_id"] for row in output] == ["recent-sess"]

    def test_share_preview_json(self, bundle_index, capsys):
        """share --preview --json outputs session list as JSON."""
        from clawjournal.cli import _run_share
        args = MagicMock(session_ids=[], status="approved", note=None,
                         force=False, json=True, preview=True)
        _run_share(args)
        output = json.loads(capsys.readouterr().out)
        assert "sessions" in output
        assert output["total"] == 3

    def test_share_no_sessions_exits(self, bundle_index):
        from clawjournal.cli import _run_share
        args = MagicMock(session_ids=[], status=None, note=None,
                         force=False, json=False, preview=False)
        with pytest.raises(SystemExit):
            _run_share(args)


class TestPiiCli:
    def test_pii_review_and_apply(self, tmp_path, monkeypatch, capsys):
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        output_file = tmp_path / "output.jsonl"
        session = {
            "session_id": "s1",
            "messages": [
                {"content": '{"sender_id":"7859110712","name":"Jane D","username":"janedoe42"}'}
            ],
        }
        input_file.write_text(json.dumps(session) + "\n")

        monkeypatch.setattr("clawjournal.cli.review_session_pii_hybrid", lambda session, ignore_llm_errors=True, **kw: [
            {
                "session_id": "s1",
                "message_index": 0,
                "field": "content",
                "entity_text": "7859110712",
                "entity_type": "user_id",
                "confidence": 0.98,
                "reason": "id",
                "replacement": "[REDACTED_USER_ID]",
                "source": "rule",
            },
            {
                "session_id": "s1",
                "message_index": 0,
                "field": "content",
                "entity_text": "Jane D",
                "entity_type": "person_name",
                "confidence": 0.95,
                "reason": "name",
                "replacement": "[REDACTED_NAME]",
                "source": "rule",
            },
            {
                "session_id": "s1",
                "message_index": 0,
                "field": "content",
                "entity_text": "janedoe42",
                "entity_type": "username",
                "confidence": 0.98,
                "reason": "username",
                "replacement": "[REDACTED_USERNAME]",
                "source": "rule",
            },
        ])
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--json",
        ])
        main()
        review_output = json.loads(capsys.readouterr().out)
        assert review_output["provider"] == "hybrid"
        assert review_output["finding_count"] >= 3

        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-apply",
            "--file", str(input_file),
            "--findings", str(findings_file),
            "--output", str(output_file),
            "--json",
        ])
        main()
        apply_output = json.loads(capsys.readouterr().out)
        assert apply_output["replacements"] >= 3
        data = json.loads(output_file.read_text().strip())
        assert "Jane D" not in json.dumps(data)
        assert "janedoe42" not in json.dumps(data)

    def _make_ai_provider_mock(self, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.review_session_pii_with_agent", lambda session, **kw: [{
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.95,
            "reason": "name",
            "replacement": "[REDACTED_NAME]",
            "source": "ai",
        }])

    def test_pii_review_ai_provider(self, tmp_path, monkeypatch, capsys):
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": [{"content": "Jane D"}]}) + "\n")
        self._make_ai_provider_mock(monkeypatch)
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "ai",
            "--json",
        ])
        main()
        output = json.loads(capsys.readouterr().out)
        assert output["provider"] == "ai"
        assert output["finding_count"] == 1

    def test_pii_review_claude_provider_backward_compat(self, tmp_path, monkeypatch, capsys):
        """Legacy --provider claude is normalized to the generic ai provider."""
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": [{"content": "Jane D"}]}) + "\n")
        self._make_ai_provider_mock(monkeypatch)
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "claude",
            "--json",
        ])
        main()
        output = json.loads(capsys.readouterr().out)
        assert output["provider"] == "ai"
        assert output["finding_count"] == 1

    def test_collect_pii_findings_normalizes_claude_alias(self, monkeypatch):
        from clawjournal.cli import _collect_pii_findings

        called_backends = []

        def fake_agent_review(session, backend="auto", **kw):
            called_backends.append(backend)
            return []

        monkeypatch.setattr("clawjournal.cli.review_session_pii_with_agent", fake_agent_review)
        _collect_pii_findings(
            [{"session_id": "s1", "messages": [{"content": "Alice"}]}],
            "claude",
            backend="codex",
        )
        assert called_backends == ["codex"]

    def test_pii_review_hybrid_tolerates_llm_failure(self, tmp_path, monkeypatch, capsys):
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": [{"content": '{"name":"Jane D"}'}]}) + "\n")
        monkeypatch.setattr("clawjournal.cli.review_session_pii_hybrid", lambda session, ignore_llm_errors=True, **kw: [{
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.95,
            "reason": "name",
            "replacement": "[REDACTED_NAME]",
            "source": "rule",
        }])
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "hybrid",
            "--json",
        ])
        main()
        output = json.loads(capsys.readouterr().out)
        assert output["provider"] == "hybrid"
        assert output["finding_count"] == 1

    def test_apply_pii_findings_helper(self, tmp_path):
        from clawjournal.cli import _apply_pii_findings
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        output_file = tmp_path / "output.jsonl"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": [{"content": "Jane D"}]}) + "\n")
        findings_file.write_text(json.dumps({"findings": [{
            "session_id": "s1",
            "message_index": 0,
            "field": "content",
            "entity_text": "Jane D",
            "entity_type": "person_name",
            "confidence": 0.95,
            "replacement": "[REDACTED_NAME]"
        }]}))
        result = _apply_pii_findings(input_file, findings_file, output_file)
        assert result["replacements"] == 1
        assert "[REDACTED_NAME]" in output_file.read_text()

    def test_pii_review_unimplemented_provider(self, tmp_path, monkeypatch):
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": []}) + "\n")
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "codex",
        ])
        with pytest.raises(SystemExit):
            main()

    def test_pii_review_passes_backend(self, tmp_path, monkeypatch, capsys):
        """--backend flag on pii-review is threaded to the dispatcher."""
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(json.dumps({"session_id": "s1", "messages": [{"content": "Alice"}]}) + "\n")
        called_backends = []

        def fake_agent_review(session, backend="auto", **kw):
            called_backends.append(backend)
            return []

        monkeypatch.setattr("clawjournal.cli.review_session_pii_with_agent", fake_agent_review)
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "ai",
            "--backend", "codex",
            "--json",
        ])
        main()
        assert called_backends == ["codex"]

    def test_pii_review_progress_output(self, tmp_path, monkeypatch, capsys):
        """Non-rules providers print per-session progress to stderr."""
        input_file = tmp_path / "input.jsonl"
        findings_file = tmp_path / "findings.json"
        input_file.write_text(
            json.dumps({"session_id": "abc123", "messages": [{"content": "x"}]}) + "\n"
            + json.dumps({"session_id": "def456", "messages": [{"content": "y"}]}) + "\n"
        )
        monkeypatch.setattr("clawjournal.cli.review_session_pii_with_agent", lambda s, **kw: [])
        monkeypatch.setattr("sys.argv", [
            "clawjournal", "pii-review",
            "--file", str(input_file),
            "--output", str(findings_file),
            "--provider", "ai",
            "--json",
        ])
        main()
        stderr = capsys.readouterr().err
        assert "[1/2]" in stderr
        assert "[2/2]" in stderr


class TestHasSessionSourcesClaude:
    def test_claude_dir_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.CLAUDE_DIR", tmp_path)
        monkeypatch.setattr("clawjournal.cli.LOCAL_AGENT_DIR", tmp_path / "nonexistent")
        assert _has_session_sources("claude") is True

    def test_local_agent_dir_only(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.CLAUDE_DIR", tmp_path / "nonexistent")
        monkeypatch.setattr("clawjournal.cli.LOCAL_AGENT_DIR", tmp_path)
        assert _has_session_sources("claude") is True

    def test_neither_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.CLAUDE_DIR", tmp_path / "nonexistent1")
        monkeypatch.setattr("clawjournal.cli.LOCAL_AGENT_DIR", tmp_path / "nonexistent2")
        assert _has_session_sources("claude") is False

    def test_auto_includes_local_agent(self, tmp_path, monkeypatch):
        monkeypatch.setattr("clawjournal.cli.CLAUDE_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.LOCAL_AGENT_DIR", tmp_path)
        monkeypatch.setattr("clawjournal.cli.CODEX_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.CUSTOM_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.GEMINI_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.KIMI_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.OPENCODE_DIR", tmp_path / "no")
        monkeypatch.setattr("clawjournal.cli.OPENCLAW_DIR", tmp_path / "no")
        assert _has_session_sources("auto") is True
