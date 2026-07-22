"""Share-gate orchestration: scan, classify, redact-and-rescan, decide.

``run_share_gate`` is the single chokepoint both export paths call on
the merged ``sessions.jsonl`` (post-deterministic-redaction, and again
post-PII-rewrite). The gate:

1. Betterleaks scans (broad detection, local-only) and TruffleHog scans
   verified-only (live-credential check) on the initial merged artifact,
   returning raw matches in *file-level* form (JSONL-escaped bytes as
   they appear on disk).
2. Every finding is tiered by ``scan_policy.classify`` against the
   findings-table decisions of the session its line maps to.
3. Raw matches of redact/review/block findings are replaced with
   placeholders directly in the serialized lines — string replacement
   on the line, then a ``json.loads`` validation, because scanners
   report the escaped on-disk form, not the decoded value. A
   replacement that breaks a line's JSON is rolled back and its
   finding escalated to review.
4. Betterleaks alone rescans until no broad finding remains to redact
   (bounded by ``max_passes``; non-convergence escalates and blocks).
5. If any bytes changed, TruffleHog performs one final verified-only
   scan so a credential revealed by rewriting cannot leave the machine.

Block/review findings accumulate across passes (their raws get
redacted too — a best-effort scrub of the on-disk export dir that also
keeps a later rescan from re-observing them; a span whose replacement
would break its line's JSON is rolled back, so a *blocked* export dir
can still hold that plaintext). Redaction is scoped to each finding's
own line: the same value can be redact-tier in one session and
warn-tier (user-ignored) in another, and a redact decision must not
strip it from the session where the user chose to keep it. The
returned ``GateReport``'s ``blocking`` property is the policy outcome
the callers act on.

All rewriting happens strictly before any manifest write, zip build,
or seal/fingerprint hashing in the callers — bytes are frozen only
after the gate returns.
"""

from __future__ import annotations

import dataclasses
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from ..redaction import betterleaks, scan_policy, trufflehog
from ..redaction.scan_policy import GateReport, PolicyFinding

# Raws shorter than this are never redacted at the gate: single
# characters / tiny fragments would shred unrelated text. Mirrors the
# findings-engine minimum.
_MIN_RAW_LENGTH = 3


def _finding_key(f: PolicyFinding) -> tuple:
    return (f.engine, f.rule, f.line, f.raw_sha256)


def _session_id_for_line(manifest: dict[str, Any], line: int | None) -> str | None:
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not isinstance(line, int):
        return None
    if line < 1 or line > len(sessions):
        return None
    session = sessions[line - 1]
    if not isinstance(session, dict):
        return None
    session_id = session.get("session_id")
    return session_id if isinstance(session_id, str) and session_id else None


class _DecisionCache:
    """Per-session findings-table decisions, loaded once per session.

    Same query ``apply_findings_to_blob`` uses; ``ignored`` covers both
    explicit user decisions and allowlist hits (recorded as ignored at
    write time).
    """

    def __init__(self, conn: sqlite3.Connection, manifest: dict[str, Any]):
        self._conn = conn
        self._manifest = manifest
        self._by_session: dict[str, dict[str, str]] = {}

    def status_for(self, line: int | None, raw: str | None) -> str | None:
        if raw is None:
            return None
        session_id = _session_id_for_line(self._manifest, line)
        if session_id is None:
            return None
        decisions = self._by_session.get(session_id)
        if decisions is None:
            try:
                rows = self._conn.execute(
                    "SELECT entity_hash, status FROM findings WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
                decisions = {row["entity_hash"]: row["status"] for row in rows}
            except sqlite3.Error:
                decisions = {}
            self._by_session[session_id] = decisions
        from ..findings import hash_entity  # noqa: PLC0415 — lazy to avoid cycle

        # The gate hashes the *file-level escaped* raw. Scan-time
        # scanner engines hash the ``json.dumps`` serialized form too
        # (``_serialize_session_for_scan``), so decisions match under
        # the default engine set. A regex-only findings config stores
        # only decoded-form hashes; an escaped-form lookup then misses
        # and the finding falls through to redact/review — fail-safe,
        # never fail-open.
        return decisions.get(hash_entity(raw))


def _policy_findings_for_pass(
    bl_raws: list[dict],
    th_raws: list[dict],
    decision_cache: _DecisionCache,
) -> list[PolicyFinding]:
    """Classify one pass's raw matches into ``PolicyFinding``s."""
    from ..redaction.secrets import _shannon_entropy  # noqa: PLC0415 — lazy

    findings: list[PolicyFinding] = []
    seen: set[tuple] = set()

    for match in bl_raws:
        raw = match["raw"]
        entropy = match.get("entropy")
        if entropy is None:
            entropy = _shannon_entropy(raw)
        tier, reason = scan_policy.classify(
            engine=betterleaks.BETTERLEAKS_ENGINE_ID,
            rule=match["rule_id"],
            status="none",
            entropy=entropy,
            decision_status=decision_cache.status_for(match.get("line"), raw),
        )
        finding = PolicyFinding(
            engine=betterleaks.BETTERLEAKS_ENGINE_ID,
            rule=match["rule_id"],
            status="none",
            line=match.get("line"),
            masked=trufflehog.mask_secret(raw),
            raw_sha256=_raw_sha(raw),
            entropy=entropy,
            tier=tier,
            tier_reason=reason,
            raw=raw,
        )
        if _finding_key(finding) not in seen:
            seen.add(_finding_key(finding))
            findings.append(finding)

    for match in th_raws:
        raw = match["raw"]
        entropy = _shannon_entropy(raw)
        tier, reason = scan_policy.classify(
            engine=trufflehog.TRUFFLEHOG_ENGINE_ID,
            rule=match["detector"],
            status=match["status"],
            entropy=entropy,
            decision_status=decision_cache.status_for(match.get("line"), raw),
        )
        finding = PolicyFinding(
            engine=trufflehog.TRUFFLEHOG_ENGINE_ID,
            rule=match["detector"],
            status=match["status"],
            line=match.get("line"),
            masked=trufflehog.mask_secret(raw),
            raw_sha256=_raw_sha(raw),
            entropy=entropy,
            tier=tier,
            tier_reason=reason,
            raw=raw,
        )
        if _finding_key(finding) not in seen:
            seen.add(_finding_key(finding))
            findings.append(finding)

    return findings


def _raw_sha(raw: str) -> str:
    import hashlib  # noqa: PLC0415

    return f"sha256:{hashlib.sha256(raw.encode()).hexdigest()}"


def _rewrite_line(
    line: str,
    replacements: list[tuple[str, str]],
) -> tuple[str, dict[str, int], set[str]]:
    """Replace raws in one serialized JSONL line, keeping it valid JSON.

    Returns ``(new_line, counts_by_raw, failed_raws)``. Replacement is
    string-level (scanners report file-level escaped bytes); validity
    is enforced by parsing the result. If the combined rewrite breaks
    the line's JSON — e.g. a regex match that spans a structural
    boundary — the raws are retried one at a time so a single bad span
    can't veto the good ones; the offenders are reported back for
    escalation instead of being force-applied.
    """
    applicable = [(raw, ph) for raw, ph in replacements if raw in line]
    if not applicable:
        return line, {}, set()

    def _apply(base: str, pairs: list[tuple[str, str]]) -> tuple[str, dict[str, int]]:
        out = base
        counts: dict[str, int] = {}
        for raw, placeholder in pairs:
            n = out.count(raw)
            if n:
                out = out.replace(raw, placeholder)
                counts[raw] = n
        return out, counts

    candidate, counts = _apply(line, applicable)
    if _is_valid_json_line(candidate):
        return candidate, counts, set()

    # Retry raw-by-raw, longest first (the caller pre-sorts), keeping
    # only replacements that preserve JSON validity.
    failed: set[str] = set()
    out = line
    counts = {}
    for raw, placeholder in applicable:
        n = out.count(raw)
        if not n:
            continue
        attempt = out.replace(raw, placeholder)
        if _is_valid_json_line(attempt):
            out = attempt
            counts[raw] = n
        else:
            failed.add(raw)
    return out, counts, failed


def _is_valid_json_line(line: str) -> bool:
    try:
        json.loads(line)
    except (json.JSONDecodeError, ValueError):
        return False
    return True


def _rewrite_sessions_file(
    sessions_file: Path,
    to_redact: list[PolicyFinding],
) -> tuple[int, dict[int, int], set[tuple[int, str]]]:
    """Replace the given findings' raws in ``sessions_file``, scoped to
    each finding's own line. Returns ``(total_replacements,
    replacements_by_line, failed_pairs)`` where ``failed_pairs`` holds
    ``(line, raw)`` for spans rolled back to keep a line's JSON valid.
    Atomic: written to a sibling tmp file and ``os.replace``d in.

    Scoping matters: the same raw can be redact-tier in one session and
    warn-tier (user-ignored) in another, and per-session decisions must
    not bleed across lines. Occurrences on other lines arrive as their
    own findings (scanners report every match), and the rescan loop
    catches anything the current pass's map missed. A finding without
    line metadata errs toward redaction on every line.
    """
    by_line_map: dict[int, dict[str, str]] = {}
    global_map: dict[str, str] = {}
    # Sort (rule, raw) ascending first so the placeholder choice for a
    # raw flagged by two rules is deterministic across runs.
    for f in sorted(to_redact, key=lambda f: (f.rule, f.raw or "")):
        if f.raw is None or len(f.raw) < _MIN_RAW_LENGTH:
            continue
        if f.engine == betterleaks.BETTERLEAKS_ENGINE_ID:
            placeholder = betterleaks.placeholder_for_rule(f.rule)
        else:
            placeholder = trufflehog.placeholder_for_detector(f.rule)
        if isinstance(f.line, int):
            by_line_map.setdefault(f.line, {}).setdefault(f.raw, placeholder)
        else:
            global_map.setdefault(f.raw, placeholder)

    if not by_line_map and not global_map:
        return 0, {}, set()

    total = 0
    by_line: dict[int, int] = {}
    failed_pairs: set[tuple[int, str]] = set()
    tmp_file = sessions_file.with_suffix(sessions_file.suffix + ".gate-tmp")
    try:
        with open(sessions_file, encoding="utf-8") as src, \
                open(tmp_file, "w", encoding="utf-8") as dst:
            for line_no, line in enumerate(src, start=1):
                merged = dict(global_map)
                merged.update(by_line_map.get(line_no, {}))
                stripped = line.rstrip("\n")
                if merged:
                    # Longest raw first so overlaps replace cleanly.
                    replacements = sorted(
                        merged.items(), key=lambda kv: -len(kv[0])
                    )
                    new_line, counts, failed = _rewrite_line(
                        stripped, replacements
                    )
                else:
                    new_line, counts, failed = stripped, {}, set()
                for raw in failed:
                    failed_pairs.add((line_no, raw))
                if counts:
                    line_total = sum(counts.values())
                    total += line_total
                    by_line[line_no] = by_line.get(line_no, 0) + line_total
                dst.write(new_line + "\n")
        os.replace(tmp_file, sessions_file)
    except BaseException:
        tmp_file.unlink(missing_ok=True)
        raise
    return total, by_line, failed_pairs


def _rawless_backstop_findings(bl_report=None, th_report=None) -> list[PolicyFinding]:
    """Sticky findings for scanner hits that returned no usable raw.

    ``scan_file_with_raws`` only surfaces raw matches with a non-empty
    secret value, but both parsers still count the finding in their
    report (``raw_sha256`` stays ``None``). Without a span there is
    nothing to redact and no way to match a findings-table decision,
    so fail closed: a verified credential blocks, everything else
    requires review. Dropping these silently would let a verified live
    credential ship whenever TruffleHog omits ``Raw``.
    """
    out: list[PolicyFinding] = []
    if th_report is not None:
        for f in th_report.findings:
            if f.raw_sha256 is None:
                out.append(
                    PolicyFinding(
                        engine=trufflehog.TRUFFLEHOG_ENGINE_ID,
                        rule=f.detector,
                        status=f.status,
                        line=f.line,
                        masked=f.masked,
                        raw_sha256=None,
                        entropy=None,
                        tier="block" if f.status == "verified" else "review",
                        tier_reason="finding_without_raw",
                        raw=None,
                    )
                )
    if bl_report is not None:
        for f in bl_report.findings:
            if f.raw_sha256 is None:
                out.append(
                    PolicyFinding(
                        engine=betterleaks.BETTERLEAKS_ENGINE_ID,
                        rule=f.rule_id,
                        status="none",
                        line=f.line,
                        masked=f.masked,
                        raw_sha256=None,
                        entropy=f.entropy,
                        tier="review",
                        tier_reason="finding_without_raw",
                        raw=None,
                    )
                )
    return out


def _accumulate_policy_findings(
    findings: list[PolicyFinding],
    sticky: dict[tuple, PolicyFinding],
    warns: dict[tuple, PolicyFinding],
) -> None:
    for finding in findings:
        if finding.tier in ("block", "review"):
            sticky.setdefault(_finding_key(finding), finding)
        elif finding.tier == "warn":
            warns.setdefault(_finding_key(finding), finding)


def _rewrite_policy_findings(
    sessions_file: Path,
    findings: list[PolicyFinding],
    *,
    sticky: dict[tuple, PolicyFinding],
    redacted_keys: set[tuple],
) -> tuple[list[PolicyFinding], int, dict[int, int]]:
    redactable = [
        finding
        for finding in findings
        if finding.tier in ("redact", "review", "block")
        and finding.raw is not None
        and len(finding.raw) >= _MIN_RAW_LENGTH
    ]
    if not redactable:
        return [], 0, {}

    replaced, by_line, failed_pairs = _rewrite_sessions_file(
        sessions_file, redactable
    )
    failed_raws = {raw for _line, raw in failed_pairs}
    for finding in redactable:
        failed_here = (
            (finding.line, finding.raw) in failed_pairs
            or (finding.line is None and finding.raw in failed_raws)
        )
        if failed_here and finding.tier not in ("block", "review"):
            escalated = dataclasses.replace(
                finding, tier="review", tier_reason="unredactable_span"
            )
            sticky.setdefault(_finding_key(escalated), escalated)
        elif finding.tier == "redact" and not failed_here:
            redacted_keys.add(_finding_key(finding))
    return redactable, replaced, by_line


def run_share_gate(
    sessions_file: Path,
    manifest: dict[str, Any],
    *,
    conn: sqlite3.Connection,
    max_passes: int = 3,
) -> GateReport:
    """Run the tiered secret-scan gate over ``sessions_file``.

    Never raises on scanner trouble — failures surface on the report
    (``binary_missing`` / ``scan_error``) so callers fail closed.
    """
    if max_passes < 1:
        raise ValueError("max_passes must be at least 1")

    bl_bypassed = betterleaks.is_bypassed()
    th_bypassed = trufflehog.is_bypassed()
    if bl_bypassed or th_bypassed:
        # A partially bypassed gate is not a full scan; treat any bypass
        # as a whole-gate bypass so the manifest records it and the
        # upload path can refuse to ship it.
        return GateReport(
            scanned_path=str(sessions_file),
            scanned_sha256=_hash_or_empty(sessions_file),
            bypassed=True,
            engine=_combined_fingerprint(),
        )

    missing = [
        name
        for name, module in (("betterleaks", betterleaks), ("trufflehog", trufflehog))
        if not module.is_available()
    ]
    if missing:
        return GateReport(
            scanned_path=str(sessions_file),
            scanned_sha256=_hash_or_empty(sessions_file),
            binary_missing=True,
            missing_binaries=missing,
            engine=_combined_fingerprint(),
        )

    decision_cache = _DecisionCache(conn, manifest)
    sticky: dict[tuple, PolicyFinding] = {}   # block/review, accumulated
    warns: dict[tuple, PolicyFinding] = {}
    redacted_keys: set[tuple] = set()         # distinct redact-tier findings
    total_redactions = 0
    redactions_by_line: dict[int, int] = {}
    converged = False
    passes = 0
    bl_report = None
    th_report = None
    last_redactable: list[PolicyFinding] = []

    while passes < max_passes:
        passes += 1
        bl_report, bl_raws = betterleaks.scan_file_with_raws(sessions_file)
        th_raws = []
        current_th_report = None
        if passes == 1:
            current_th_report, th_raws = trufflehog.scan_file_with_raws(
                sessions_file, results="verified"
            )
            th_report = current_th_report
        error = (
            bl_report.scan_error
            or (current_th_report.scan_error if current_th_report else None)
            or ("betterleaks binary disappeared mid-gate" if bl_report.binary_missing else None)
            or (
                "trufflehog binary disappeared mid-gate"
                if current_th_report and current_th_report.binary_missing
                else None
            )
        )
        if error:
            return _finalize_report(
                GateReport(
                    scanned_path=str(sessions_file),
                    scanned_sha256=_hash_or_empty(sessions_file),
                    scan_error=error,
                    rescan_passes=passes,
                ),
                bl_report,
                th_report,
            )

        findings = _policy_findings_for_pass(bl_raws, th_raws, decision_cache)
        _accumulate_policy_findings(findings, sticky, warns)
        for f in _rawless_backstop_findings(bl_report, current_th_report):
            sticky.setdefault(_finding_key(f), f)

        # Redact-tier raws are replaced so the share can proceed;
        # block/review-tier raws are replaced too, a best-effort scrub
        # of the on-disk debug artifact (blocking is decided by
        # `sticky`, not by what remains in the file — an unredactable
        # span stays in the blocked export dir). Warn-tier values
        # are deliberately left alone — an ignored/allowlisted value is
        # the user's standing decision to keep it.
        redactable, replaced, by_line = _rewrite_policy_findings(
            sessions_file,
            findings,
            sticky=sticky,
            redacted_keys=redacted_keys,
        )
        last_redactable = [f for f in redactable if f.tier == "redact"]
        if not redactable:
            converged = True
            break
        total_redactions += replaced
        for line_no, count in by_line.items():
            redactions_by_line[line_no] = redactions_by_line.get(line_no, 0) + count

    if not converged:
        # Pass budget exhausted with redactable findings still turning
        # up — escalate the stragglers so a human decides; never ship a
        # file the scanners still flag.
        for f in last_redactable:
            escalated = dataclasses.replace(
                f, tier="review", tier_reason="non_convergent_escalation"
            )
            sticky.setdefault(_finding_key(escalated), escalated)
            redacted_keys.discard(_finding_key(f))

    # Rewriting can reveal a credential hidden behind an earlier matched span.
    # Verify the final bytes once, rather than repeating a network-backed
    # TruffleHog pass during every Betterleaks convergence iteration.
    if total_redactions:
        th_report, final_th_raws = trufflehog.scan_file_with_raws(
            sessions_file, results="verified"
        )
        error = (
            th_report.scan_error
            or (
                "trufflehog binary disappeared mid-gate"
                if th_report.binary_missing
                else None
            )
        )
        if error:
            return _finalize_report(
                GateReport(
                    scanned_path=str(sessions_file),
                    scanned_sha256=_hash_or_empty(sessions_file),
                    scan_error=error,
                    rescan_passes=passes,
                ),
                bl_report,
                th_report,
            )

        final_findings = _policy_findings_for_pass(
            [], final_th_raws, decision_cache
        )
        _accumulate_policy_findings(final_findings, sticky, warns)
        for finding in _rawless_backstop_findings(th_report=th_report):
            sticky.setdefault(_finding_key(finding), finding)
        _final_redactable, replaced, by_line = _rewrite_policy_findings(
            sessions_file,
            final_findings,
            sticky=sticky,
            redacted_keys=redacted_keys,
        )
        total_redactions += replaced
        for line_no, count in by_line.items():
            redactions_by_line[line_no] = (
                redactions_by_line.get(line_no, 0) + count
            )

    all_findings = sorted(
        list(sticky.values()) + list(warns.values()),
        key=lambda f: (f.line if f.line is not None else 10**9, f.engine, f.rule),
    )
    tier_counts: dict[str, int] = {}
    for f in all_findings:
        tier_counts[f.tier] = tier_counts.get(f.tier, 0) + 1
    # Redacted findings are transient (their raws are gone from the
    # file), so they are counted rather than listed.
    if redacted_keys:
        tier_counts["redact"] = len(redacted_keys)

    return _finalize_report(
        GateReport(
            scanned_path=str(sessions_file),
            scanned_sha256=_hash_or_empty(sessions_file),
            findings=all_findings,
            tier_counts=tier_counts,
            gate_redactions=total_redactions,
            redactions_by_line=redactions_by_line,
            rescan_passes=passes,
            converged=converged,
        ),
        bl_report,
        th_report,
    )


def _finalize_report(report: GateReport, bl_report, th_report) -> GateReport:
    """Stamp fingerprints and per-engine summaries onto the gate report."""
    report.engine = _combined_fingerprint()
    if bl_report is not None:
        bl_report.engine = betterleaks.engine_fingerprint()
        report.betterleaks_report = bl_report
        report.engines["betterleaks"] = bl_report.summary()
    if th_report is not None:
        th_report.engine = trufflehog.engine_fingerprint()
        report.trufflehog_report = th_report
        report.engines["trufflehog"] = th_report.summary()
    return report


def _combined_fingerprint() -> str:
    return (
        f"betterleaks {_short_fp(betterleaks.engine_fingerprint())} + "
        f"trufflehog {_short_fp(trufflehog.engine_fingerprint())}"
    )


def _short_fp(fingerprint: str) -> str:
    # engine_fingerprint returns e.g. "betterleaks 1.6.1" / "missing";
    # keep only the version-ish tail for the combined string.
    parts = fingerprint.split()
    return parts[-1] if parts else "unknown"


def _hash_or_empty(path: Path) -> str:
    try:
        return trufflehog._sha256_file(path)
    except OSError:
        return ""


def build_blocked_sessions(
    manifest: dict[str, Any],
    findings: list[PolicyFinding],
) -> list[dict[str, Any]]:
    """Map blocking-tier findings back to exported sessions by line.

    ``sessions.jsonl`` is one line per exported session. If every
    finding maps, the UI/runner can offer per-trace recovery (remove
    and retry; route to pending_review). If any finding cannot be
    mapped, return ``[]`` so callers keep the existing hard block.
    Entries keep the legacy ``detector`` key alongside ``engine`` /
    ``rule`` / ``tier``.
    """
    if not findings:
        return []
    sessions = manifest.get("sessions")
    if not isinstance(sessions, list) or not sessions:
        return []

    blocked_by_id: dict[str, dict[str, Any]] = {}
    for finding in findings:
        session_id = _session_id_for_line(manifest, finding.line)
        if session_id is None:
            return []
        session = sessions[finding.line - 1]  # type: ignore[operator] — line validated above
        entry = blocked_by_id.setdefault(
            session_id,
            {
                "session_id": session_id,
                "project": session.get("project"),
                "source": session.get("source"),
                "model": session.get("model"),
                "line": finding.line,
                "findings": [],
            },
        )
        entry["findings"].append({
            "line": finding.line,
            "detector": finding.rule,
            "status": finding.status,
            "masked": finding.masked,
            "engine": finding.engine,
            "rule": finding.rule,
            "tier": finding.tier,
        })

    return sorted(blocked_by_id.values(), key=lambda item: item["line"])
