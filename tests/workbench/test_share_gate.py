"""Unit tests for the share gate's redact-and-rescan mechanics.

End-to-end tier behavior through ``export_share_to_disk`` lives in
``test_share_gates.py::TestSecretScanGate``; this file exercises the
line-rewrite machinery directly — escaped raws, JSON-validity
protection, atomicity — and the gate's bypass/miss short-circuits.
"""

import json

from clawjournal.redaction.scan_policy import PolicyFinding
from clawjournal.workbench import share_gate
from clawjournal.workbench.share_gate import (
    _rewrite_line,
    _rewrite_sessions_file,
    build_blocked_sessions,
    run_share_gate,
)


def _finding(raw, *, rule="slack-bot-token", tier="redact", line=1, engine="betterleaks"):
    return PolicyFinding(
        engine=engine,
        rule=rule,
        status="none",
        line=line,
        masked="***",
        raw_sha256="sha256:x",
        entropy=4.5,
        tier=tier,
        tier_reason="default_redact",
        raw=raw,
    )


class TestRewriteLine:
    def test_replaces_escaped_form_and_keeps_json_valid(self):
        # Scanners report the on-disk escaped form: a private key inside
        # a JSON string arrives with literal backslash-n sequences. The
        # string-level replacement must operate on exactly that form.
        escaped_key = "-----BEGIN RSA PRIVATE KEY-----\\nABC\\n-----END RSA PRIVATE KEY-----"
        line = json.dumps({"content": "key: " + "-----BEGIN RSA PRIVATE KEY-----\nABC\n-----END RSA PRIVATE KEY-----"})
        assert escaped_key in line  # sanity: escaped form is what's on disk

        new_line, counts, failed = _rewrite_line(
            line, [(escaped_key, "[REDACTED_PRIVATE_KEY]")]
        )
        assert failed == set()
        assert counts == {escaped_key: 1}
        parsed = json.loads(new_line)
        assert parsed["content"] == "key: [REDACTED_PRIVATE_KEY]"

    def test_multiple_occurrences_all_replaced(self):
        line = json.dumps({"a": "tok-SECRET123ABC", "b": "x tok-SECRET123ABC y"})
        new_line, counts, failed = _rewrite_line(
            line, [("tok-SECRET123ABC", "[REDACTED_TOK]")]
        )
        assert counts == {"tok-SECRET123ABC": 2}
        assert "tok-SECRET123ABC" not in new_line
        assert failed == set()

    def test_json_breaking_span_is_rolled_back_and_reported(self):
        # A regex match spanning a structural boundary (here: closing
        # quote + comma + opening quote) would corrupt the line. The
        # offender is rolled back and reported; an independent good
        # replacement on the same line still lands.
        line = '{"a": "left-part", "b": "tok-GOODSECRET99"}'
        bad_span = 'left-part", "b'
        new_line, counts, failed = _rewrite_line(
            line,
            [(bad_span, "[REDACTED_BAD]"), ("tok-GOODSECRET99", "[REDACTED_GOOD]")],
        )
        assert bad_span in failed
        assert counts == {"tok-GOODSECRET99": 1}
        parsed = json.loads(new_line)
        assert parsed["a"] == "left-part"
        assert parsed["b"] == "[REDACTED_GOOD]"

    def test_untouched_line_passes_through(self):
        line = '{"a": "clean"}'
        new_line, counts, failed = _rewrite_line(line, [("absent", "[X]")])
        assert new_line == line
        assert counts == {} and failed == set()


class TestRewriteSessionsFile:
    def test_rewrites_counts_per_line_and_stays_valid(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        target.write_text(
            json.dumps({"s": 1, "content": "leak tok-AAABBBCCC111"}) + "\n"
            + json.dumps({"s": 2, "content": "clean"}) + "\n"
            + json.dumps({"s": 3, "content": "tok-AAABBBCCC111 tok-AAABBBCCC111"}) + "\n"
        )

        # One finding per flagged line — scanners report every match,
        # and the rewrite is scoped to each finding's own line.
        total, by_line, failed = _rewrite_sessions_file(
            target,
            [_finding("tok-AAABBBCCC111", line=1), _finding("tok-AAABBBCCC111", line=3)],
        )

        assert total == 3
        assert by_line == {1: 1, 3: 2}
        assert failed == set()
        lines = target.read_text().splitlines()
        assert len(lines) == 3
        for line in lines:
            json.loads(line)  # every line still valid JSON
        assert "tok-AAABBBCCC111" not in target.read_text()
        assert "[REDACTED_SLACK_BOT_TOKEN]" in lines[0]

    def test_longest_raw_wins_on_overlap(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        target.write_text(json.dumps({"c": "prefix-tok-LONGSECRET123"}) + "\n")

        total, _by_line, _failed = _rewrite_sessions_file(
            target,
            [
                _finding("tok-LONGSECRET123", rule="short-rule"),
                _finding("prefix-tok-LONGSECRET123", rule="long-rule"),
            ],
        )
        # The longer raw is applied first, consuming the overlap.
        assert total == 1
        assert "[REDACTED_LONG_RULE]" in target.read_text()

    def test_short_raws_are_never_applied(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        target.write_text(json.dumps({"c": "a b c"}) + "\n")
        total, _by_line, _failed = _rewrite_sessions_file(target, [_finding("b")])
        assert total == 0
        assert json.loads(target.read_text())["c"] == "a b c"

    def test_no_tmp_file_left_behind(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        target.write_text(json.dumps({"c": "leak tok-AAABBBCCC111"}) + "\n")
        _rewrite_sessions_file(target, [_finding("tok-AAABBBCCC111")])
        assert [p.name for p in tmp_path.iterdir()] == ["sessions.jsonl"]

    def test_redaction_is_scoped_to_the_findings_line(self, tmp_path):
        # The same value can be redact-tier in one session and
        # warn-tier (user-ignored) in another: a finding on line 2
        # must not strip the value from line 1.
        target = tmp_path / "sessions.jsonl"
        target.write_text(
            json.dumps({"s": "a", "content": "keep tok-SHAREDSECRET99"}) + "\n"
            + json.dumps({"s": "b", "content": "strip tok-SHAREDSECRET99"}) + "\n"
        )

        total, by_line, failed = _rewrite_sessions_file(
            target, [_finding("tok-SHAREDSECRET99", line=2)]
        )

        assert total == 1
        assert by_line == {2: 1}
        assert failed == set()
        lines = target.read_text().splitlines()
        assert "tok-SHAREDSECRET99" in lines[0]  # user's keep-decision honored
        assert "tok-SHAREDSECRET99" not in lines[1]

    def test_lineless_finding_falls_back_to_every_line(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        target.write_text(
            json.dumps({"content": "one tok-AAABBBCCC111"}) + "\n"
            + json.dumps({"content": "two tok-AAABBBCCC111"}) + "\n"
        )
        total, by_line, _failed = _rewrite_sessions_file(
            target, [_finding("tok-AAABBBCCC111", line=None)]
        )
        assert total == 2
        assert by_line == {1: 1, 2: 1}
        assert "tok-AAABBBCCC111" not in target.read_text()

    def test_failed_span_is_reported_with_its_line(self, tmp_path):
        target = tmp_path / "sessions.jsonl"
        # The span crosses a structural boundary on line 2 only.
        target.write_text(
            '{"a": "clean line"}\n'
            '{"a": "left-part", "b": "x"}\n'
        )
        bad_span = 'left-part", "b'
        _total, _by_line, failed = _rewrite_sessions_file(
            target, [_finding(bad_span, line=2)]
        )
        assert failed == {(2, bad_span)}
        # Rolled back: the line is untouched and still valid JSON.
        assert json.loads(target.read_text().splitlines()[1])["a"] == "left-part"


class TestRunShareGateShortCircuits:
    def test_any_bypass_marks_whole_gate_bypassed(self, tmp_path, monkeypatch):
        from clawjournal.redaction import betterleaks, trufflehog

        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")
        # Autouse fixture sets both; unset only one — a half-bypassed
        # gate is still not a full scan.
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        assert trufflehog.is_bypassed() and not betterleaks.is_bypassed()

        report = run_share_gate(target, {"sessions": []}, conn=None)
        assert report.bypassed is True
        assert report.blocking is False

    def test_missing_binaries_fail_closed_with_names(self, tmp_path, monkeypatch):
        from clawjournal.redaction import betterleaks, trufflehog

        target = tmp_path / "sessions.jsonl"
        target.write_text("{}\n")
        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: False)
        monkeypatch.setattr(trufflehog, "is_available", lambda: False)

        report = run_share_gate(target, {"sessions": []}, conn=None)
        assert report.binary_missing is True
        assert report.missing_binaries == ["betterleaks", "trufflehog"]
        assert report.blocking is True


class TestRunShareGateLoop:
    def _arm_real_gate(self, monkeypatch):
        from clawjournal.redaction import betterleaks, trufflehog

        monkeypatch.delenv(betterleaks.SKIP_ENV_VAR, raising=False)
        monkeypatch.delenv(trufflehog.SKIP_ENV_VAR, raising=False)
        monkeypatch.setattr(betterleaks, "is_available", lambda: True)
        monkeypatch.setattr(trufflehog, "is_available", lambda: True)
        return betterleaks, trufflehog

    def test_finding_without_raw_blocks_instead_of_vanishing(
        self, tmp_path, monkeypatch
    ):
        # A verified TruffleHog finding whose Raw is empty/absent never
        # reaches the tiered raw set — it must still block (and a
        # raw-less betterleaks hit must still require review), not
        # silently drop out of the gate.
        betterleaks, trufflehog = self._arm_real_gate(monkeypatch)
        target = tmp_path / "sessions.jsonl"
        target.write_text('{"content": "clean"}\n')

        th_report = trufflehog.TruffleHogReport(
            scanned_path=str(target),
            scanned_sha256="x",
            findings=[
                trufflehog.TruffleHogFinding(
                    detector="GitHub",
                    status="verified",
                    line=1,
                    masked="[REDACTED]",
                    raw_sha256=None,
                )
            ],
            verified=1,
        )
        bl_report = betterleaks.BetterleaksReport(
            scanned_path=str(target),
            scanned_sha256="x",
            findings=[
                betterleaks.BetterleaksFinding(
                    rule_id="pkcs12-file",
                    description="",
                    line=1,
                    masked="[REDACTED]",
                    raw_sha256=None,
                    entropy=None,
                )
            ],
        )
        monkeypatch.setattr(
            trufflehog, "scan_file_with_raws", lambda *a, **kw: (th_report, [])
        )
        monkeypatch.setattr(
            betterleaks, "scan_file_with_raws", lambda *a, **kw: (bl_report, [])
        )

        report = run_share_gate(
            target, {"sessions": [{"session_id": "s-a"}]}, conn=None
        )

        assert report.blocking is True
        assert report.block_reason == "secret-scan-findings"
        assert report.converged is True  # nothing redactable, still blocked
        assert report.tier_counts.get("block") == 1
        assert report.tier_counts.get("review") == 1
        reasons = {f.tier_reason for f in report.findings}
        assert reasons == {"finding_without_raw"}

    def test_redact_tier_does_not_strip_ignored_value_in_other_session(
        self, tmp_path, monkeypatch
    ):
        # The same secret in two sessions: ignored (warn) in session A,
        # untriaged (redact) in session B. Session B's line is redacted,
        # session A's line keeps the value, and the manifest tiers stay
        # consistent with the bytes.
        import sqlite3 as sqlite3_mod

        from clawjournal.findings import hash_entity

        betterleaks, trufflehog = self._arm_real_gate(monkeypatch)
        # Deliberately defanged (letters in the digit segments) so GitHub
        # push protection never mistakes the fixture for a live token.
        raw = "xoxb-FAKE0FAKE0FAKE-FIXTURE0TOKEN-AbCdEfGhIjKlMnOpQrStUvWx"
        target = tmp_path / "sessions.jsonl"
        target.write_text(
            json.dumps({"session": "a", "content": "keep " + raw}) + "\n"
            + json.dumps({"session": "b", "content": "strip " + raw}) + "\n"
        )

        conn = sqlite3_mod.connect(":memory:")
        conn.row_factory = sqlite3_mod.Row
        conn.execute(
            "CREATE TABLE findings (session_id TEXT, entity_hash TEXT, status TEXT)"
        )
        conn.execute(
            "INSERT INTO findings VALUES (?, ?, ?)",
            ("s-a", hash_entity(raw), "ignored"),
        )

        def fake_bl_scan(path, **kwargs):
            raws = [
                {"rule_id": "slack-bot-token", "raw": raw, "line": i, "entropy": 4.4}
                for i, line in enumerate(
                    path.read_text().splitlines(), start=1
                )
                if raw in line
            ]
            report = betterleaks.BetterleaksReport(
                scanned_path=str(path), scanned_sha256="x"
            )
            return report, raws

        monkeypatch.setattr(betterleaks, "scan_file_with_raws", fake_bl_scan)
        monkeypatch.setattr(
            trufflehog,
            "scan_file_with_raws",
            lambda path, **kw: (
                trufflehog.TruffleHogReport(
                    scanned_path=str(path), scanned_sha256="x"
                ),
                [],
            ),
        )

        manifest = {"sessions": [{"session_id": "s-a"}, {"session_id": "s-b"}]}
        report = run_share_gate(target, manifest, conn=conn)

        assert report.blocking is False
        assert report.converged is True
        assert report.tier_counts.get("warn") == 1
        assert report.tier_counts.get("redact") == 1
        assert report.gate_redactions == 1
        lines = target.read_text().splitlines()
        assert raw in lines[0]  # session A: user's keep-decision honored
        assert raw not in lines[1]
        assert "[REDACTED_SLACK_BOT_TOKEN]" in lines[1]


class TestBuildBlockedSessions:
    MANIFEST = {
        "sessions": [
            {"session_id": "s-a", "project": "p", "source": "claude", "model": "m"},
            {"session_id": "s-b", "project": "p", "source": "claude", "model": "m"},
        ]
    }

    def test_maps_lines_and_carries_tier_keys(self):
        blocked = build_blocked_sessions(
            self.MANIFEST,
            [
                _finding("x1", tier="review", line=2),
                _finding("x2", tier="block", line=1, engine="trufflehog", rule="GitHub"),
            ],
        )
        assert [b["session_id"] for b in blocked] == ["s-a", "s-b"]
        first = blocked[0]["findings"][0]
        assert first["tier"] == "block"
        assert first["engine"] == "trufflehog"
        assert first["detector"] == "GitHub"  # legacy key kept for the UI/CLI

    def test_unmappable_line_returns_empty(self):
        assert build_blocked_sessions(
            self.MANIFEST, [_finding("x", line=None)]
        ) == []
        assert build_blocked_sessions(
            self.MANIFEST, [_finding("x", line=99)]
        ) == []

    def test_no_raw_leaks_into_entries(self):
        blocked = build_blocked_sessions(
            self.MANIFEST, [_finding("super-secret-raw", tier="block", line=1)]
        )
        assert "super-secret-raw" not in json.dumps(blocked)
