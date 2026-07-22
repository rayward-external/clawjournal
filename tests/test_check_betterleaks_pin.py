"""Hermetic tests for scripts/check_betterleaks_pin.py.

The script's network fetch lives in the CI workflow; the comparison
logic is pure and tested here without touching the network. A synthetic
"upstream" is built from the vendored table so the matching case can
never silently rot, and corruption cases prove the mismatch paths.
"""

import importlib.util
from pathlib import Path

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_betterleaks_pin.py"
_spec = importlib.util.spec_from_file_location("check_betterleaks_pin", _SCRIPT)
pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pin)


def _upstream_text_matching_table() -> str:
    """A checksums.txt whose rows match the vendored table exactly, plus
    extra unrelated rows (sigstore bundle, other artifacts) that must be
    ignored."""
    lines = ["deadbeef" * 8 + "  betterleaks_extra_artifact.deb"]
    for key, sha in pin._ARCHIVE_SHA256.items():
        lines.append(f"{sha}  {pin.archive_filename(key)}")
    return "\n".join(lines) + "\n"


def test_parse_checksums_ignores_malformed_lines():
    text = (
        "abc123  file_a.tar.gz\n"
        "\n"
        "# a comment with spaces\n"
        "onlyonefield\n"
        "DEAD  file_b.zip\n"
    )
    parsed = pin.parse_checksums(text)
    assert parsed == {"file_a.tar.gz": "abc123", "file_b.zip": "dead"}


def test_compare_passes_when_table_matches_upstream():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    assert pin.compare(upstream) == []


def test_compare_uses_upstream_archive_flavors():
    # The vendored table's windows rows must be compared against .zip
    # filenames — a .tar.gz-only comparison would report every windows
    # row missing (or worse, silently pass on a stale name).
    text = _upstream_text_matching_table()
    assert f"betterleaks_{pin.PINNED_VERSION}_windows_x64.zip" in text
    assert f"betterleaks_{pin.PINNED_VERSION}_linux_x64.tar.gz" in text


def test_compare_flags_a_wrong_hash():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    target = pin.archive_filename("linux_x64")
    upstream[target] = "0" * 64
    errors = pin.compare(upstream)
    assert len(errors) == 1
    assert target in errors[0]
    assert "vendored" in errors[0] and "upstream" in errors[0]


def test_compare_flags_a_missing_row():
    upstream = pin.parse_checksums(_upstream_text_matching_table())
    target = pin.archive_filename("windows_arm64")
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
    bad.write_text("0000  betterleaks_0.0.0_linux_x64.tar.gz\n")
    assert pin.main(["prog", str(bad)]) == 1

    assert pin.main(["prog"]) == 2  # wrong arg count
    assert pin.main(["prog", str(tmp_path / "nope.txt")]) == 2  # unreadable


def test_compare_is_case_insensitive_on_hashes():
    upstream = pin.parse_checksums(
        "\n".join(
            f"{sha.upper()}  {pin.archive_filename(key)}"
            for key, sha in pin._ARCHIVE_SHA256.items()
        )
    )
    assert pin.compare(upstream) == []
