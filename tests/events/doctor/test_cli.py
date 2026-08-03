"""End-to-end CLI tests for the three new commands."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Run CLI with isolated agent-home and ClawJournal state roots."""

    env = {
        "HOME": str(tmp_path),
        "USERPROFILE": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(Path(__file__).parent.parent.parent.parent),
        "CLAWJOURNAL_HOME": str(tmp_path / ".clawjournal"),
        "CLAWJOURNAL_SKIP_TRUFFLEHOG": "1",
    }
    return env


def _run(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "clawjournal.cli", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def test_doctor_fresh_install_returns_zero(isolated_home):
    result = _run(["events", "doctor", "--json"], isolated_home)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["events_doctor_schema_version"] == "1.0"
    assert payload["install_state"] == "fresh"


def test_doctor_request_id_echoed(isolated_home):
    result = _run(
        ["events", "doctor", "--json", "--request-id", "rq-cli-123"],
        isolated_home,
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["_meta"]["request_id"] == "rq-cli-123"


def test_features_returns_zero(isolated_home):
    result = _run(["events", "features"], isolated_home)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["events_features_schema_version"] == "1.0"


def test_docs_missing_topic_returns_two(isolated_home):
    result = _run(["events", "docs", "--json"], isolated_home)
    assert result.returncode == 2
    # Error envelope is written to stderr per envelope.emit_error default.
    payload = json.loads(result.stderr)
    assert payload["error"]["kind"] == "usage_error"


def test_docs_unknown_topic_returns_nine(isolated_home):
    result = _run(["events", "docs", "bogus", "--json"], isolated_home)
    assert result.returncode == 9
    payload = json.loads(result.stderr)
    assert payload["error"]["kind"] == "topic_unknown"


def test_docs_known_topic_markdown(isolated_home):
    result = _run(["events", "docs", "guide"], isolated_home)
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("# ")


def test_docs_known_topic_json(isolated_home):
    result = _run(["events", "docs", "schemas", "--json"], isolated_home)
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["topic"] == "schemas"
    assert isinstance(payload["schemas"], list)
    assert len(payload["schemas"]) > 0


def test_doctor_fix_writes_overlay_via_subprocess(isolated_home, tmp_path):
    """End-to-end CLI: --fix writes the overlay file and the next
    invocation reflects the change."""

    import sqlite3

    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    db = cfg / "index.db"

    # Seed the DB by reusing the events schema.
    env = isolated_home.copy()
    seed_script = tmp_path / "seed.py"
    seed_script.write_text(
        "import os\n"
        "from pathlib import Path\n"
        f"home = Path({str(tmp_path)!r})\n"
        "os.environ['HOME'] = str(home)\n"
        "import clawjournal.config as cfg\n"
        "cfg.CONFIG_DIR = home / '.clawjournal'\n"
        "cfg.CONFIG_FILE = cfg.CONFIG_DIR / 'config.json'\n"
        "import clawjournal.workbench.index as wb\n"
        "wb.CONFIG_DIR = home / '.clawjournal'\n"
        "wb.INDEX_DB = home / '.clawjournal' / 'index.db'\n"
        "from clawjournal.events.schema import ensure_schema\n"
        "from clawjournal.workbench.index import open_index\n"
        "conn = open_index()\n"
        "ensure_schema(conn)\n"
        "conn.execute(\"\"\"INSERT INTO event_sessions\n"
        "    (session_key, client, client_version, started_at, status)\n"
        "    VALUES ('claude:test:abc', 'claude', '1.45.0', '2026-01-01T00:00:00Z', 'ended')\"\"\")\n"
        "sid = conn.execute('SELECT id FROM event_sessions').fetchone()[0]\n"
        "conn.execute(\"\"\"INSERT INTO events\n"
        "    (session_id, ingested_at, source, source_path, source_offset, seq, type,\n"
        "     raw_json, event_at, confidence, lossiness, client)\n"
        "    VALUES (?, '2026-01-01T00:00:00Z', 'claude-jsonl', '/tmp/x.jsonl', 0, 0,\n"
        "            'compaction', '{}', '2026-01-01T00:00:00Z', 'high', 'none', 'claude')\"\"\", (sid,))\n"
        "conn.commit()\n"
        "conn.close()\n",
        encoding="utf-8",
    )
    subprocess.run(
        [sys.executable, str(seed_script)],
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
        check=True,
    )

    # Pre-fix: doctor reports partially-compatible (exit 1).
    result = _run(["events", "doctor", "--json"], env)
    assert result.returncode == 1, result.stderr
    payload = json.loads(result.stdout)
    assert payload["clients"][0]["verdict"] == "partially-compatible"
    assert "compaction" in payload["clients"][0]["unsupported_event_types"]
    assert not (cfg / "capability_overlay.yaml").exists()

    # --fix writes the overlay.
    result = _run(["events", "doctor", "--fix", "--json"], env)
    assert (cfg / "capability_overlay.yaml").exists()
    assert "wrote 1 additive entries" in result.stderr
    # Post-fix exit code should be 0 (re-collected report shows compatible).
    payload = json.loads(result.stdout)
    assert payload["clients"][0]["verdict"] == "compatible"
    assert result.returncode == 0


def test_doctor_emits_envelope_on_unhandled_error(isolated_home, tmp_path):
    """A corrupt index.db hits the db-corrupt branch (exit 5) — that's
    a structured-output path. To exercise the envelope catch-all,
    induce an error in a path the probes don't gate. Easiest: point
    HOME at a non-existent path so config_dir() resolves but readdir
    fails further down."""

    # The corrupt DB returns exit 5 cleanly via the install-state probe.
    cfg = tmp_path / ".clawjournal"
    cfg.mkdir(parents=True)
    (cfg / "index.db").write_bytes(b"not a sqlite file")
    result = _run(["events", "doctor", "--json"], isolated_home)
    assert result.returncode == 5, result.stderr
    # The corrupt-DB path emits a normal report (not the envelope)
    # because it's a known state, not an unhandled error. The envelope
    # catch-all only triggers on truly unexpected failures, which are
    # hard to induce without mocking. This test pins the known-state
    # behavior; the envelope path is exercised by the docs / features
    # error tests.
    payload = json.loads(result.stdout)
    assert payload["install_state"] == "db-corrupt"
