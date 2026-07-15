"""CLI boundary for recurring hosted sharing."""

from __future__ import annotations

import json

from . import auto_upload
from .auto_upload_scheduler import install as install_scheduler
from .auto_upload_scheduler import remove as remove_scheduler
from .workbench.index import open_index


def _print(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False, default=str))


def run_auto_upload(args) -> None:
    action = args.auto_upload_action
    conn = open_index()
    try:
        if action == "enable":
            if not args.accept_terms or not args.certify_ownership:
                raise ValueError(
                    "Enabling automatic sharing requires --accept-terms and "
                    "--certify-ownership."
                )
            terms = auto_upload.recurring_terms()
            consent_version = args.consent_version or terms.get("consent_version")
            retention_version = (
                args.retention_policy_version
                or terms.get("retention_policy_version")
            )
            enrollment = auto_upload.enable_enrollment(
                conn,
                consent_version=str(consent_version or ""),
                retention_policy_version=str(retention_version or ""),
                cadence_days=args.cadence_days,
            )
            scheduler = install_scheduler()
            _print({"ok": True, "enrollment": enrollment, "scheduler": scheduler})
            return
        if action == "status":
            _print(auto_upload.enrollment_status(conn))
            return
        if action == "preview":
            enrollment = auto_upload.get_enrollment(conn)
            if enrollment is None:
                raise ValueError("Automatic sharing is not enrolled.")
            if not args.no_scan:
                errors = auto_upload.strict_refresh(enrollment)
                if errors:
                    _print({"ok": False, "status": "retrying", "scan_errors": errors})
                    raise SystemExit(1)
            report = auto_upload.candidate_report(conn, enrollment)
            sessions = report["sessions"]
            _print({
                "ok": True,
                "count": len(sessions),
                "deferred_count": report["deferred_count"],
                "limit": report["limit"],
                "order": report["order"],
                "eligibility": report["eligibility"],
                "sessions": [
                    {
                        "session_id": item["session_id"],
                        "source": item.get("source"),
                        "project": item.get("project"),
                        "title": item.get("display_title"),
                        "revision": item.get("revision_hash"),
                        "updated_since_last_share": item.get("updated_since_last_share"),
                    }
                    for item in sessions
                ],
            })
            return
        if action == "run":
            result = auto_upload.run_once(
                force=not args.scheduled,
                scan=not args.no_scan,
            )
            if not args.scheduled or not result.get("ok"):
                _print(result)
            if not result.get("ok") and result.get("status") not in ("paused", "off"):
                raise SystemExit(1)
            return
        if action == "pause":
            _print({"ok": True, "enrollment": auto_upload.set_enrollment_state(conn, "paused")})
            return
        if action == "resume":
            enrollment = auto_upload.set_enrollment_state(conn, "enabled")
            scheduler = install_scheduler()
            _print({"ok": True, "enrollment": enrollment, "scheduler": scheduler})
            return
        if action == "disable":
            enrollment = auto_upload.set_enrollment_state(conn, "off")
            scheduler = remove_scheduler()
            _print({"ok": True, "enrollment": enrollment, "scheduler": scheduler})
            return
        if action == "hook":
            from .auto_upload_scheduler import spawn_if_due
            spawn_if_due(args.client)
            return
    except ValueError as exc:
        _print({"ok": False, "error": str(exc)})
        raise SystemExit(2) from exc
    finally:
        conn.close()
