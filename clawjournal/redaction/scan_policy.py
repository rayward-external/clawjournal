"""Tiered policy for the share gate's secret scanners.

The gate runs two engines — Betterleaks (broad, local-only detection)
and TruffleHog verified-only (live-credential check) — and this module
decides, per finding, what happens to the share:

- ``block``   the trace cannot ship (TruffleHog-verified live credential)
- ``review``  the trace needs a human (private-key material, findings
              that survived the redact-and-rescan loop, allowlisted
              values that nonetheless verified live)
- ``redact``  the span is replaced with a placeholder and the share
              proceeds (recognizable-but-unverified tokens — the
              common case that used to reject whole sessions)
- ``warn``    recorded in the manifest, never blocks (soft rules,
              low-entropy hits, values the user ignored/allowlisted)

Scanner failure (missing binary / timeout / bad exit) is not a tier:
the gate report carries ``binary_missing`` / ``scan_error`` and the
``blocking`` property fails closed on either.

No third-party scanner ever decides on its own whether a whole session
survives — that inversion (scanner output feeding a policy we own) is
the point of this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from .betterleaks import BetterleaksReport
    from .trufflehog import TruffleHogReport

Tier = Literal["block", "review", "redact", "warn"]

BLOCKING_TIERS: frozenset[str] = frozenset({"block", "review"})

# Rules whose match is unmistakable credential *structure* — a private
# key blob is dangerous whether or not any provider can verify it, and
# auto-redacting it silently would hide a serious leak from the user.
# Betterleaks rule ids; TruffleHog's PrivateKey detector is listed
# defensively even though the gate runs it verified-only.
REVIEW_RULES: frozenset[str] = frozenset({
    "private-key",
    "pkcs12-file",
    "PrivateKey",
})

# Rules that are keyword/context guesses rather than recognizable token
# shapes. Kept deliberately small — mis-tiering errs toward "redact",
# which is safe; growing this list is a tuning decision.
SOFT_RULES: frozenset[str] = frozenset({
    "generic-api-key",
})

# Below this Shannon entropy a matched "secret" is overwhelmingly a
# demo value, placeholder, or natural-language collision. (Real tokens
# in the vendored fixtures sit near 5.0; the escaped private-key blob
# at 3.86.)
LOW_ENTROPY_THRESHOLD = 3.0


@dataclass
class PolicyFinding:
    engine: str                # "betterleaks" | "trufflehog"
    rule: str                  # betterleaks rule id or TruffleHog detector
    status: str                # trufflehog: verified/unverified/unknown; betterleaks: "none"
    line: int | None           # 1-based sessions.jsonl line == session index
    masked: str
    raw_sha256: str | None
    entropy: float | None
    tier: Tier
    tier_reason: str
    # Transient plaintext for the redact loop only — never serialized;
    # summary()/write_report()/build_blocked_sessions() must not read it.
    raw: str | None = field(default=None, repr=False)

    def public_dict(self) -> dict:
        """Manifest-safe projection (no raw). ``detector`` mirrors
        ``rule`` so pre-tier consumers of blocked-session entries keep
        working."""
        return {
            "engine": self.engine,
            "rule": self.rule,
            "detector": self.rule,
            "status": self.status,
            "line": self.line,
            "masked": self.masked,
            "raw_sha256": self.raw_sha256,
            "entropy": self.entropy,
            "tier": self.tier,
            "tier_reason": self.tier_reason,
        }


def classify(
    *,
    engine: str,
    rule: str,
    status: str,
    entropy: float | None,
    decision_status: str | None,
) -> tuple[Tier, str]:
    """Tier a single finding.

    ``decision_status`` is the findings-table status for the salted
    hash of this value (``ignored`` covers both an explicit user
    decision and an allowlist hit — ``write_findings_to_db`` records
    allowlist matches as ``ignored``/``decided_by=allowlist``).

    Order matters:

    1. A TruffleHog-**verified** live credential blocks — unless the
       user ignored/allowlisted this exact value, which downgrades to
       ``review`` rather than ``warn``: a confirmed-live credential
       never ships silently, but the user's standing decision earns a
       human checkpoint instead of a hard wall.
    2. Ignored/allowlisted values warn. This fixes the long-standing
       trap where allowlisting a value made the redactor skip it and
       the gate then re-found and blocked it.
    3. Unmistakable credential structure (``REVIEW_RULES``) needs a
       human.
    4. Keyword-guess rules and low-entropy matches warn.
    5. Everything else — a recognizable token shape that could not be
       verified — is redacted and the share proceeds.
    """
    verified = engine == "trufflehog" and status == "verified"
    ignored = decision_status == "ignored"

    if verified:
        if ignored:
            return "review", "allowlisted_but_verified"
        return "block", "trufflehog_verified"
    if ignored:
        return "warn", "decision_ignored"
    if rule in REVIEW_RULES:
        return "review", "review_rule"
    if rule in SOFT_RULES:
        return "warn", "soft_rule"
    if entropy is not None and entropy < LOW_ENTROPY_THRESHOLD:
        return "warn", "low_entropy"
    return "redact", "default_redact"


@dataclass
class GateReport:
    """Combined result of one share-gate run (both engines + policy).

    Replaces the single-scanner ``TruffleHogReport`` as the object the
    export chokepoints consult: ``blocking`` here is a *policy*
    outcome, not "any finding exists".
    """

    scanned_path: str
    scanned_sha256: str
    findings: list[PolicyFinding] = field(default_factory=list)
    tier_counts: dict[str, int] = field(default_factory=dict)
    gate_redactions: int = 0
    redactions_by_line: dict[int, int] = field(default_factory=dict)
    rescan_passes: int = 0
    converged: bool = True
    bypassed: bool = False
    binary_missing: bool = False
    missing_binaries: list[str] = field(default_factory=list)
    scan_error: str | None = None
    # Combined engine fingerprint, e.g. "betterleaks 1.6.1 + trufflehog 3.95.5".
    engine: str = ""
    # Per-engine manifest summaries ({"betterleaks": {...}, "trufflehog": {...}}).
    engines: dict[str, dict] = field(default_factory=dict)
    # The final-pass sub-reports, kept for the legacy trufflehog.json /
    # betterleaks report artifacts. Never serialized directly.
    trufflehog_report: "TruffleHogReport | None" = field(default=None, repr=False)
    betterleaks_report: "BetterleaksReport | None" = field(default=None, repr=False)

    @property
    def block_review_findings(self) -> list[PolicyFinding]:
        return [f for f in self.findings if f.tier in BLOCKING_TIERS]

    @property
    def blocking(self) -> bool:
        if self.bypassed:
            return False
        if self.binary_missing:
            return True
        if self.scan_error:
            return True
        if not self.converged:
            return True
        return any(f.tier in BLOCKING_TIERS for f in self.findings)

    @property
    def block_reason(self) -> str | None:
        if self.bypassed:
            return None
        if self.binary_missing:
            return "scanner-not-installed"
        if self.scan_error:
            return "scanner-error"
        if not self.converged or any(f.tier in BLOCKING_TIERS for f in self.findings):
            return "secret-scan-findings"
        return None

    def summary(self) -> dict:
        """Public summary safe for the share manifest — no raw values.

        Blocking-tier findings surface first in ``examples`` so the
        five-example window never hides the reason a share blocked
        behind a page of warnings.
        """
        ordered = sorted(
            self.findings,
            key=lambda f: (
                0 if f.tier == "block" else
                1 if f.tier == "review" else
                2 if f.tier == "redact" else 3,
                f.line if f.line is not None else 10**9,
            ),
        )
        return {
            "findings": len(self.findings),
            "tier_counts": dict(self.tier_counts),
            "gate_redactions": self.gate_redactions,
            "rescan_passes": self.rescan_passes,
            "converged": self.converged,
            "bypassed": self.bypassed,
            "binary_missing": self.binary_missing,
            "missing_binaries": list(self.missing_binaries),
            "scan_error": self.scan_error,
            "engine": self.engine,
            "engines": {name: dict(summary) for name, summary in self.engines.items()},
            "examples": [
                {
                    "engine": f.engine,
                    "rule": f.rule,
                    "status": f.status,
                    "line": f.line,
                    "masked": f.masked,
                    "tier": f.tier,
                    "tier_reason": f.tier_reason,
                }
                for f in ordered[:5]
            ],
        }


def write_report(path: Path, report: GateReport) -> None:
    payload = {
        # Basename only, so tempfile paths don't leak tmpdir structure
        # into the manifest bundle.
        "scanned_path": Path(report.scanned_path).name,
        "scanned_sha256": report.scanned_sha256,
        "bypassed": report.bypassed,
        "binary_missing": report.binary_missing,
        "scan_error": report.scan_error,
        "converged": report.converged,
        "engine": report.engine,
        "findings": [f.public_dict() for f in report.findings],
        "summary": report.summary(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def format_block_message(report: GateReport) -> str:
    if report.bypassed:
        return (
            "Secret scanners were bypassed via CLAWJOURNAL_SKIP_BETTERLEAKS/"
            "CLAWJOURNAL_SKIP_TRUFFLEHOG."
        )
    if report.binary_missing:
        from . import betterleaks, trufflehog  # noqa: PLC0415 — avoid import cycle

        hints = []
        if "betterleaks" in report.missing_binaries:
            hints.append(betterleaks.INSTALL_HINT)
        if "trufflehog" in report.missing_binaries:
            hints.append(trufflehog.INSTALL_HINT)
        return "\n".join(hints) or "A required secret scanner is not installed."
    if report.scan_error:
        return (
            "Secret scan failed before producing a result. "
            f"Share blocked: {report.scan_error}."
        )

    blocking = report.block_review_findings
    counts = report.tier_counts
    examples = ", ".join(
        f"L{f.line if f.line is not None else '?'} {f.tier} {f.engine}:{f.rule} {f.masked}"
        for f in blocking[:5]
    )
    suffix = "" if len(blocking) <= 5 else f" (+{len(blocking) - 5} more)"
    convergence_note = (
        "" if report.converged
        else " Redaction did not converge; remaining findings escalated to review."
    )
    return (
        f"Secret scan blocked the share: {len(blocking)} blocking finding(s) "
        f"(block={counts.get('block', 0)}, review={counts.get('review', 0)}; "
        f"auto-redacted={report.gate_redactions}, warnings={counts.get('warn', 0)})."
        f"{convergence_note} Examples: {examples}{suffix}"
    )
