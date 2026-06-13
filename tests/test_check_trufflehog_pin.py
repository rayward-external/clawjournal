"""Hermetic tests for scripts/check_trufflehog_pin.py.

The script's network fetch lives in the CI workflow; the comparison
logic is pure and tested here without touching the network. A synthetic
"upstream" is built from the vendored table so the matching case can
never silently rot, and corruption cases prove the mismatch paths.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_trufflehog_pin.py"
_spec = importlib.util.spec_from_file_location("check_trufflehog_pin", _SCRIPT)
pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin)


def _upstream_text_matching_table() -> str:
    """A checksums.txt whose rows match the vendored table exactly, plus
    extra unrelated rows (man pages, other artifacts) that must be ignored."""
    lines = ["deadbeef" * 8 + "  trufflehog_extra_artifact.deb"]
    for key, sha in pin._ARCHIVE_SHA256.items():
        lines.append(f"{sha}  trufflehog_{pin.PINNED_VERSION}_{key}.tar.gz")
    return "\n".join(lines) + "\n"


def test_parse_checksums_ignores_malformed_lines():
    text = (
        "abc123  file_a.tar.gz\n"
        "\n"
        "# a comment with spaces\n"
        "onlyonefield\n"
        "DEAD  file_b.tar.gz\n"
    )
    parsed = pin.parse_checksums(text)
    assert parsed == {"file_a.tar.gz": "abc123", "file_b.tar.gz": "dead"}


def test_compare_passes_when_table_matches_upstream():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    assert pin.compare(upstream) == []


def test_compare_flags_a_wrong_hash():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    # Corrupt one row's hash.
    target = f"trufflehog_{pin.PINNED_VERSION}_linux_amd64.tar.gz"
    upstream[target] = "0" * 64
    errors = pin.compare(upstream)
    assert len(errors) == 1
    assert target in errors[0]
    assert "vendored" in errors[0] and "upstream" in errors[0]


def test_compare_flags_a_missing_row():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    target = f"trufflehog_{pin.PINNED_VERSION}_windows_arm64.tar.gz"
    del upstream[target]
    errors = pin.compare(upstream)
    assert len(errors) == 1
    assert "not present" in errors[0]


def test_main_exit_codes(tmp_path, capsys):
    good = tmp_path / "good.txt"
    good.write_text(_upstream_text_matching_table())
    assert pin.main(["prog", str(good)]) == 0
    assert "match upstream" in capsys.readouterr().out

    bad = tmp_path / "bad.txt"
    bad.write_text("0000  trufflehog_0.0.0_linux_amd64.tar.gz\n")
    assert pin.main(["prog", str(bad)]) == 1

    assert pin.main(["prog"]) == 2  # wrong arg count
    assert pin.main(["prog", str(tmp_path / "nope.txt")]) == 2  # unreadable


def test_compare_is_case_insensitive_on_hashes():
    upstream = pin.parse_checksums(
        "\n".join(
            f"{sha.upper()}  trufflehog_{pin.PINNED_VERSION}_{key}.tar.gz"
            for key, sha in pin._ARCHIVE_SHA256.items()
        )
    )
    assert pin.compare(upstream) == []
