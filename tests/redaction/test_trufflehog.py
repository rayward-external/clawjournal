"""Tests for the TruffleHog share-time gate."""

import json
import os
import subprocess
from pathlib import Path

import pytest

from clawjournal.redaction import trufflehog


class TestMaskSecret:
    def test_short_value_fully_hidden(self):
        assert trufflehog.mask_secret("abc") == "***"
        assert trufflehog.mask_secret("12345678") == "***"

    def test_generic_keeps_4_prefix_4_suffix(self):
        raw = "sk-abcdef1234567890"
        masked = trufflehog.mask_secret(raw)
        assert masked.startswith("sk-a")
        assert masked.endswith("7890")
        assert "***" in masked
        assert raw not in masked

    def test_npm_tokens_keep_longer_prefix(self):
        raw = "npm_1234567890abcdefGHIJ"
        masked = trufflehog.mask_secret(raw)
        # npm_ prefix keeps 8 leading chars so reviewers recognize the type.
        assert masked.startswith("npm_1234")
        assert masked.endswith("GHIJ")


class TestStatusClassification:
    """VerificationError is a top-level field on TruffleHog's JSONL
    output (see pkg/output/json.go); a nested-only probe was a
    correctness bug that misclassified every verification failure as
    ``unverified`` instead of ``unknown``."""

    def test_top_level_verification_error_is_unknown(self):
        record = {
            "DetectorName": "Stripe",
            "Verified": False,
            "VerificationError": "dial tcp: lookup api.stripe.com: no such host",
            "Raw": "synthetic_stripe_abcdefghijklmnopqrstuvwxyzABCDEF",
        }
        assert trufflehog._classify_trufflehog_status(record) == "unknown"

    def test_nested_verification_error_still_classified_as_unknown(self):
        # Defensive fallback: older / alternate TruffleHog versions
        # that put the error under ExtraData.
        record = {
            "DetectorName": "Stripe",
            "Verified": False,
            "ExtraData": {"verification_error": "connection refused"},
            "Raw": "synthetic_stripe_abcdef",
        }
        assert trufflehog._classify_trufflehog_status(record) == "unknown"

    def test_empty_verification_error_is_unverified(self):
        record = {
            "DetectorName": "Stripe",
            "Verified": False,
            "VerificationError": "",  # present but empty per TruffleHog's `omitempty` behavior
            "Raw": "synthetic_stripe_abcdef",
        }
        assert trufflehog._classify_trufflehog_status(record) == "unverified"


class TestParseFinding:
    def test_verified_wins_over_error(self):
        record = {
            "DetectorName": "GitHub",
            "Verified": True,
            "Raw": "ghp_abc1234567890defghijklmnop",
            "SourceMetadata": {"Data": {"Filesystem": {"line": 42}}},
        }
        finding = trufflehog._parse_finding(record)
        assert finding is not None
        assert finding.status == "verified"
        assert finding.line == 42
        assert finding.raw_sha256 is not None
        assert finding.raw_sha256.startswith("sha256:")
        assert "ghp_" in finding.masked  # prefix preserved
        assert record["Raw"] not in finding.masked  # raw never leaks

    def test_verification_error_classified_as_unknown(self):
        record = {
            "DetectorName": "AWS",
            "Verified": False,
            "ExtraData": {"verification_error": "connection refused"},
            "Raw": "AKIAIOSFODNN7EXAMPLE",
        }
        finding = trufflehog._parse_finding(record)
        assert finding is not None
        assert finding.status == "unknown"

    def test_no_verification_error_is_unverified(self):
        record = {
            "DetectorName": "Stripe",
            "Verified": False,
            "Raw": "synthetic_stripe_abcdefghijklmnopqrstuv",
        }
        finding = trufflehog._parse_finding(record)
        assert finding is not None
        assert finding.status == "unverified"

    def test_missing_detector_returns_none(self):
        assert trufflehog._parse_finding({"Raw": "x"}) is None


class TestScanFile:
    """scan_file exercises the subprocess contract — we mock subprocess.run
    to fake TruffleHog output while asserting the CLI flags we pass."""

    def _enable_real_scan(self, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        # Pin resolution so argv assertions don't depend on whether the
        # machine running the tests has a real binary installed.
        monkeypatch.setattr(trufflehog, "resolve_binary", lambda: "trufflehog")

    def test_bypass_env_var_short_circuits(self, tmp_path, monkeypatch):
        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")
        # Autouse fixture already sets SKIP_ENV_VAR=1; verify bypass path.
        called = {"n": 0}

        def fake_run(*args, **kwargs):
            called["n"] += 1
            raise AssertionError("subprocess should not run under bypass")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = trufflehog.scan_file(target)
        assert report.bypassed is True
        assert report.blocking is False
        assert report.block_reason is None
        assert called["n"] == 0

    def test_missing_binary_reports_blocking(self, tmp_path, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: False)
        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")

        report = trufflehog.scan_file(target)
        assert report.binary_missing is True
        assert report.blocking is True
        assert report.block_reason == "trufflehog-not-installed"

    def test_clean_pass_produces_non_blocking_report(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text('{"hello":"world"}\n')

        def fake_run(cmd, **kwargs):
            assert cmd[0] == "trufflehog"
            assert "filesystem" in cmd
            assert "--no-update" in cmd
            # Detectors known to trip on agent-trace structural content
            # are excluded at the TruffleHog layer.
            assert any(
                arg.startswith("--exclude-detectors=") and "refiner" in arg
                for arg in cmd
            ), f"expected --exclude-detectors=refiner in {cmd}"
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = trufflehog.scan_file(target)
        assert report.findings == []
        assert report.blocking is False
        assert report.binary_missing is False

    def test_findings_are_parsed_deduped_and_block(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        finding = {
            "DetectorName": "GitHub",
            "Verified": True,
            "Raw": "ghp_abc1234567890defghijklmnop",
            "SourceMetadata": {"Data": {"Filesystem": {"line": 7}}},
        }
        duplicate = dict(finding)
        other = {
            "DetectorName": "Slack",
            "Verified": False,
            "Raw": "xoxb-abcdefghij-klmnopqrst-uvwxyz1234567",
            "SourceMetadata": {"Data": {"Filesystem": {"line": 19}}},
        }
        stdout = "\n".join(json.dumps(x) for x in (finding, duplicate, other)) + "\n"

        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 183, stdout=stdout, stderr=""),
        )
        report = trufflehog.scan_file(target)
        assert len(report.findings) == 2  # duplicate collapsed
        assert report.verified == 1
        assert report.unverified == 1
        assert report.blocking is True
        assert report.block_reason == "trufflehog-findings"
        # Ordered by line.
        assert report.findings[0].line == 7
        assert report.findings[1].line == 19
        # Raw values never appear in the public summary.
        summary = report.summary()
        payload = json.dumps(summary)
        assert finding["Raw"] not in payload
        assert other["Raw"] not in payload
        assert "GitHub" in summary["top_detectors"]

    def test_unverified_generic_azure_findings_still_block(self, tmp_path, monkeypatch):
        """The legacy generic Azure detector fires on ordinary agent-session
        terminal content (localhost connection strings, file paths, ANSI color
        codes) and Azure-API verification can return unverified. The export
        gate remains fail-closed: TruffleHog findings still block unless the
        detector is explicitly excluded."""
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        findings = [
            {
                "DetectorName": "Azure",
                "Verified": False,
                "Raw": "127.0.0.1:51678->127.0.0.1:5432:",
                "SourceMetadata": {"Data": {"Filesystem": {"line": 3}}},
            },
            {
                "DetectorName": "Azure",
                "Verified": False,
                "Raw": "scripts/seed-routing-rules.sh\n??",
                "SourceMetadata": {"Data": {"Filesystem": {"line": 3}}},
            },
            {
                "DetectorName": "Azure",
                "Verified": False,
                "Raw": "[90mnull[0m[0m\n",
                "SourceMetadata": {"Data": {"Filesystem": {"line": 5}}},
            },
        ]
        stdout = "\n".join(json.dumps(x) for x in findings) + "\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 183, stdout=stdout, stderr=""),
        )
        report = trufflehog.scan_file(target)
        assert len(report.findings) == 3
        assert report.unverified == 3
        assert report.blocking is True

    def test_verified_generic_azure_still_blocks(self, tmp_path, monkeypatch):
        """A verified generic-Azure finding is a real, live credential and
        must still block."""
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        finding = {
            "DetectorName": "Azure",
            "Verified": True,
            "Raw": "a-real-confirmed-azure-secret-0001",
            "SourceMetadata": {"Data": {"Filesystem": {"line": 4}}},
        }
        stdout = json.dumps(finding) + "\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 183, stdout=stdout, stderr=""),
        )
        report = trufflehog.scan_file(target)
        assert report.verified == 1
        assert report.blocking is True

    def test_specific_azure_detector_unverified_still_blocks(self, tmp_path, monkeypatch):
        """Specific Azure detectors (AzureStorage, etc.) also block even when
        unverified."""
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        finding = {
            "DetectorName": "AzureStorage",
            "Verified": False,
            "Raw": "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y==",
            "SourceMetadata": {"Data": {"Filesystem": {"line": 6}}},
        }
        stdout = json.dumps(finding) + "\n"
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(cmd, 183, stdout=stdout, stderr=""),
        )
        report = trufflehog.scan_file(target)
        assert report.unverified == 1
        assert report.blocking is True

    def test_unexpected_exit_code_blocks_with_error_report(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kwargs: subprocess.CompletedProcess(
                cmd, 2, stdout="", stderr="boom",
            ),
        )
        report = trufflehog.scan_file(target)
        assert report.blocking is True
        assert report.block_reason == "trufflehog-error"
        assert report.scan_error == "unexpected exit status 2"

    def test_timeout_blocks_with_error_report(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        def fake_run(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd=cmd, timeout=60)

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = trufflehog.scan_file(target)
        assert report.blocking is True
        assert report.block_reason == "trufflehog-error"
        assert report.scan_error == "timed out after 60 seconds"

    def test_spawn_failure_blocks_with_error_report(self, tmp_path, monkeypatch):
        self._enable_real_scan(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text("x\n")

        def fake_run(cmd, **kwargs):
            raise OSError("exec format error")

        monkeypatch.setattr(subprocess, "run", fake_run)
        report = trufflehog.scan_file(target)
        assert report.blocking is True
        assert report.block_reason == "trufflehog-error"
        assert report.scan_error == "could not execute the trufflehog binary"

    def test_hash_target_failure_blocks_with_error_report(self, tmp_path, monkeypatch):
        """A permission error or mid-read I/O error reading the scan
        target must surface as a blocking report, not bubble up as a
        raw OSError to the share-export caller (no block_reason would
        otherwise be persisted in the manifest)."""
        self._enable_real_scan(monkeypatch)

        def boom(path):
            raise PermissionError("simulated permission denied")

        monkeypatch.setattr(trufflehog, "_sha256_file", boom)
        report = trufflehog.scan_file(tmp_path / "unreadable.jsonl")
        assert report.blocking is True
        assert report.block_reason == "trufflehog-error"
        assert "PermissionError" in (report.scan_error or "")


class TestScanText:
    def test_scan_text_round_trips_through_temp_file(self, monkeypatch):
        """scan_text writes to a temp file, invokes scan_file, cleans up."""
        seen_paths: list[str] = []

        def fake_scan_file(path):
            seen_paths.append(str(path))
            assert path.exists(), "temp file should exist when scan_file is called"
            assert path.read_text() == '{"hello":"world"}'
            return trufflehog.TruffleHogReport(
                scanned_path=str(path),
                scanned_sha256="sha256:0",
            )

        monkeypatch.setattr(trufflehog, "scan_file", fake_scan_file)
        report = trufflehog.scan_text('{"hello":"world"}')
        assert report.blocking is False
        # Temp file cleaned up after return.
        assert not Path(seen_paths[0]).exists()


class TestWriteReport:
    def test_report_round_trips_without_raw_values(self, tmp_path):
        report = trufflehog.TruffleHogReport(
            scanned_path="/x",
            scanned_sha256="sha256:abcd",
            findings=[
                trufflehog.TruffleHogFinding(
                    detector="GitHub",
                    status="verified",
                    line=1,
                    masked="ghp_a***4567",
                    raw_sha256="sha256:deadbeef",
                )
            ],
            verified=1,
            top_detectors=["GitHub"],
        )
        out = tmp_path / "report.json"
        trufflehog.write_report(out, report)
        payload = json.loads(out.read_text())
        assert payload["summary"]["findings"] == 1
        assert payload["findings"][0]["masked"] == "ghp_a***4567"
        assert "raw" not in json.dumps(payload).lower() or "raw_sha256" in json.dumps(payload)

    def test_engine_version_recorded_in_report_and_summary(self, tmp_path):
        # The export chokepoints stamp `engine` before persisting so each
        # share records which scanner version actually ran (the managed
        # binary can drift from the source pin between installs).
        report = trufflehog.TruffleHogReport(
            scanned_path="/x",
            scanned_sha256="sha256:abcd",
            engine="trufflehog 3.95.5",
        )
        assert report.summary()["engine"] == "trufflehog 3.95.5"
        out = tmp_path / "report.json"
        trufflehog.write_report(out, report)
        payload = json.loads(out.read_text())
        assert payload["engine"] == "trufflehog 3.95.5"
        assert payload["summary"]["engine"] == "trufflehog 3.95.5"

    def test_engine_defaults_to_empty_for_unstamped_reports(self):
        report = trufflehog.TruffleHogReport(scanned_path="/x", scanned_sha256="")
        assert report.engine == ""
        assert report.summary()["engine"] == ""


class TestPlaceholderForDetector:
    def test_normalizes_to_upper_snake(self):
        assert trufflehog.placeholder_for_detector("GitHub") == "[REDACTED_GITHUB]"
        assert trufflehog.placeholder_for_detector("Slack OAuth Token") == "[REDACTED_SLACK_OAUTH_TOKEN]"

    def test_empty_detector_falls_back(self):
        assert trufflehog.placeholder_for_detector("") == "[REDACTED_TRUFFLEHOG]"


class TestFindingsEngineEntryPoints:
    """scan_session_for_trufflehog_findings emits RawFinding rows whose
    offsets point at each occurrence of the raw secret in every text
    field. Only the subprocess shim is mocked; the field walk and
    offset computation run for real against a realistic session dict.
    """

    @staticmethod
    def _fake_matches(monkeypatch, raws):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog, "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "detector": detector, "status": "verified"}
                for raw, detector in raws
            ],
        )

    def test_scan_text_for_raw_matches_keeps_unverified_generic_azure(self, monkeypatch):
        """The redaction-engine path preserves unverified generic Azure hits so
        it stays consistent with the fail-closed gate."""
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        rows = [
            {"DetectorName": "Azure", "Verified": False, "Raw": "127.0.0.1:5432"},
            {"DetectorName": "Azure", "Verified": True, "Raw": "real-azure-secret-0001"},
            {"DetectorName": "Slack", "Verified": False, "Raw": "xoxb-abc-def"},
        ]
        stdout = ("\n".join(json.dumps(r) for r in rows) + "\n").encode()
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: subprocess.CompletedProcess(a, 183, stdout=stdout, stderr=b""),
        )
        matches = trufflehog._scan_text_for_raw_matches("payload")
        pairs = {(m["detector"], m["status"]) for m in matches}
        assert ("Azure", "unverified") in pairs
        assert ("Azure", "verified") in pairs
        assert ("Slack", "unverified") in pairs

    def test_serialize_session_does_not_duplicate_widened_text(self):
        """Widened leaves appear once (via the JSON dump), not twice — the
        relocation walk must not bloat the scan payload toward the size cap."""
        raw = "WIDENEDUNIQUEMARKERXYZ"
        session = {"messages": [{"content": "c", "extra": {"k": raw}, "tool_uses": []}]}
        payload = trufflehog._serialize_session_for_scan(session)
        assert payload.count(raw) == 1

    def test_findings_emitted_for_widened_message_fields(self, monkeypatch):
        """A secret living only in the widened message model (author /
        invocations / snippets / extra) must surface as a reviewable
        RawFinding, not just get redacted silently at apply time."""
        raw_secret = "synthetic_widened_secret_abcdef"
        self._fake_matches(monkeypatch, [(raw_secret, "Stripe")])
        session = {
            "messages": [
                {
                    "content": "legacy text",
                    "author": raw_secret,
                    "invocations": [{"result": f"out {raw_secret}"}],
                    "extra": {"raw": {"deep": raw_secret}},
                    "tool_uses": [],
                },
            ],
        }
        out = trufflehog.scan_session_for_trufflehog_findings(session)
        fields = {f.field for f in out}
        assert "author" in fields
        assert any(f.field.startswith("invocations[0]") for f in out)
        assert any(f.field.startswith("extra") for f in out)
        assert all(f.entity_text == raw_secret for f in out)

    def test_findings_emitted_per_occurrence(self, monkeypatch):
        from clawjournal.findings import RawFinding

        raw_secret = "synthetic_stripe_verysecretabcdef"
        self._fake_matches(monkeypatch, [(raw_secret, "Stripe")])
        session = {
            "project": f"prefix {raw_secret} suffix",
            "messages": [
                {"content": f"first occurrence: {raw_secret}", "tool_uses": []},
                {"content": f"second here {raw_secret}, and again {raw_secret}", "tool_uses": []},
            ],
        }
        out = trufflehog.scan_session_for_trufflehog_findings(session)
        assert all(isinstance(f, RawFinding) for f in out)
        assert len(out) == 4  # project + msg0 + 2x msg1
        engines = {f.engine for f in out}
        assert engines == {"trufflehog"}
        detectors = {f.entity_type for f in out}
        assert detectors == {"Stripe"}
        for f in out:
            assert f.entity_text == raw_secret
            assert f.length == len(raw_secret)
            assert f.confidence == 1.0

    def test_missing_binary_returns_no_findings(self, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: False)
        session = {"messages": [{"content": "sk-anything-looks-secret-like"}]}
        assert trufflehog.scan_session_for_trufflehog_findings(session) == []


class TestApplyTruffleHogPass:
    """Legacy-path apply: replace raw strings in-place, emit log
    entries that match the existing ``{type, confidence,
    original_length, field, message_index?}`` shape."""

    def test_replaces_in_all_locations_and_logs_per_occurrence(self, monkeypatch):
        raw = "xoxb-0123456789-ABCDEFGHIJKL"
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog, "_scan_text_for_raw_matches",
            lambda text: [{"raw": raw, "detector": "Slack", "status": "verified"}],
        )
        session = {
            "project": f"p {raw} q",
            "messages": [
                {"content": f"hi {raw}", "tool_uses": [{"input": {"path": f"/x/{raw}/y"}}]},
            ],
        }
        total, log = trufflehog.apply_trufflehog_pass(session)
        assert total == 3
        assert session["project"] == "p [REDACTED_SLACK] q"
        assert session["messages"][0]["content"] == "hi [REDACTED_SLACK]"
        assert session["messages"][0]["tool_uses"][0]["input"]["path"] == "/x/[REDACTED_SLACK]/y"
        types = {e["type"] for e in log}
        assert types == {"trufflehog_slack"}
        assert len(log) == 3
        assert all(e["original_length"] == len(raw) for e in log)
        assert all("confidence" in e for e in log)


class TestApplyPathIntegration:
    """End-to-end through ``apply_findings_to_blob``: TruffleHog's
    subprocess must run exactly once regardless of ``max_passes``, and
    ``decisions`` must be honored (ignored hashes stay in the blob)."""

    @staticmethod
    def _seed(conn, sess_id, content):
        from clawjournal.findings import reset_salt_cache
        from clawjournal.workbench.index import upsert_sessions

        reset_salt_cache()
        sess = {
            "session_id": sess_id,
            "display_title": "t",
            "project": "p",
            "source": "claude",
            "start_time": "2026-04-20T00:00:00",
            "end_time": "2026-04-20T00:10:00",
            "messages": [{
                "role": "user", "content": content,
                "thinking": "", "tool_uses": [],
            }],
            "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                      "input_tokens": 1, "output_tokens": 0},
        }
        upsert_sessions(conn, [sess])
        conn.commit()

    @staticmethod
    def _patch_trufflehog_once(monkeypatch, raw, detector="Stripe"):
        calls = {"n": 0}

        def fake_scan(text):
            calls["n"] += 1
            return [{"raw": raw, "detector": detector, "status": "verified"}]

        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", fake_scan)
        return calls

    def test_subprocess_runs_once_not_per_pass(self, tmp_path, monkeypatch):
        import sqlite3
        from clawjournal.findings import hash_entity, reset_salt_cache
        from clawjournal.redaction.secrets import apply_findings_to_blob
        from clawjournal.workbench.index import INDEX_DB, open_index  # noqa: F401

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
        reset_salt_cache()

        conn = open_index()
        try:
            raw = "synthetic_stripe_abcdefg1234567890abcdef"
            self._seed(conn, "sess-one", content=f"payload {raw}")
            calls = self._patch_trufflehog_once(monkeypatch, raw)

            # apply_findings_to_blob is the DB-backed redaction entry.
            # With max_passes=3, a naive implementation would shell out
            # three times; the fix caches the map once outside the loop.
            blob = dict(conn.execute(
                "SELECT * FROM sessions WHERE session_id = ?", ("sess-one",),
            ).fetchone())
            # Construct a blob shape the apply path walks.
            blob = {
                "session_id": "sess-one",
                "display_title": "t", "project": "p", "git_branch": "",
                "messages": [{"content": f"payload {raw}", "thinking": "", "tool_uses": []}],
            }
            redacted, n = apply_findings_to_blob(blob, conn, "sess-one", max_passes=3)
            assert n >= 1
            assert raw not in redacted["messages"][0]["content"]
            assert "[REDACTED_STRIPE]" in redacted["messages"][0]["content"]
            assert calls["n"] == 1, (
                f"TruffleHog subprocess ran {calls['n']} times; "
                f"should be exactly 1 regardless of max_passes"
            )
        finally:
            conn.close()

    def test_json_serialization_context_matches_are_redacted(self, tmp_path, monkeypatch):
        from clawjournal.findings import reset_salt_cache
        from clawjournal.redaction.secrets import apply_findings_to_blob
        from clawjournal.workbench.index import open_index

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
        reset_salt_cache()

        raw = "407e111122223333444455556666c7fa"

        def fake_scan(text):
            if '"serverId":' in text and raw in text:
                return [{"raw": raw, "detector": "NpmToken", "status": "unverified"}]
            return []

        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(trufflehog, "_scan_text_for_raw_matches", fake_scan)

        conn = open_index()
        try:
            blob = {
                "session_id": "sess-json-context",
                "display_title": "t",
                "project": "p",
                "git_branch": "",
                "messages": [{
                    "content": "tool server started",
                    "thinking": "",
                    "tool_uses": [{"input": {"serverId": raw}}],
                }],
            }
            redacted, n = apply_findings_to_blob(blob, conn, "sess-json-context")
            assert n == 1
            assert (
                redacted["messages"][0]["tool_uses"][0]["input"]["serverId"]
                == "[REDACTED_NPMTOKEN]"
            )
        finally:
            conn.close()

    def test_ignored_hash_is_not_redacted(self, tmp_path, monkeypatch):
        import sqlite3
        from clawjournal.findings import hash_entity, reset_salt_cache
        from clawjournal.redaction.secrets import apply_findings_to_blob
        from clawjournal.workbench.index import open_index

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
        reset_salt_cache()

        conn = open_index()
        try:
            raw = "xoxb-should-be-left-alone-XYZ12345"
            self._seed(conn, "sess-ign", content=f"keep this {raw}")
            self._patch_trufflehog_once(monkeypatch, raw, detector="Slack")

            # Seed an 'ignored' decision for this raw's hash.
            entity_hash = hash_entity(raw)
            conn.execute(
                "INSERT INTO findings "
                "(finding_id, session_id, engine, rule, entity_type, entity_hash, "
                " entity_length, field, message_index, tool_field, offset, length, "
                " confidence, status, decided_by, decision_source_id, decided_at, "
                " decision_reason, revision, created_at) "
                "VALUES ('fid', 'sess-ign', 'trufflehog', 'Slack', 'Slack', ?, ?, "
                "        'content', 0, NULL, 0, ?, 1.0, 'ignored', 'user', NULL, "
                "        '2026-04-20T00:00:00', NULL, 'v1:test', '2026-04-20T00:00:00')",
                (entity_hash, len(raw), len(raw)),
            )
            conn.commit()

            blob = {
                "session_id": "sess-ign",
                "display_title": "t", "project": "p", "git_branch": "",
                "messages": [{"content": f"keep this {raw}", "thinking": "", "tool_uses": []}],
            }
            redacted, _ = apply_findings_to_blob(blob, conn, "sess-ign")
            # Ignored decisions must not be redacted by any engine.
            assert raw in redacted["messages"][0]["content"]
            assert "[REDACTED_SLACK]" not in redacted["messages"][0]["content"]
        finally:
            conn.close()


class TestApplyLoopExit:
    """Loop in apply_findings_to_blob now relies on the
    ``pass_count == 0 and pass_num > 0`` guard to exit when
    trufflehog_map is non-empty. Guard has to actually fire for any
    max_passes >= 2 or we'd spin."""

    def test_exits_within_two_passes_when_trufflehog_map_is_sticky(self, tmp_path, monkeypatch):
        from clawjournal.findings import reset_salt_cache
        from clawjournal.redaction.secrets import apply_findings_to_blob
        from clawjournal.workbench.index import open_index, upsert_sessions

        monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
        monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
        monkeypatch.setattr("clawjournal.workbench.index.CONFIG_DIR", tmp_path)
        reset_salt_cache()

        conn = open_index()
        try:
            raw = "synthetic_stripe_exit_guard_probe_0123456789"
            upsert_sessions(conn, [{
                "session_id": "sess-exit",
                "display_title": "t", "project": "p", "source": "claude",
                "start_time": "2026-04-20T00:00:00", "end_time": "2026-04-20T00:10:00",
                "messages": [{"role": "user", "content": f"hi {raw}", "thinking": "", "tool_uses": []}],
                "stats": {"user_messages": 1, "assistant_messages": 0, "tool_uses": 0,
                          "input_tokens": 1, "output_tokens": 0},
            }])
            conn.commit()

            monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
            monkeypatch.setattr(trufflehog, "is_available", lambda: True)
            monkeypatch.setattr(
                trufflehog, "_scan_text_for_raw_matches",
                lambda text: [{"raw": raw, "detector": "Stripe", "status": "verified"}],
            )

            blob = {
                "session_id": "sess-exit",
                "display_title": "t", "project": "p", "git_branch": "",
                "messages": [{"content": f"hi {raw}", "thinking": "", "tool_uses": []}],
            }
            # max_passes=5: no infinite loop; should finish quickly.
            redacted, n = apply_findings_to_blob(blob, conn, "sess-exit", max_passes=5)
            assert n >= 1
            assert raw not in redacted["messages"][0]["content"]
        finally:
            conn.close()


class TestDeterministicPlaceholder:
    """When two detectors flag the same raw, the chosen placeholder
    must be stable across runs. Sort key is ``(detector, raw)``."""

    def test_multi_detector_same_raw_stable_order(self, monkeypatch):
        raw = "stable_key_xyz_0123456789abcdef"
        # Return detectors in reverse alphabetical order — if the code
        # trusted input order the placeholder would come from "Zeta".
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog, "_scan_text_for_raw_matches",
            lambda text: [
                {"raw": raw, "detector": "Zeta", "status": "unverified"},
                {"raw": raw, "detector": "Alpha", "status": "verified"},
            ],
        )
        session = {"messages": [{"content": f"x {raw} y", "tool_uses": []}]}
        total, log = trufflehog.apply_trufflehog_pass(session)
        assert total == 1
        assert session["messages"][0]["content"] == "x [REDACTED_ALPHA] y"
        assert all(e["type"] == "trufflehog_alpha" for e in log)


class TestFieldNameConsistency:
    """The scan-time engine (scan_session_for_trufflehog_findings) and
    the legacy apply pass (apply_trufflehog_pass) must use the same
    field convention — ``tool_uses[<idx>].<branch>`` — so downstream
    consumers (derive_preview, redaction_log correlation) can line
    entries up. Reviewer flagged a prior `tool_<branch>` shortcut."""

    def test_apply_pass_uses_bracketed_tool_field_names(self, monkeypatch):
        raw = "ghp_abcdefghijklmnop12345678901234"
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(
            trufflehog, "_scan_text_for_raw_matches",
            lambda text: [{"raw": raw, "detector": "GitHub", "status": "verified"}],
        )
        session = {
            "messages": [{
                "content": "",
                "tool_uses": [
                    {"input": {"cmd": f"echo {raw}"}, "output": f"got {raw}"},
                ],
            }],
        }
        _, log = trufflehog.apply_trufflehog_pass(session)
        fields = {entry.get("field") for entry in log}
        # Both string-valued tool output and nested dict-valued tool
        # input should use the bracketed form matching
        # findings._resolve_field_text's regex.
        assert any(f and f.startswith("tool_uses[0].output") for f in fields), fields
        assert any(f and f.startswith("tool_uses[0].input") for f in fields), fields


class TestSubprocessHardening:
    """Locks the security-critical invocation details: env scrub,
    stdin streaming, oversized-payload short-circuit."""

    def test_scrubbed_env_excludes_secrets(self, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-mustnotleak")
        monkeypatch.setenv("OPENAI_API_KEY", "sk-mustnotleak")
        monkeypatch.setenv("PATH", "/usr/bin:/bin")
        monkeypatch.setenv("HOME", "/home/u")
        env = trufflehog._scrubbed_subprocess_env()
        assert "ANTHROPIC_API_KEY" not in env
        assert "OPENAI_API_KEY" not in env
        assert env.get("PATH") == "/usr/bin:/bin"
        assert env.get("HOME") == "/home/u"

    def test_payload_streams_via_stdin_not_tempfile(self, tmp_path, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        monkeypatch.setattr(trufflehog, "resolve_binary", lambda: "trufflehog")

        captured: dict = {}

        def fake_run(args, **kwargs):
            captured["args"] = args
            captured["input"] = kwargs.get("input")
            captured["env"] = kwargs.get("env")
            return subprocess.CompletedProcess(args, 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        trufflehog._scan_text_for_raw_matches("hello world payload")
        assert captured["args"][0] == "trufflehog"
        # stdin mode, not filesystem — no on-disk payload.
        assert "stdin" in captured["args"]
        assert "filesystem" not in captured["args"]
        assert captured["input"] == b"hello world payload"
        # Env must be the scrubbed one, not None (None → inherit parent).
        assert isinstance(captured["env"], dict)

    def test_oversized_payload_returns_empty(self, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        calls = {"n": 0}

        def fake_run(*a, **k):
            calls["n"] += 1
            return subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Just over the 200 MB cap.
        huge = "x" * (trufflehog._MAX_SCAN_BYTES + 1)
        assert trufflehog._scan_text_for_raw_matches(huge) == []
        # Short-circuit must fire before any subprocess is spawned.
        assert calls["n"] == 0

    def test_timeout_returns_empty(self, monkeypatch):
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)

        def fake_run(args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args, timeout=30)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert trufflehog._scan_text_for_raw_matches("some text with a token") == []


class TestEngineFingerprint:
    """engine_fingerprint feeds into findings_revision so cached
    scans invalidate when the user installs or upgrades trufflehog."""

    def test_missing_binary_reports_missing(self, monkeypatch):
        trufflehog.reset_version_cache()
        monkeypatch.setattr(trufflehog, "_binary_signature", lambda: ("missing",))
        assert trufflehog.engine_fingerprint() == "missing"

    def test_version_captured_when_available(self, monkeypatch):
        trufflehog.reset_version_cache()
        monkeypatch.setattr(trufflehog, "_binary_signature", lambda: ("/fake/trufflehog", 123, 456))

        def fake_run(args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="trufflehog 3.94.3\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        fp = trufflehog.engine_fingerprint()
        assert "3.94.3" in fp

    def test_memoized_until_binary_changes(self, monkeypatch):
        trufflehog.reset_version_cache()
        sig = ["/fake/trufflehog", 100, 456]
        monkeypatch.setattr(trufflehog, "_binary_signature", lambda: tuple(sig))
        calls = {"n": 0}

        def fake_run(args, **kwargs):
            calls["n"] += 1
            return subprocess.CompletedProcess(args, 0, stdout=f"trufflehog 3.{sig[1]}.0\n", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        trufflehog.engine_fingerprint()
        trufflehog.engine_fingerprint()
        trufflehog.engine_fingerprint()
        assert calls["n"] == 1
        # A binary upgrade (mtime change) must invalidate the cache so
        # a long-running daemon notices without a restart.
        sig[1] = 200
        trufflehog.engine_fingerprint()
        assert calls["n"] == 2

    def test_invalidation_through_real_os_stat_seam(self, tmp_path, monkeypatch):
        """Cache invalidation must work through the REAL `os.stat` path,
        not just a stubbed `_binary_signature`. This is the seam the
        daemon relies on to notice an in-place `trufflehog install` —
        when a managed binary is overwritten, its size/mtime change and
        the cached fingerprint must be recomputed.
        """
        trufflehog.reset_version_cache()
        config_dir = tmp_path / ".clawjournal"
        monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
        managed = trufflehog.managed_binary_path()
        managed.parent.mkdir(parents=True)
        managed.write_bytes(b"v1")
        managed.chmod(0o755)
        # No PATH fallback so the managed copy is the resolved binary.
        monkeypatch.setattr(
            "clawjournal.redaction.trufflehog.shutil.which", lambda name: None
        )

        version = {"v": "3.90.0"}

        def fake_run(args, **kwargs):
            # Real argv resolves to the managed path — proves we went
            # through resolve_binary()/os.stat, not a stub.
            assert args[0] == str(managed)
            return subprocess.CompletedProcess(
                args, 0, stdout=f"trufflehog {version['v']}\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)

        assert trufflehog.engine_fingerprint() == "trufflehog 3.90.0"

        # Simulate an in-place upgrade: new bytes (new size), and bump the
        # mtime explicitly so the change is observable even on filesystems
        # with coarse mtime granularity.
        version["v"] = "3.95.5"
        managed.write_bytes(b"v2-which-is-longer")
        st = managed.stat()
        os.utime(managed, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000_000))

        assert trufflehog.engine_fingerprint() == "trufflehog 3.95.5"


class TestFormatBlockMessage:
    def test_missing_binary_includes_install_hint(self):
        report = trufflehog.TruffleHogReport(
            scanned_path="/x", scanned_sha256="sha256:0", binary_missing=True,
        )
        msg = trufflehog.format_block_message(report)
        assert "brew install trufflehog" in msg
        assert "CLAWJOURNAL_SKIP_TRUFFLEHOG" in msg

    def test_findings_include_masked_examples(self):
        report = trufflehog.TruffleHogReport(
            scanned_path="/x",
            scanned_sha256="sha256:0",
            findings=[
                trufflehog.TruffleHogFinding(
                    detector="GitHub", status="verified", line=5,
                    masked="ghp_a***4567", raw_sha256="sha256:x",
                )
            ],
            verified=1,
        )
        msg = trufflehog.format_block_message(report)
        assert "verified=1" in msg
        assert "ghp_a***4567" in msg

    def test_scan_error_mentions_blocked_status(self):
        report = trufflehog.TruffleHogReport(
            scanned_path="/x",
            scanned_sha256="sha256:0",
            scan_error="unexpected exit status 2",
        )
        msg = trufflehog.format_block_message(report)
        assert "Share blocked" in msg
        assert "unexpected exit status 2" in msg
