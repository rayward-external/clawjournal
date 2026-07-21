"""Table-driven tests for the share gate's tier policy."""

import pytest

from clawjournal.redaction import scan_policy
from clawjournal.redaction.scan_policy import GateReport, PolicyFinding, classify


class TestClassify:
    @pytest.mark.parametrize(
        ("engine", "rule", "status", "entropy", "decision", "expected_tier", "expected_reason"),
        [
            # 1. Verified live credential blocks…
            ("trufflehog", "GitHub", "verified", 4.8, None, "block", "trufflehog_verified"),
            # …unless ignored/allowlisted, which still needs a human.
            ("trufflehog", "GitHub", "verified", 4.8, "ignored", "review", "allowlisted_but_verified"),
            # 2. Ignored/allowlisted values warn (the old gate re-found
            # and blocked them).
            ("betterleaks", "slack-bot-token", "none", 4.8, "ignored", "warn", "decision_ignored"),
            # Accepted/open decisions don't downgrade anything.
            ("betterleaks", "slack-bot-token", "none", 4.8, "accepted", "redact", "default_redact"),
            ("betterleaks", "slack-bot-token", "none", 4.8, "open", "redact", "default_redact"),
            # 3. Unmistakable structure needs review — even ignored
            # doesn't apply here because review_rule ranks below the
            # ignored check (ignored private key warns).
            ("betterleaks", "private-key", "none", 3.9, None, "review", "review_rule"),
            ("betterleaks", "pkcs12-file", "none", 3.9, None, "review", "review_rule"),
            ("betterleaks", "private-key", "none", 3.9, "ignored", "warn", "decision_ignored"),
            # 4. Soft rules and low entropy warn.
            ("betterleaks", "generic-api-key", "none", 4.8, None, "warn", "soft_rule"),
            ("betterleaks", "npm-token", "none", 1.5, None, "warn", "low_entropy"),
            # 5. Default: recognizable unverified token → redact.
            ("betterleaks", "npm-token", "none", 4.9, None, "redact", "default_redact"),
            ("betterleaks", "npm-token", "none", None, None, "redact", "default_redact"),
        ],
    )
    def test_tier_table(
        self, engine, rule, status, entropy, decision, expected_tier, expected_reason
    ):
        tier, reason = classify(
            engine=engine,
            rule=rule,
            status=status,
            entropy=entropy,
            decision_status=decision,
        )
        assert (tier, reason) == (expected_tier, expected_reason)

    def test_verified_precedence_beats_every_other_signal(self):
        # A verified credential that is also a soft rule with low
        # entropy still blocks.
        tier, _ = classify(
            engine="trufflehog",
            rule="generic-api-key",
            status="verified",
            entropy=0.5,
            decision_status=None,
        )
        assert tier == "block"


def _finding(tier, *, line=1, reason="r", engine="betterleaks", rule="npm-token"):
    return PolicyFinding(
        engine=engine,
        rule=rule,
        status="none",
        line=line,
        masked="abc***xyz",
        raw_sha256="sha256:x",
        entropy=4.5,
        tier=tier,
        tier_reason=reason,
    )


class TestGateReport:
    def _report(self, **kwargs):
        defaults = {"scanned_path": "sessions.jsonl", "scanned_sha256": "sha256:0"}
        defaults.update(kwargs)
        return GateReport(**defaults)

    def test_clean_report_is_not_blocking(self):
        report = self._report()
        assert report.blocking is False
        assert report.block_reason is None

    def test_warn_only_report_ships(self):
        report = self._report(
            findings=[_finding("warn")], tier_counts={"warn": 1}
        )
        assert report.blocking is False

    @pytest.mark.parametrize("tier", ["block", "review"])
    def test_blocking_tiers_block(self, tier):
        report = self._report(findings=[_finding(tier)])
        assert report.blocking is True
        assert report.block_reason == "secret-scan-findings"

    def test_failure_modes_fail_closed(self):
        assert self._report(binary_missing=True).blocking is True
        assert self._report(binary_missing=True).block_reason == "scanner-not-installed"
        assert self._report(scan_error="boom").blocking is True
        assert self._report(scan_error="boom").block_reason == "scanner-error"
        assert self._report(converged=False).blocking is True

    def test_bypass_is_never_blocking_but_recorded(self):
        report = self._report(bypassed=True, findings=[_finding("block")])
        assert report.blocking is False
        assert report.block_reason is None
        assert report.summary()["bypassed"] is True

    def test_summary_orders_blocking_findings_first(self):
        report = self._report(
            findings=[
                _finding("warn", line=1),
                _finding("review", line=9),
                _finding("block", line=5),
            ],
        )
        tiers = [e["tier"] for e in report.summary()["examples"]]
        assert tiers == ["block", "review", "warn"]

    def test_summary_and_report_carry_no_raw(self, tmp_path):
        finding = _finding("block")
        finding.raw = "the-actual-secret-value"
        report = self._report(findings=[finding])
        import json

        assert "the-actual-secret-value" not in json.dumps(report.summary())
        out = tmp_path / "secret-scan.json"
        scan_policy.write_report(out, report)
        assert "the-actual-secret-value" not in out.read_text()
        assert "sessions.jsonl" in out.read_text()  # basename only


class TestFormatBlockMessage:
    def test_findings_message_names_tiers_and_examples(self):
        report = GateReport(
            scanned_path="s.jsonl",
            scanned_sha256="sha256:0",
            findings=[_finding("block", line=3)],
            tier_counts={"block": 1, "warn": 2},
            gate_redactions=4,
        )
        message = scan_policy.format_block_message(report)
        assert "1 blocking finding(s)" in message
        assert "block=1" in message
        assert "auto-redacted=4" in message
        assert "warnings=2" in message
        assert "L3 block betterleaks:npm-token abc***xyz" in message

    def test_missing_binary_message_carries_install_hints(self):
        report = GateReport(
            scanned_path="s.jsonl",
            scanned_sha256="sha256:0",
            binary_missing=True,
            missing_binaries=["betterleaks", "trufflehog"],
        )
        message = scan_policy.format_block_message(report)
        assert "clawjournal betterleaks install" in message
        assert "clawjournal trufflehog install" in message

    def test_non_convergence_is_called_out(self):
        report = GateReport(
            scanned_path="s.jsonl",
            scanned_sha256="sha256:0",
            findings=[_finding("review", reason="non_convergent_escalation")],
            tier_counts={"review": 1},
            converged=False,
        )
        assert "did not converge" in scan_policy.format_block_message(report)
