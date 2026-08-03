"""Capability overlay tests (phase-1 plan 08)."""

from __future__ import annotations

import importlib
import warnings
from pathlib import Path

import pytest

from clawjournal.events.doctor import overlay as overlay_mod


@pytest.fixture(autouse=True)
def _reset_overlay_cache(monkeypatch, tmp_path):
    """Each test gets a fresh ~/.clawjournal/ via monkeypatched home."""

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    overlay_mod.reset_cache()
    yield
    overlay_mod.reset_cache()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_overlay_path_follows_configured_state_root(monkeypatch, tmp_path):
    state_root = tmp_path / "relocated-state"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", state_root)

    assert overlay_mod.overlay_path() == state_root / "capability_overlay.yaml"


def test_no_overlay_returns_shipped_matrix():
    matrix = overlay_mod.effective_matrix()
    # Sanity: claude/user_message ships as supported.
    assert matrix[("claude", "user_message")][0] is True


def test_overlay_adds_supported_entry():
    _write(
        overlay_mod.overlay_path(),
        """version: 1
entries:
  - client: claude
    event_type: stderr_chunk
    supported: true
    reason: introduced in client 1.45
""",
    )
    matrix = overlay_mod.effective_matrix()
    supported, reason = matrix[("claude", "stderr_chunk")]
    assert supported is True
    assert reason == "introduced in client 1.45"


def test_overlay_refuses_downgrade(monkeypatch):
    _write(
        overlay_mod.overlay_path(),
        """version: 1
entries:
  - client: claude
    event_type: user_message
    supported: false
    reason: try to disable
""",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        matrix = overlay_mod.effective_matrix()
    # Shipped value wins.
    assert matrix[("claude", "user_message")][0] is True
    # Warning surfaced.
    messages = [str(w.message) for w in caught]
    assert any("refuses to downgrade" in m for m in messages)


def test_malformed_yaml_warns_and_passes_through():
    _write(overlay_mod.overlay_path(), "{ unclosed\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        matrix = overlay_mod.effective_matrix()
    messages = [str(w.message) for w in caught]
    assert any("malformed YAML" in m for m in messages)
    # Shipped matrix still loaded.
    assert matrix[("claude", "user_message")][0] is True


def test_unknown_client_skipped():
    _write(
        overlay_mod.overlay_path(),
        """version: 1
entries:
  - client: bogus
    event_type: user_message
    supported: true
    reason: nope
""",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        overlay_mod.effective_matrix()
    messages = [str(w.message) for w in caught]
    assert any("unknown client" in m for m in messages)


def test_unknown_event_type_skipped():
    _write(
        overlay_mod.overlay_path(),
        """version: 1
entries:
  - client: claude
    event_type: tool_call_v2
    supported: true
    reason: structural drift
""",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        overlay_mod.effective_matrix()
    messages = [str(w.message) for w in caught]
    assert any("unknown event_type" in m for m in messages)


def test_entry_count_cap_rejects_oversized_overlay():
    rows = "\n".join(
        f"  - {{client: claude, event_type: tool_call, supported: true, reason: r{i}}}"
        for i in range(101)
    )
    _write(overlay_mod.overlay_path(), f"version: 1\nentries:\n{rows}\n")
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        matrix = overlay_mod.effective_matrix()
    messages = [str(w.message) for w in caught]
    assert any(
        "exceeds maximum" in m or "entries exceeds" in m for m in messages
    )
    # Shipped matrix unchanged.
    assert matrix[("claude", "user_message")][0] is True


def test_unknown_major_version_ignored():
    _write(
        overlay_mod.overlay_path(),
        "version: 99\nentries: []\n",
    )
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        overlay_mod.effective_matrix()
    messages = [str(w.message) for w in caught]
    assert any("newer than supported" in m for m in messages)


def test_write_overlay_entries_round_trip():
    overlay_mod.write_overlay_entries(
        [
            {
                "client": "codex",
                "event_type": "stderr_chunk",
                "supported": True,
                "reason": "test",
            }
        ]
    )
    matrix = overlay_mod.effective_matrix()
    assert matrix[("codex", "stderr_chunk")][0] is True


def test_write_overlay_backs_up_malformed_existing():
    """A user mid-edit (malformed YAML in overlay file) running
    `events doctor --fix` must not silently lose their work — the
    pre-existing content is preserved as `.bak`."""

    target = overlay_mod.overlay_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    malformed = "version: 1\nentries:\n  { unclosed"
    target.write_text(malformed, encoding="utf-8")

    with warnings.catch_warnings(record=True):
        warnings.simplefilter("always")
        overlay_mod.write_overlay_entries(
            [
                {
                    "client": "claude",
                    "event_type": "compaction",
                    "supported": True,
                    "reason": "test",
                }
            ]
        )

    backup = target.with_suffix(".yaml.bak")
    assert backup.exists(), "expected .bak file with previous malformed content"
    assert backup.read_text() == malformed
    # New overlay is well-formed.
    assert "version: 1" in target.read_text()
    assert "compaction" in target.read_text()


@pytest.mark.parametrize(
    "argv",
    [
        ["clawjournal", "--help"],
        ["clawjournal", "events", "--help"],
        ["clawjournal", "events", "ingest", "--help"],
        ["clawjournal", "events", "doctor", "--help"],
    ],
)
def test_lazy_load_no_pyyaml_on_help(tmp_path, argv):
    """No CLI help path imports PyYAML — only paths that actually need
    the overlay or features should pay the cost."""

    import subprocess
    import sys

    script = tmp_path / "probe.py"
    script.write_text(
        "import sys, io, contextlib\n"
        f"sys.argv = {argv!r}\n"
        "from clawjournal.cli import main\n"
        "buf = io.StringIO()\n"
        "with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):\n"
        "    try:\n"
        "        main()\n"
        "    except SystemExit:\n"
        "        pass\n"
        "print('YAML_LOADED' if 'yaml' in sys.modules else 'YAML_ABSENT')\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        [sys.executable, str(script)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("YAML_ABSENT"), (
        f"argv={argv!r}\nstdout: {result.stdout!r}\nstderr: {result.stderr!r}"
    )
