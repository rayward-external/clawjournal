"""Terminal adapter for the reusable automatic-upload service."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Any


def add_auto_upload_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "auto-upload", help="Manage authorized automatic weekly sharing"
    )
    commands = parser.add_subparsers(dest="auto_upload_command", required=True)

    enable = commands.add_parser("enable", help="Review terms and enable automatic sharing")
    enable.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    enable.add_argument("--accept-authorization-version", default=None)
    enable.add_argument("--accept-retention-version", default=None)
    enable.add_argument("--json", action="store_true")

    status = commands.add_parser("status", help="Show automatic-sharing status")
    status.add_argument("--json", action="store_true")

    preview = commands.add_parser("preview", help="Preview the current candidate report")
    preview.add_argument("--refresh", action="store_true")
    preview.add_argument("--json", action="store_true")

    run = commands.add_parser("run", help="Run one extra capped cycle now")
    run.add_argument("--json", action="store_true")

    for name, help_text in (
        ("pause", "Pause automatic sharing while keeping hooks installed"),
        ("resume", "Resume after rechecking the accepted local profile"),
        ("disable", "Turn off automatic sharing and revoke upload authority"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("--json", action="store_true")
    return parser


def _print_human(result: dict[str, Any]) -> None:
    if result.get("ok") is False:
        print(f"Automatic upload: {result.get('code', 'error')}", file=sys.stderr)
        if result.get("message"):
            print(str(result["message"]), file=sys.stderr)
        return
    if "mode" in result:
        overlay = f", {result['overlay']}" if result.get("overlay") else ""
        print(f"Automatic upload: {result['mode']} / {result.get('health', 'ready')}{overlay}")
        scope = result.get("scope") or {}
        if scope.get("sources"):
            print(f"Sources: {', '.join(scope['sources'])}")
        if scope.get("projects"):
            print(f"Projects: {', '.join(scope['projects'])}")
        if result.get("next_due_at"):
            print(f"Next due: {result['next_due_at']} (on the next supported agent session)")
        if result.get("next_retry_at"):
            print(f"Next retry: {result['next_retry_at']}")
        eligibility = result.get("eligibility") or {}
        print(
            "Eligible: "
            f"{eligibility.get('eligible_count', 0)} "
            f"(next cycle selects {eligibility.get('selected_count', 0)})"
        )
        return
    if "selected" in result:
        print(
            f"Eligible: {result.get('eligible_count', 0)}; "
            f"selected: {result.get('selected_count', 0)}; "
            f"deferred by cap: {result.get('deferred_by_cap', 0)}"
        )
        for reason, count in sorted((result.get("exclusion_counts") or {}).items()):
            if count:
                print(f"  {reason}: {count}")
        return
    print(f"Automatic upload: {result.get('code', 'complete')}")
    if result.get("count") is not None:
        print(f"Trace count: {result['count']}")
    if result.get("receipt_reference"):
        print(f"Receipt: {result['receipt_reference']}")


def _emit(result: dict[str, Any], *, output_json: bool) -> None:
    if output_json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_human(result)


def _interactive_accept(args, challenge: dict[str, Any]) -> tuple[str, str] | None:
    if not sys.stdin.isatty():
        return None
    authorization = challenge["authorization"]
    retention = challenge["retention"]
    scope = challenge["scope"]
    ai = challenge["ai"]
    print("\nRecurring scope authorization\n")
    print(str(authorization["text"]))
    print("\nRetention\n")
    print(str(retention["text"]))
    print(f"\nSources: {', '.join(scope['sources'])}")
    print(f"Projects: {', '.join(scope['projects'])}")
    print(f"Cycle cap: {challenge['cap']}; cadence: {challenge['cadence_days']} days")
    if challenge.get("destination_origin"):
        print(f"Destination: {challenge['destination_origin']}")
    print(
        "AI-PII: "
        + (f"enabled via {ai.get('backend')}" if ai.get("enabled") else "disabled")
    )
    entered_auth = input(
        f"Type authorization version {authorization['version']} to accept: "
    ).strip()
    if entered_auth != authorization["version"]:
        return None
    entered_retention = input(
        f"Type retention version {retention['version']} to accept: "
    ).strip()
    if entered_retention != retention["version"]:
        return None
    return entered_auth, entered_retention


def _fresh_email_verification() -> bool:
    if not sys.stdin.isatty():
        return False
    from .workbench.daemon import (
        confirm_pending_email_verification,
        request_email_verification,
    )

    email = input("Academic email for fresh enrollment verification: ").strip()
    if not email:
        return False
    request_email_verification(email)
    code = getpass.getpass("Verification code: ").strip()
    if not code:
        return False
    confirm_pending_email_verification(code)
    return True


def run(args) -> None:
    from . import auto_upload

    command = args.auto_upload_command
    output_json = bool(getattr(args, "json", False))
    if command == "status":
        result = auto_upload.status()
    elif command == "preview":
        result = auto_upload.preview(refresh=bool(args.refresh))
    elif command == "run":
        result = auto_upload.run_cycle(force=True)
    elif command == "pause":
        result = auto_upload.pause()
    elif command == "resume":
        result = auto_upload.resume()
    elif command == "disable":
        result = auto_upload.disable()
    elif command == "enable":
        auth_version = args.accept_authorization_version
        retention_version = args.accept_retention_version
        result = auto_upload.enable(
            agent=args.agent,
            accepted_authorization_version=auth_version,
            accepted_retention_version=retention_version,
        )
        if result.get("code") == "authorization_required" and not output_json:
            accepted = _interactive_accept(args, result)
            if accepted is None:
                result = {
                    "ok": False,
                    "code": "authorization_not_accepted",
                    "message": "Exact recurring authorization versions were not accepted.",
                }
            else:
                auth_version, retention_version = accepted
                result = auto_upload.enable(
                    agent=args.agent,
                    accepted_authorization_version=auth_version,
                    accepted_retention_version=retention_version,
                )
        if result.get("code") == "email_verification_required" and not output_json:
            if _fresh_email_verification():
                # Re-fetching also catches terms changed while the code was in flight.
                result = auto_upload.enable(
                    agent=args.agent,
                    accepted_authorization_version=auth_version,
                    accepted_retention_version=retention_version,
                )
            else:
                result = {
                    "ok": False,
                    "code": "email_verification_required",
                    "message": "Fresh email verification is required to enroll.",
                }
    else:  # pragma: no cover - argparse enforces the command set
        raise ValueError(f"unsupported automatic-upload command: {command}")
    _emit(result, output_json=output_json)
    if result.get("ok") is False:
        raise SystemExit(1)
