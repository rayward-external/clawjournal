"""Shared share-flow service used by the CLI (and available to the daemon).

This module is the single sanctioned home for the share-flow logic that the
terminal UI and the web/daemon both need: the redaction classifiers/labels
(ported once from the web's `li`/`gi`/`hi`), a single redaction-record builder
used for BOTH the review preview and packaging coverage, hosted-destination
capability resolution, and thin wrappers over the packaging/submit/zip helpers.

The terminal layer (``share_cli``) depends only on this module — it does not
reach into daemon-private helpers or re-implement the classifiers. The daemon
can adopt these same helpers over time.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .workbench.index import (
    apply_share_redactions,
    create_share,
    get_share,
    release_gate_blockers,
)

# Wrapped daemon helpers — imported here so callers depend on share_flow, not on
# daemon-private names directly.
from .workbench.daemon import (
    _build_share_zip as _daemon_build_zip,
    _fetch_hosted_share_capabilities,
    _prepare_share_export_for_upload,
    confirm_email_verification as _confirm_email_verification,
    fetch_hosted_consent as _fetch_hosted_consent,
    hosted_upload_status as _hosted_upload_status,
    request_email_verification as _request_email_verification,
    submit_share_to_hosted,
)

# ---- redaction classifiers / labels (single Python source; mirrors web) -----

BUCKET_KEYS = ("tokens", "emails", "paths", "timestamps", "urls", "other")

# bucket -> human label used in the per-trace breakdown ("Redacting your traces").
CATEGORY_LABELS = {
    "tokens": "Secrets & credentials",
    "emails": "Email addresses",
    "paths": "File paths & usernames",
    "urls": "URLs",
    "timestamps": "Timestamps coarsened",
    "other": "Other",
}


def bucket_for(type_str: str) -> str:
    """Classify a redaction_log entry type into a display bucket (web `li`)."""
    t = (type_str or "").lower()
    if "email" in t:
        return "emails"
    if "url" in t:
        return "urls"
    if "path" in t or "username" in t or "home" in t:
        return "paths"
    if "time" in t or "date" in t:
        return "timestamps"
    if t.startswith("trufflehog") or any(k in t for k in ("token", "key", "secret", "jwt", "cred", "auth")):
        return "tokens"
    return "other"


def redaction_buckets(log: list[dict]) -> tuple[dict[str, int], int]:
    """Aggregate a redaction_log into the 6 buckets + the TruffleHog hit count."""
    buckets = {k: 0 for k in BUCKET_KEYS}
    th_hits = 0
    for e in log or []:
        etype = e.get("type", "")
        buckets[bucket_for(etype)] += 1
        if str(etype).lower().startswith("trufflehog"):
            th_hits += 1
    return buckets, th_hits


def _plural(n: int) -> str:
    return "" if n == 1 else "s"


def category_breakdown(rec: dict) -> list[str]:
    """Per-category redaction breakdown (web `gi`), TruffleHog subset called out."""
    b = rec["buckets"]
    th = rec.get("th_hits", 0)
    out: list[str] = []
    if b["tokens"]:
        label = CATEGORY_LABELS["tokens"]
        if th:
            label += f" (incl. {th} via TruffleHog)"
        out.append(f"{label}: {b['tokens']}")
    for key in ("emails", "paths", "urls", "timestamps", "other"):
        if b[key]:
            out.append(f"{CATEGORY_LABELS[key]}: {b[key]}")
    counts: dict[str, int] = {}
    for f in rec.get("ai_findings") or []:
        name = (f.get("entity_type") or "").replace("_", " ").strip() or "pii"
        counts[name] = counts.get(name, 0) + 1
    for name, c in sorted(counts.items(), key=lambda kv: -kv[1]):
        out.append(f"AI-flagged {name}: {c}")
    return out


def trace_status(rec: dict) -> str:
    """'clear' or 'review' (web `hi`). Anything not fully AI-reviewed, or with a
    low-confidence AI finding, needs human review."""
    cov = rec.get("ai_coverage", "disabled")
    if cov in ("rules_only", "disabled"):
        return "review"
    if any((f.get("confidence", 0) or 0) < 0.85 for f in (rec.get("ai_findings") or [])):
        return "review"
    return "clear"


# ---- the one redaction-record builder (preview == packaging contract) -------

def build_redaction_record(conn, session_detail: dict, settings: dict,
                           ai_pii: bool, *, backend: str = "auto") -> dict:
    """Build the redaction record for one trace — the SAME computation used for
    the review preview. Returns:
        {redacted, count, buckets, th_hits, ai_findings, ai_coverage, status}
    ai_coverage is one of: 'disabled' (AI off), 'full' (AI ran), 'rules_only'
    (AI requested but unavailable/failed). Callers must keep packaging coverage
    consistent with what was previewed (see effective_ai_pii)."""
    red, count, log = apply_share_redactions(
        conn, session_detail,
        custom_strings=settings["custom_strings"],
        user_allowlist=settings["allowlist_entries"],
        extra_usernames=settings["extra_usernames"],
        blocked_domains=settings["blocked_domains"],
    )
    buckets, th_hits = redaction_buckets(log)

    ai_findings: list[dict] = []
    ai_coverage = "disabled"
    if ai_pii:
        ai_coverage = "rules_only"
        try:
            from .redaction.pii import review_session_pii_with_agent, apply_findings_to_session
            findings = review_session_pii_with_agent(red, ignore_errors=False, backend=backend)
            ai_coverage = "full"
            if findings:
                red, ai_count = apply_findings_to_session(red, findings)
                count += ai_count
                ai_findings = findings
        except Exception:  # noqa: BLE001
            ai_coverage = "rules_only"

    rec = {"redacted": red, "count": count, "buckets": buckets, "th_hits": th_hits,
           "ai_findings": ai_findings, "ai_coverage": ai_coverage}
    rec["status"] = trace_status(rec)
    return rec


def effective_ai_pii(records: list[dict], requested: bool) -> tuple[bool, bool]:
    """Decide the packaging AI-PII flag so that what shipped == what was shown.

    Returns (package_ai_pii, uniform):
      - requested False           -> (False, True)
      - all records 'full'        -> (True, True)
      - any record 'rules_only'   -> (False, False)  # AI unavailable somewhere;
        degrade everywhere to rules-only so the bundle matches a rules-only view.
    The 'uniform' flag is False when AI was requested but could not be applied
    everywhere, so the caller can warn / offer a retry before continuing.
    """
    if not requested:
        return False, True
    coverages = [r.get("ai_coverage") for r in records]
    if coverages and all(c == "full" for c in coverages):
        return True, True
    return False, False


# ---- hosted destination (capabilities + graceful fallback) ------------------

def hosted_destination() -> dict[str, Any]:
    """Resolve what the hosted destination supports right now, so the CLI can
    fall back to download-only instead of stranding users on a hosted path.
    Returns {reachable, submissions_open, can_submit, can_download, message,
    support_contact, maximum_bundle_size}."""
    info: dict[str, Any] = {
        "reachable": False, "submissions_open": False, "can_submit": False,
        "can_download": True, "message": "", "support_contact": None,
        "maximum_bundle_size": None,
    }
    try:
        caps = _fetch_hosted_share_capabilities()
    except Exception as exc:  # noqa: BLE001
        info["message"] = f"Hosted submission service unreachable ({exc}); download-only."
        return info
    info["reachable"] = True
    info["support_contact"] = caps.get("contact_email") or caps.get("support_contact")
    info["maximum_bundle_size"] = caps.get("maximum_bundle_size")
    if caps.get("submissions_open") is False:
        info["message"] = "Hosted submissions are currently closed; download-only."
        return info
    info["submissions_open"] = True
    info["can_submit"] = True
    return info


# ---- packaging / submit / zip (thin wrappers) -------------------------------

def gate_blockers(conn, session_ids: list[str]) -> list[dict]:
    return release_gate_blockers(conn, session_ids)


def package(conn, session_ids: list[str], settings: dict, *, ai_pii: bool,
            note: str | None = None) -> dict:
    """Create a share row and seal the bundle. Returns:
        {ok, share_id, export_dir, manifest, blocked_sessions, error}
    blocked_sessions is the list of sessions TruffleHog/PII blocked (for recovery).
    """
    share_id = create_share(conn, session_ids, note=note)
    share = get_share(conn, share_id)
    if share is None:
        return {"ok": False, "error": "Share row could not be loaded after creation."}
    export_dir, manifest, error = _prepare_share_export_for_upload(
        conn, share_id, share, settings, reuse_finalized=True, ai_pii_review_enabled=ai_pii,
    )
    if error:
        return {"ok": False, "share_id": share_id, "error": error.get("error", "Packaging failed."),
                "blocked_sessions": error.get("blocked_sessions") or []}
    if export_dir is None:
        return {"ok": False, "share_id": share_id, "error": "Packaging failed: no bundle produced."}
    if manifest.get("blocked"):
        return {"ok": False, "share_id": share_id,
                "error": manifest.get("block_message") or manifest.get("block_reason")
                or "Bundle marked blocked.",
                "blocked_sessions": manifest.get("blocked_sessions") or []}
    return {"ok": True, "share_id": share_id, "export_dir": export_dir, "manifest": manifest,
            "blocked_sessions": []}


def verify_coverage(manifest: dict, package_ai: bool) -> tuple[bool, str]:
    """CLI-side guard (no daemon change): the sealed artifact's AI coverage must
    match the preview decision, so we never ship something LESS redacted than
    what the user reviewed. The seal pass uses ignore_llm_errors=True and can
    silently fall back to rules-only; this catches that divergence.

    Returns (ok, message). ok=True means the sealed bundle is consistent."""
    if not package_ai:
        return True, ""  # rules-only preview -> rules-only seal is consistent
    summary = (manifest or {}).get("redaction_summary") or {}
    pr = summary.get("pii_review") or {}
    if not pr.get("ai_enabled"):
        return False, "preview was AI-reviewed but the sealed bundle ran rules-only"
    rules_only = (pr.get("coverage") or {}).get("rules_only", 0)
    if rules_only:
        return False, (f"preview was AI-reviewed but AI review failed for {rules_only} "
                       f"trace(s) during sealing (those shipped rules-only)")
    return True, ""


def build_zip(export_dir: Path) -> bytes:
    return _daemon_build_zip(export_dir)


def submit(conn, share_id: str, *, accept_terms: bool, ownership_certification: bool,
           consent_version: str, retention_policy_version: str, settings: dict,
           ai_pii: bool) -> dict:
    return submit_share_to_hosted(
        conn, share_id,
        accept_terms=accept_terms, ownership_certification=ownership_certification,
        consent_version=consent_version, retention_policy_version=retention_policy_version,
        settings=settings, ai_pii_review_enabled=ai_pii,
    )


# Thin re-exports so the CLI never imports daemon-private names.
def upload_status() -> dict:
    return _hosted_upload_status()


def consent() -> dict:
    return _fetch_hosted_consent()


def request_email_verification(email: str) -> dict:
    return _request_email_verification(email)


def confirm_email_verification(email: str, code: str) -> dict:
    return _confirm_email_verification(email, code)
