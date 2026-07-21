"""Managed share-scanner readiness and installation.

The share gate must remain fail-closed, but a ClawJournal update can add a new
required scanner before an existing participant has installed its binary.  This
module is the single preflight used by enrollment, CLI sharing, and the
workbench to repair that dependency gap with the existing pinned,
checksum-verified installers.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from typing import Any


_INSTALL_LOCK = threading.Lock()
_INSTALL_OK = frozenset({"installed", "already-installed"})


def _truthy(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in {"", "0", "false", "no", "off"}


def ensure_share_scanners(
    *,
    prefer_managed: bool = False,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Ensure Betterleaks and TruffleHog are usable for sharing.

    Missing scanners are installed into ``~/.clawjournal/bin`` using the
    pinned, checksum-verified managed installers.  ``prefer_managed`` also
    installs a managed copy when a PATH binary is already usable; enrollment
    and explicit workbench recovery use this for reproducible participant
    setup, while routine share preflight only repairs missing dependencies.

    The test/development bypass variables remain authoritative: a bypassed
    scanner is not installed here, and the existing upload gate still refuses
    to ship bypassed bundles.
    """
    from . import betterleaks, betterleaks_install, trufflehog, trufflehog_install

    scanners = (
        ("betterleaks", betterleaks, betterleaks_install),
        ("trufflehog", trufflehog, trufflehog_install),
    )
    outcomes: dict[str, dict[str, Any]] = {}

    # A daemon can receive concurrent package/retry requests.  The individual
    # installers publish atomically, and this lock also avoids duplicate
    # downloads and confusing interleaved progress within one process.
    with _INSTALL_LOCK:
        for name, scanner, installer in scanners:
            before = scanner.resolve_binary()
            available_before = scanner.is_available()
            if _truthy(os.environ.get(scanner.SKIP_ENV_VAR)):
                outcomes[name] = {
                    "ok": True,
                    "status": "bypassed",
                    "install_attempted": False,
                    "available": available_before,
                    "managed": before == str(scanner.managed_binary_path()),
                }
                continue

            already_managed = before == str(scanner.managed_binary_path())
            should_install = not available_before or (
                prefer_managed and not already_managed
            )
            if not should_install:
                outcomes[name] = {
                    "ok": True,
                    "status": "available",
                    "install_attempted": False,
                    "available": True,
                    "managed": already_managed,
                    "resolved_path": before,
                }
                continue

            if progress is not None:
                progress(f"Installing the managed {name} scanner…")
            install_result = installer.install(progress=progress)
            after = scanner.resolve_binary()
            usable = scanner.is_available()
            outcome = {
                "ok": usable,
                "status": install_result.get("status", "install-failed"),
                "install_attempted": True,
                "available": usable,
                "managed": after == str(scanner.managed_binary_path()),
                "resolved_path": after,
            }
            for key in ("version", "error", "hint"):
                if install_result.get(key) is not None:
                    outcome[key] = install_result[key]
            # A working PATH copy is a safe fallback when the explicit managed
            # install failed.  Preserve the failure status for diagnostics but
            # do not strand a share whose required scanner is still usable.
            if install_result.get("status") in _INSTALL_OK and after is None:
                outcome["error"] = "The managed install completed but the scanner could not be resolved."
            outcomes[name] = outcome

    missing = [name for name, outcome in outcomes.items() if not outcome["ok"]]
    result: dict[str, Any] = {
        "ok": not missing,
        "scanners": outcomes,
        "missing": missing,
    }
    if missing:
        details = []
        for name in missing:
            outcome = outcomes[name]
            detail = outcome.get("error") or outcome.get("status") or "not available"
            details.append(f"{name}: {detail}")
        result["error"] = (
            "Could not install the required local secret scanner"
            f"{'s' if len(missing) != 1 else ''}: " + "; ".join(details)
        )
    return result
