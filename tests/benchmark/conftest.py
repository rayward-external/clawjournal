"""Shared fixtures for benchmark module tests."""

import pytest

from clawjournal.workbench.index import open_index


@pytest.fixture
def index_conn(tmp_path, monkeypatch):
    """Open an index DB (with the benchmark tables) in a temp directory."""
    monkeypatch.setattr("clawjournal.workbench.index.INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr("clawjournal.workbench.index.BLOBS_DIR", tmp_path / "blobs")
    conn = open_index()
    yield conn
    conn.close()
