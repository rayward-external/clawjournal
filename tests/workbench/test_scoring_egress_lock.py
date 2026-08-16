"""Cross-process ownership tests for scoring AI egress."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from clawjournal.scoring.egress_lock import scoring_egress_lock
from clawjournal.workbench import index


def test_second_process_cannot_enter_scoring_egress(tmp_path, monkeypatch):
    database = tmp_path / "index.db"
    monkeypatch.setattr(index, "INDEX_DB", database)
    script = """
import pathlib
import sys
from clawjournal.workbench import index
from clawjournal.scoring.egress_lock import scoring_egress_lock

index.INDEX_DB = pathlib.Path(sys.argv[1])
with scoring_egress_lock(blocking=True) as acquired:
    print("LOCKED" if acquired else "FAILED", flush=True)
    sys.stdin.readline()
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script, str(database)],
        cwd=Path(__file__).resolve().parents[2],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == "LOCKED"
        with scoring_egress_lock(blocking=False) as acquired:
            assert acquired is False
    finally:
        if child.stdin is not None:
            child.stdin.write("release\n")
            child.stdin.flush()
        _stdout, stderr = child.communicate(timeout=10)
    assert child.returncode == 0, stderr

    with scoring_egress_lock(blocking=False) as acquired:
        assert acquired is True
