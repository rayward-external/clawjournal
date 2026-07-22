"""Terminal adapter for the reusable automatic-upload service."""

from __future__ import annotations

import argparse
import getpass
import json
import re
import sys
from typing import Any

# Strip C0/C1 control characters (keep only tab and newline) from any text the
# hosted service controls before printing it, so a malicious/compromised
# endpoint cannot inject ANSI escape sequences that rewrite or hide the
# locally-computed consent summary in the terminal.
_TERMINAL_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")


def _sanitize_terminal(text: Any) -> str:
    return _TERMINAL_CONTROL_CHARS.sub("", str(text))


def _sanitize_terminal_line(text: Any) -> str:
    """Sanitize an untrusted value that must stay on one terminal line."""

    return (
        _sanitize_terminal(text)
        .replace("\t", " ")
        .replace("\n", " ")
        .replace("\u2028", " ")
        .replace("\u2029", " ")
    )


def add_auto_upload_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "auto-upload", help="Manage authorized automatic daily sharing"
    )
    commands = parser.add_subparsers(dest="auto_upload_command", required=True)

    enable = commands.add_parser("enable", help="Review terms and enable automatic sharing")
    enable.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    enable.add_argument("--accept-authorization-version", default=None)
    enable.add_argument("--accept-retention-version", default=None)
    enable.add_argument("--accept-ownership-certification-version", default=None)
    enable.add_argument("--accept-authorization-profile-hash", default=None)
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
        print(
            _sanitize_terminal_line(
                f"Automatic upload: {result.get('code', 'error')}"
            ),
            file=sys.stderr,
        )
        if result.get("message"):
            print(_sanitize_terminal(result["message"]), file=sys.stderr)
        return
    if "mode" in result:
        overlay = f", {result['overlay']}" if result.get("overlay") else ""
        print(_sanitize_terminal_line(
            f"Automatic upload: {result['mode']} / "
            f"{result.get('health', 'ready')}{overlay}"
        ))
        scope = result.get("scope") or {}
        if scope.get("sources"):
            print(_sanitize_terminal_line(f"Sources: {', '.join(scope['sources'])}"))
        if scope.get("projects"):
            print(_sanitize_terminal_line(f"Projects: {', '.join(scope['projects'])}"))
        if result.get("next_due_at"):
            print(_sanitize_terminal_line(
                f"Next due: {result['next_due_at']} "
                "(on the next supported agent session)"
            ))
        if result.get("next_retry_at"):
            print(_sanitize_terminal_line(f"Next retry: {result['next_retry_at']}"))
        stale_hooks = [
            str(row.get("agent"))
            for row in result.get("hooks") or []
            if isinstance(row, dict) and row.get("legacy_hook_installed")
        ]
        if stale_hooks:
            print(_sanitize_terminal_line(
                f"Legacy pre-release hook installed for: {', '.join(stale_hooks)}; "
                "run 'clawjournal auto-upload enable' to migrate it "
                "(or 'disable' to remove it)"
            ))
        eligibility = result.get("eligibility") or {}
        print(_sanitize_terminal_line(
            "Eligible: "
            f"{eligibility.get('eligible_count', 0)} "
            f"(next cycle selects {eligibility.get('selected_count', 0)})"
        ))
        return
    if "selected" in result:
        print(_sanitize_terminal_line(
            f"Eligible: {result.get('eligible_count', 0)}; "
            f"selected: {result.get('selected_count', 0)}; "
            f"deferred by cap: {result.get('deferred_by_cap', 0)}"
        ))
        for reason, count in sorted((result.get("exclusion_counts") or {}).items()):
            if count:
                print(_sanitize_terminal_line(f"  {reason}: {count}"))
        return
    print(_sanitize_terminal_line(
        f"Automatic upload: {result.get('code', 'complete')}"
    ))
    if result.get("count") is not None:
        print(_sanitize_terminal_line(f"Trace count: {result['count']}"))
    if result.get("receipt_reference"):
        print(_sanitize_terminal_line(f"Receipt: {result['receipt_reference']}"))


def _emit(result: dict[str, Any], *, output_json: bool) -> None:
    if output_json:
        print(json.dumps(result, sort_keys=True))
    else:
        _print_human(result)


def _interactive_accept(
    args, challenge: dict[str, Any]
) -> tuple[str, str, str, str] | None:
    if not sys.stdin.isatty():
        return None
    authorization = challenge["authorization"]
    retention = challenge["retention"]
    ownership = challenge["ownership_certification"]
    scope = challenge["scope"]
    ai = challenge["ai"]
    raw_entries = scope.get("entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        return None
    entries: list[tuple[str, str]] = []
    for entry in raw_entries:
        if (
            not isinstance(entry, (list, tuple))
            or len(entry) != 2
            or not all(isinstance(value, str) and value for value in entry)
        ):
            return None
        entries.append((entry[0], entry[1]))
    print("\nRecurring scope authorization\n")
    print(_sanitize_terminal(authorization["text"]))
    print("\nRetention\n")
    print(_sanitize_terminal(retention["text"]))
    print("\nOwnership certification\n")
    print(_sanitize_terminal(ownership["text"]))
    print()
    print(_sanitize_terminal_line(f"Sources: {', '.join(scope['sources'])}"))
    print(_sanitize_terminal_line(f"Projects: {', '.join(scope['projects'])}"))
    print("Exact authorized source/project pairs:")
    for source, project in entries:
        print(_sanitize_terminal_line(f"  {source} -> {project}"))
    cadence_days = challenge["cadence_days"]
    cadence_unit = "day" if cadence_days == 1 else "days"
    print(_sanitize_terminal_line(
        f"Cycle cap: {challenge['cap']}; cadence: {cadence_days} {cadence_unit}"
    ))
    print(_sanitize_terminal_line(
        f"Maximum bundle size: {challenge['maximum_bundle_size']} bytes"
    ))
    if challenge.get("destination_origin"):
        print(_sanitize_terminal_line(
            f"Destination: {challenge['destination_origin']}"
        ))
    print(_sanitize_terminal_line(
        "AI-PII: "
        + (f"enabled via {ai.get('backend')}" if ai.get("enabled") else "disabled")
    ))
    try:
        entered_auth = input(
            _sanitize_terminal_line(
                f"Type authorization version {authorization['version']} to accept: "
            )
        ).strip()
        if entered_auth != authorization["version"]:
            return None
        entered_retention = input(
            _sanitize_terminal_line(
                f"Type retention version {retention['version']} to accept: "
            )
        ).strip()
        if entered_retention != retention["version"]:
            return None
        # The ownership certification is a distinct affirmative act, like the
        # manual share's --certify-ownership: it is typed separately and never
        # bundled into the terms acceptance above.
        entered_ownership = input(
            _sanitize_terminal_line(
                f"Type ownership certification version {ownership['version']} "
                "to certify: "
            )
        ).strip()
    except (EOFError, OSError, KeyboardInterrupt):
        return None
    if entered_ownership != ownership["version"]:
        return None
    profile_hash = challenge.get("authorization_profile_hash")
    if not isinstance(profile_hash, str) or not profile_hash:
        return None
    return entered_auth, entered_retention, entered_ownership, profile_hash


def _fresh_email_verification() -> bool:
    if not sys.stdin.isatty():
        return False
    from .workbench.daemon import (
        confirm_pending_email_verification,
        request_email_verification,
    )

    # request/confirm reach the hosted service and validate input, so a bad
    # email (ValueError), a mistyped code (HostedServiceError, a ValueError
    # subclass), a network failure (URLError, an OSError subclass), or Ctrl-D
    # (EOFError) must surface as a clean message, not a raw traceback — mirror
    # the verify-email command's handling.
    try:
        email = input("Academic email for fresh enrollment verification: ").strip()
        if not email:
            return False
        request_email_verification(email)
        code = getpass.getpass("Verification code: ").strip()
        if not code:
            return False
        confirm_pending_email_verification(code)
    except (OSError, RuntimeError, ValueError, EOFError, KeyboardInterrupt) as exc:
        print(
            f"Verification did not complete: {_sanitize_terminal(exc)}",
            file=sys.stderr,
        )
        return False
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
        ownership_version = args.accept_ownership_certification_version
        profile_hash = args.accept_authorization_profile_hash

        def hosted_notice() -> None:
            if not output_json:
                print(
                    "Checking the hosted service and current terms…",
                    file=sys.stderr,
                )

        scan_started = False

        def scan_progress(source: str, done: int, total: int) -> None:
            # The strict refresh reports sources and counters only, never
            # project names or paths; the values below are locally derived,
            # not hosted-controlled. Only the call whose acceptance matches
            # actually refreshes, so this appears at most once per flow.
            nonlocal scan_started
            if output_json:
                return
            if sys.stderr.isatty():
                print(
                    f"\rRefreshing source logs: {done}/{total} projects "
                    f"({source})  ",
                    end="\n" if done >= total else "",
                    file=sys.stderr,
                    flush=True,
                )
            elif not scan_started:
                scan_started = True
                print(
                    "Refreshing the enrolled source logs "
                    "(a large history can take a few minutes)…",
                    file=sys.stderr,
                )

        def scan_wait_notice() -> None:
            # Fires once when the strict refresh must wait for a scan in
            # another process (usually the daemon's background pass).
            if not output_json:
                print(
                    "Waiting for another scan to finish before refreshing "
                    "(the background scanner may be mid-pass)…",
                    file=sys.stderr,
                )

        def call_enable() -> dict[str, Any]:
            hosted_notice()
            return auto_upload.enable(
                agent=args.agent,
                accepted_authorization_version=auth_version,
                accepted_retention_version=retention_version,
                accepted_ownership_certification_version=ownership_version,
                accepted_authorization_profile_hash=profile_hash,
                scan_progress=scan_progress,
                scan_wait_notice=scan_wait_notice,
            )

        result = call_enable()
        # The refresh on the accepting call can change the displayed scope
        # (a project appeared since the challenge was shown), which bounces
        # back with the refreshed challenge; one extra acceptance round
        # covers it, and that call reuses the completed refresh instead of
        # scanning again.
        for _ in range(2):
            if output_json or result.get("code") != "authorization_required":
                break
            accepted = _interactive_accept(args, result)
            if accepted is None:
                result = {
                    "ok": False,
                    "code": "authorization_not_accepted",
                    "message": "Exact recurring authorization versions were not accepted.",
                }
                break
            (
                auth_version,
                retention_version,
                ownership_version,
                profile_hash,
            ) = accepted
            result = call_enable()
        if result.get("code") in {
            "email_verification_required",
            "enrollment_response_ambiguous",
        } and not output_json:
            if _fresh_email_verification():
                # Re-fetching also catches terms changed while the code was
                # in flight; the completed refresh is reused, not repeated.
                result = call_enable()
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
