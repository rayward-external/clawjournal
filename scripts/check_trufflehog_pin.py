#!/usr/bin/env python3
"""Verify the vendored TruffleHog checksums match upstream's release.

Compares ``clawjournal/redaction/trufflehog_install.py``'s
``_ARCHIVE_SHA256`` (and ``PINNED_VERSION``) against an upstream
``checksums.txt``. Exits non-zero with a clear diff on any mismatch.

This catches a fat-fingered or partial pin bump — a maintainer updating
``PINNED_VERSION`` but forgetting a hash row, or pasting one platform's
checksum into another's — that the hermetic unit tests cannot see
because they never touch the network. The network fetch itself lives in
the CI workflow (authenticated, with retry); this script is pure
comparison so it stays unit-testable.

Usage:
    python scripts/check_trufflehog_pin.py path/to/checksums.txt
"""

from __future__ import annotations

import sys
from pathlib import Path

# Importable without `pip install` regardless of cwd: clawjournal/ lives
# one level up from scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clawjournal.redaction.trufflehog_install import (  # noqa: E402
    PINNED_VERSION,
    _ARCHIVE_SHA256,
)


def parse_checksums(text: str) -> dict[str, str]:
    """Parse upstream ``checksums.txt`` lines of the form ``<sha256>  <filename>``."""
    out: dict[str, str] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) != 2:
            continue
        sha, name = parts
        out[name] = sha.lower()
    return out


def compare(upstream: dict[str, str]) -> list[str]:
    """Return a list of human-readable mismatch messages (empty == OK)."""
    errors: list[str] = []
    for key, vendored in sorted(_ARCHIVE_SHA256.items()):
        filename = f"trufflehog_{PINNED_VERSION}_{key}.tar.gz"
        actual = upstream.get(filename)
        if actual is None:
            errors.append(f"{filename}: not present in upstream checksums.txt")
        elif actual != vendored.lower():
            errors.append(f"{filename}: vendored {vendored} != upstream {actual}")
    return errors


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_trufflehog_pin.py <checksums.txt>", file=sys.stderr)
        return 2
    try:
        text = Path(argv[1]).read_text()
    except OSError as exc:
        print(f"could not read {argv[1]}: {exc}", file=sys.stderr)
        return 2

    errors = compare(parse_checksums(text))
    if errors:
        print(f"TruffleHog pin mismatch for v{PINNED_VERSION}:", file=sys.stderr)
        for err in errors:
            print(f"  {err}", file=sys.stderr)
        print(
            "\nIf you bumped PINNED_VERSION, refresh every _ARCHIVE_SHA256 row "
            "from the new release's checksums.txt. A stale row fails closed.",
            file=sys.stderr,
        )
        return 1

    print(
        f"All {len(_ARCHIVE_SHA256)} vendored checksums match "
        f"upstream TruffleHog v{PINNED_VERSION}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
