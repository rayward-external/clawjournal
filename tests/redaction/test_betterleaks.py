"""Tests for the Betterleaks scanner wrapper.

The subprocess contract is faked by intercepting ``subprocess.run``,
locating ``--report-path`` in the argv, and writing report JSON there —
mirroring how the real binary communicates (verified against
betterleaks 1.6.1; ``fixtures/betterleaks_report.json`` is a vendored
real report from that binary).
"""

import json
import subprocess
from pathlib import Path

import pytest

from clawjournal.redaction import betterleaks

FIXTURE_REPORT = Path(__file__).parent / "fixtures" / "betterleaks_report.json"


def _report_path_from(cmd: list[str]) -> Path:
    return Path(cmd[cmd.index("--report-path") + 1])


def _fake_run_writing(payload: str, returncode: int, capture: dict | None = None):
    """A subprocess.run stand-in that writes ``payload`` to the argv's
    --report-path, like the real binary does."""

    def fake_run(cmd, **kwargs):
        if capture is not None:
            capture["argv"] = cmd
            capture["kwargs"] = kwargs
        _report_path_from(cmd).write_text(payload)
        return subprocess.CompletedProcess(cmd, returncode, stdout=b"", stderr=b"")

    return fake_run


class TestPlaceholderForRule:
    def test_normalizes_rule_id(self):
        assert betterleaks.placeholder_for_rule("slack-bot-token") == (
            "[REDACTED_SLACK_BOT_TOKEN]"
        )
        assert betterleaks.placeholder_for_rule("private-key") == "[REDACTED_PRIVATE_KEY]"

    def test_empty_rule_falls_back(self):
        assert betterleaks.placeholder_for_rule("--") == "[REDACTED_BETTERLEAKS]"

    def test_placeholders_match_bundled_allowlist(self):
        # The bundled TOML allowlists [REDACTED_*] so the redact-and-rescan
        # loop converges; every placeholder this module emits must match it.
        import re

        allowlist_re = re.compile(r"\[REDACTED(?:_[A-Z0-9_]+)?\]")
        for rule in ("slack-bot-token", "private-key", "aws-secret-access-key", "--"):
            assert allowlist_re.fullmatch(betterleaks.placeholder_for_rule(rule))


class TestParseFinding:
    def _fixture_entries(self) -> list[dict]:
        return json.loads(FIXTURE_REPORT.read_text())

    def test_parses_real_report_entry(self):
        entry = next(
            e for e in self._fixture_entries() if e["RuleID"] == "slack-bot-token"
        )
        finding = betterleaks._parse_finding(entry)
        assert finding is not None
        assert finding.rule_id == "slack-bot-token"
        assert finding.line == 1
        assert finding.entropy == pytest.approx(4.97, abs=0.01)
        assert finding.raw_sha256 is not None and finding.raw_sha256.startswith("sha256:")
        # Masked keeps only edges of the secret.
        assert entry["Secret"] not in finding.masked
        assert finding.masked.startswith("xoxb")

    def test_escaped_private_key_is_parsed_in_file_level_form(self):
        # Betterleaks reports secrets as the bytes on disk: a key inside a
        # JSONL string arrives with literal \n escape sequences intact.
        entry = next(
            e for e in self._fixture_entries() if e["RuleID"] == "private-key"
        )
        assert "\\n" in entry["Secret"] and "\n" not in entry["Secret"]
        finding = betterleaks._parse_finding(entry)
        assert finding is not None
        assert finding.line == 2

    def test_missing_rule_id_returns_none(self):
        assert betterleaks._parse_finding({"Secret": "x"}) is None
        assert betterleaks._parse_finding({"RuleID": "", "Secret": "x"}) is None

    def test_missing_secret_still_reports_with_generic_mask(self):
        finding = betterleaks._parse_finding({"RuleID": "some-rule", "StartLine": 3})
        assert finding is not None
        assert finding.masked == "[REDACTED]"
        assert finding.raw_sha256 is None

    def test_invalid_start_line_is_dropped(self):
        finding = betterleaks._parse_finding({"RuleID": "r", "StartLine": 0})
        assert finding is not None and finding.line is None


class TestReadReportPayload:
    def test_clean_scan_null_normalizes_to_empty(self, tmp_path):
        report = tmp_path / "r.json"
        report.write_text("null\n")
        assert betterleaks._read_report_payload(report) == []

    def test_findings_array_passes_through(self, tmp_path):
        report = tmp_path / "r.json"
        report.write_text('[{"RuleID": "x"}, "not-a-dict"]')
        assert betterleaks._read_report_payload(report) == [{"RuleID": "x"}]

    def test_missing_empty_or_malformed_is_none(self, tmp_path):
        assert betterleaks._read_report_payload(tmp_path / "absent.json") is None
        empty = tmp_path / "empty.json"
        empty.write_text("")
        assert betterleaks._read_report_payload(empty) is None
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        assert betterleaks._read_report_payload(bad) is None
        scalar = tmp_path / "scalar.json"
        scalar.write_text('"unexpected"')
        assert betterleaks._read_report_payload(scalar) is None


class TestScanFile:
    def _enable_real_scan(self, monkeypatch):
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: True)
        # Pin resolution so argv assertions don't depend on whether the
        # machine running the tests has a real binary installed.
        monkeypatch.setattr(betterleaks, "resolve_binary", lambda: "betterleaks")

    def test_bypass_env_var_short_circuits(self, tmp_path, monkeypatch):
        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")
        # Autouse fixture already sets SKIP_ENV_VAR=1; verify bypass path.

        def fake_run(*args, **kwargs):
            raise AssertionError("subprocess should not run under bypass")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = betterleaks.scan_file(target)
        assert report.bypassed is True
        assert report.findings == []

    def test_missing_binary_is_reported(self, tmp_path, monkeypatch):
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: False)
        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")

        report = betterleaks.scan_file(target)
        assert report.binary_missing is True
        assert report.scan_error is None

    def test_clean_scan_argv_contract(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text('{"hello":"world"}\n')
        seen: dict = {}

        monkeypatch.setattr(subprocess, "run", _fake_run_writing("null", 0, seen))
        report = betterleaks.scan_file(target)

        assert report.findings == []
        assert report.binary_missing is False
        assert report.scan_error is None
        cmd = seen["argv"]
        assert cmd[0] == "betterleaks"
        assert cmd[1] == "dir"
        assert cmd[2] == str(target)
        assert "--report-format" in cmd and cmd[cmd.index("--report-format") + 1] == "json"
        assert "--exit-code" in cmd and cmd[cmd.index("--exit-code") + 1] == "183"
        assert "--no-banner" in cmd
        # The bundled config ships in-repo, so every scan passes it.
        assert "--config" in cmd
        assert cmd[cmd.index("--config") + 1] == str(betterleaks.bundled_config_path())
        # Live validation must NEVER be enabled: candidate secrets stay local.
        assert all(not str(arg).startswith("--validation") for arg in cmd)
        # Non-streaming scans get DEVNULL stdin; env is scrubbed, not inherited.
        assert seen["kwargs"].get("stdin") is subprocess.DEVNULL
        assert isinstance(seen["kwargs"].get("env"), dict)

    def test_findings_parsed_from_real_report_fixture(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        fixture_text = FIXTURE_REPORT.read_text()

        monkeypatch.setattr(subprocess, "run", _fake_run_writing(fixture_text, 183))
        report = betterleaks.scan_file(target)

        assert [f.rule_id for f in report.findings] == ["slack-bot-token", "private-key"]
        assert [f.line for f in report.findings] == [1, 2]  # sorted by line
        assert report.top_rules == ["private-key", "slack-bot-token"]
        # Raw secrets never appear in the summary or persisted report.
        for secret in (f["Secret"] for f in json.loads(fixture_text)):
            assert secret not in json.dumps(report.summary())
        out = tmp_path / "betterleaks.json"
        betterleaks.write_report(out, report)
        persisted = out.read_text()
        for secret in (f["Secret"] for f in json.loads(fixture_text)):
            assert secret not in persisted
        # Only the basename of the scanned path is persisted.
        assert json.loads(persisted)["scanned_path"] == "sessions.jsonl"

    def test_duplicate_findings_are_collapsed(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        entry = json.loads(FIXTURE_REPORT.read_text())[0]
        payload = json.dumps([entry, dict(entry)])

        monkeypatch.setattr(subprocess, "run", _fake_run_writing(payload, 183))
        report = betterleaks.scan_file(target)
        assert len(report.findings) == 1

    def test_unexpected_exit_status_is_scan_error(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        monkeypatch.setattr(subprocess, "run", _fake_run_writing("null", 126))
        report = betterleaks.scan_file(target)
        assert report.scan_error == "unexpected exit status 126"

    def test_timeout_is_scan_error(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=120)

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = betterleaks.scan_file(target)
        assert "timed out" in (report.scan_error or "")

    def test_spawn_failure_is_scan_error(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        def fake_run(cmd, **kwargs):
            raise OSError("exec format error")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = betterleaks.scan_file(target)
        assert report.scan_error == "could not execute the betterleaks binary"

    def test_missing_report_file_is_scan_error(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        def fake_run(cmd, **kwargs):
            # Simulate a binary that exited cleanly but wrote no report:
            # unlink the mkstemp placeholder the wrapper created.
            _report_path_from(cmd).unlink()
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = betterleaks.scan_file(target)
        assert report.scan_error == "scan produced no readable JSON report"

    def test_unhashable_target_is_scan_error(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        report = betterleaks.scan_file(tmp_path / "does-not-exist.jsonl")
        assert report.scan_error is not None
        assert "could not hash scan target" in report.scan_error

    def test_report_tempfile_is_always_unlinked(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        seen: dict = {}

        monkeypatch.setattr(
            subprocess, "run", _fake_run_writing(FIXTURE_REPORT.read_text(), 183, seen)
        )
        betterleaks.scan_file(target)
        # The raw-bearing report file must not outlive the call.
        assert not _report_path_from(seen["argv"]).exists()


class TestScanText:
    def test_delegates_to_scan_file_and_cleans_up(self, monkeypatch):
        captured: dict = {}

        def fake_scan_file(path):
            captured["path"] = Path(path)
            captured["content"] = Path(path).read_text()
            return betterleaks.BetterleaksReport(
                scanned_path=str(path), scanned_sha256="sha256:x"
            )

        monkeypatch.setattr(betterleaks, "scan_file", fake_scan_file)
        report = betterleaks.scan_text("some session text")
        assert captured["content"] == "some session text"
        assert not captured["path"].exists()  # temp file removed
        assert report.scanned_sha256 == "sha256:x"


class TestRawMatches:
    def _enable(self, monkeypatch):
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: True)
        monkeypatch.setattr(betterleaks, "resolve_binary", lambda: "betterleaks")

    def test_file_scan_returns_raw_line_and_entropy(self, tmp_path, monkeypatch):
        self._enable(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        monkeypatch.setattr(
            subprocess, "run", _fake_run_writing(FIXTURE_REPORT.read_text(), 183)
        )
        matches = betterleaks._scan_file_for_raw_matches(target)

        by_rule = {m["rule_id"]: m for m in matches}
        assert set(by_rule) == {"private-key", "slack-bot-token"}
        slack = by_rule["slack-bot-token"]
        assert slack["raw"].startswith("xoxb-")
        assert slack["line"] == 1
        assert slack["entropy"] == pytest.approx(4.97, abs=0.01)

    def test_stdin_scan_streams_payload(self, monkeypatch):
        self._enable(monkeypatch)
        seen: dict = {}

        monkeypatch.setattr(subprocess, "run", _fake_run_writing("null", 0, seen))
        matches = betterleaks._scan_text_for_raw_matches("scan this text")

        assert matches == []
        assert seen["argv"][1] == "stdin"
        assert seen["kwargs"]["input"] == b"scan this text"
        assert "stdin" not in seen["kwargs"]  # input= replaces stdin=DEVNULL

    def test_bypass_and_missing_binary_fail_soft(self, tmp_path, monkeypatch):
        # Autouse fixture sets the bypass env var.
        assert betterleaks._scan_text_for_raw_matches("text") == []
        assert betterleaks._scan_file_for_raw_matches(tmp_path / "x") == []

        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: False)
        assert betterleaks._scan_text_for_raw_matches("text") == []

    def test_scan_error_fails_soft_to_empty(self, tmp_path, monkeypatch):
        self._enable(monkeypatch)

        def fake_run(cmd, **kwargs):
            raise OSError("boom")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert betterleaks._scan_text_for_raw_matches("text") == []
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        assert betterleaks._scan_file_for_raw_matches(target) == []

    def test_oversized_payload_is_skipped(self, monkeypatch):
        self._enable(monkeypatch)
        monkeypatch.setattr(betterleaks, "_MAX_SCAN_BYTES", 8)
        assert betterleaks._scan_text_for_raw_matches("longer than eight") == []


class TestSessionFindings:
    def test_matches_are_relocated_per_field(self, monkeypatch):
        raw = "xoxb-FAKE0FAKE0FAKE-FIXTURE0TOKEN-AbCdEfGhIjKlMnOpQrStUvWx"
        monkeypatch.setattr(
            betterleaks,
            "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "rule_id": "slack-bot-token", "entropy": 4.9, "line": 1}
            ],
        )
        session = {
            "display_title": f"token {raw} in title",
            "messages": [
                {"content": f"first {raw} second {raw}"},
                {"tool_uses": [{"input": {"command": f"echo {raw}"}}]},
            ],
        }

        findings = betterleaks.scan_session_for_betterleaks_findings(session)

        assert len(findings) == 4  # 1 title + 2 content + 1 tool input
        assert {f.engine for f in findings} == {betterleaks.BETTERLEAKS_ENGINE_ID}
        assert {f.rule for f in findings} == {"slack-bot-token"}
        title_hit = next(f for f in findings if f.field == "display_title")
        assert title_hit.offset == len("token ")
        assert title_hit.length == len(raw)
        content_hits = [f for f in findings if f.field == "content"]
        assert [f.offset for f in content_hits] == [
            len("first "),
            len(f"first {raw} second "),
        ]

    def test_short_raws_are_ignored(self, monkeypatch):
        monkeypatch.setattr(
            betterleaks,
            "_scan_text_for_raw_matches",
            lambda text: [{"raw": "ab", "rule_id": "r", "entropy": None, "line": None}],
        )
        session = {"messages": [{"content": "ab"}]}
        assert betterleaks.scan_session_for_betterleaks_findings(session) == []

    def test_no_matches_short_circuits(self, monkeypatch):
        monkeypatch.setattr(betterleaks, "_scan_text_for_raw_matches", lambda text: [])
        assert betterleaks.scan_session_for_betterleaks_findings({"messages": []}) == []


class TestSecretMapFromBlob:
    def test_maps_raw_to_rule_placeholder(self, monkeypatch):
        raw = "xoxb-FAKE0FAKE0FAKE-FIXTURE0TOKEN-AbCdEfGhIjKlMnOpQrStUvWx"
        monkeypatch.setattr(
            betterleaks,
            "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "rule_id": "slack-bot-token", "entropy": 4.9, "line": 1}
            ],
        )
        out = betterleaks.betterleaks_secret_map_from_blob({"messages": []}, {})
        assert out == {raw: "[REDACTED_SLACK_BOT_TOKEN]"}

    def test_ignored_decision_skips_raw(self, monkeypatch):
        from clawjournal.findings import hash_entity

        raw = "xoxb-FAKE0FAKE0FAKE-FIXTURE0TOKEN-AbCdEfGhIjKlMnOpQrStUvWx"
        monkeypatch.setattr(
            betterleaks,
            "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "rule_id": "slack-bot-token", "entropy": 4.9, "line": 1}
            ],
        )
        decisions = {hash_entity(raw): "ignored"}
        assert betterleaks.betterleaks_secret_map_from_blob({}, decisions) == {}

    def test_overlapping_rules_pick_deterministic_placeholder(self, monkeypatch):
        raw = "shared-raw-value-123456789"
        monkeypatch.setattr(
            betterleaks,
            "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "rule_id": "zzz-rule", "entropy": None, "line": 1},
                {"raw": raw, "rule_id": "aaa-rule", "entropy": None, "line": 1},
            ],
        )
        out = betterleaks.betterleaks_secret_map_from_blob({}, {})
        # (rule_id, raw) ascending → "aaa-rule" wins regardless of scan order.
        assert out == {raw: "[REDACTED_AAA_RULE]"}


class TestEngineFingerprint:
    def _fingerprint_with(self, monkeypatch, tmp_path, banner: str) -> str:
        binary = tmp_path / "betterleaks"
        binary.write_bytes(b"#!/bin/sh\n")
        binary.chmod(0o755)
        monkeypatch.setattr(betterleaks, "resolve_binary", lambda: str(binary))
        betterleaks.reset_version_cache()

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 0, stdout=banner, stderr=""
            ),
        )
        try:
            return betterleaks.engine_fingerprint()
        finally:
            betterleaks.reset_version_cache()

    def test_parses_version_flag_banner(self, monkeypatch, tmp_path):
        # `betterleaks --version` prints "betterleaks version 1.6.1".
        assert self._fingerprint_with(
            monkeypatch, tmp_path, "betterleaks version 1.6.1\n"
        ) == "betterleaks 1.6.1"

    def test_parses_bare_semver_banner(self, monkeypatch, tmp_path):
        # The `version` subcommand prints a bare "1.6.1".
        assert self._fingerprint_with(monkeypatch, tmp_path, "1.6.1\n") == (
            "betterleaks 1.6.1"
        )

    def test_missing_binary_reports_missing(self, monkeypatch):
        monkeypatch.setattr(betterleaks, "resolve_binary", lambda: None)
        betterleaks.reset_version_cache()
        try:
            assert betterleaks.engine_fingerprint() == "missing"
        finally:
            betterleaks.reset_version_cache()
