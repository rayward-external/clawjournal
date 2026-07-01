"""Mode A preflight (§7.0): refuse to run with a broken/empty setup.

Requires explicit source scope + confirmed projects, an installed agent backend,
and TruffleHog present (unless an explicit dev bypass). Returns concrete
next-step messages instead of silently producing an empty skill.
"""

from __future__ import annotations

import shutil

from ..config import load_config
from ..redaction import trufflehog
from ..scoring.backends import AUTO_BACKEND_FALLBACK_ORDER, BACKEND_COMMANDS


def installed_backends() -> list[str]:
    return [b for b in AUTO_BACKEND_FALLBACK_ORDER if shutil.which(BACKEND_COMMANDS.get(b, b))]


def preflight(*, require_trufflehog: bool = True, backend: str = "auto") -> list[str]:
    """Return a list of blocking problems (empty = good to go)."""
    problems: list[str] = []
    cfg = load_config()
    if not cfg.get("source"):
        problems.append("Source scope is not set. Run: clawjournal config --source all")
    if not cfg.get("projects_confirmed"):
        problems.append("Projects are not confirmed. Run: clawjournal config --confirm-projects")
    requested = (backend or "auto").strip().lower()
    if requested != "auto":
        command = BACKEND_COMMANDS.get(requested)
        if not command:
            problems.append(f"Unsupported backend: {backend}")
        elif not shutil.which(command):
            problems.append(f"{requested} backend is not on PATH (missing `{command}`).")
    elif not installed_backends():
        problems.append("No agent backend found on PATH (need `claude` or `codex`).")
    if require_trufflehog and not trufflehog.is_bypassed() and not trufflehog.is_available():
        problems.append(
            "TruffleHog is not installed (the secret-scan gate). Run: "
            "clawjournal trufflehog install  (or set CLAWJOURNAL_SKIP_TRUFFLEHOG=1 for dev)."
        )
    return problems
