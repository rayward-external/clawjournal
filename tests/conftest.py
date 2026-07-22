"""Shared fixtures for clawjournal tests."""

import sys

import pytest

from clawjournal.redaction.anonymizer import Anonymizer


def _clear_strict_scan_reuse() -> None:
    # getattr-guarded so a mixed setup (this conftest running against an
    # older installed clawjournal without the memo, e.g. the editable
    # install's console script inside a worktree) errors in the tests that
    # actually exercise the new behavior instead of at every test's setup.
    auto_upload = sys.modules.get("clawjournal.auto_upload")
    reset = getattr(auto_upload, "_reset_strict_scan_reuse", None)
    if reset is not None:
        reset()


@pytest.fixture(autouse=True)
def _reset_strict_scan_reuse():
    """auto_upload.enable() memoizes a completed strict refresh at module
    level so one interactive enrollment only re-parses the history once. A
    test's refresh must never satisfy another test's, so clear the memo
    around every test (lazily — most tests never import auto_upload).
    """
    _clear_strict_scan_reuse()
    yield
    _clear_strict_scan_reuse()


@pytest.fixture(autouse=True)
def _bypass_secret_scanners_by_default(monkeypatch):
    """TruffleHog and Betterleaks are mandatory share-time gates in
    production, but most tests don't have the binaries installed and
    shouldn't depend on them. Default every test to the bypass path;
    tests that exercise a gate itself ``monkeypatch.delenv(...)`` the
    relevant variable and mock the subprocess. Two separate variables
    (not one unified switch) so each wrapper's ``is_bypassed`` stays
    self-contained and the manifest records which scanner was bypassed.
    """
    monkeypatch.setenv("CLAWJOURNAL_SKIP_TRUFFLEHOG", "1")
    monkeypatch.setenv("CLAWJOURNAL_SKIP_BETTERLEAKS", "1")


@pytest.fixture
def sample_user_entry():
    """Realistic JSONL user entry dict."""
    return {
        "type": "user",
        "timestamp": 1706000000000,
        "cwd": "/Users/testuser/Documents/myproject",
        "gitBranch": "main",
        "version": "1.0.0",
        "sessionId": "abc-123",
        "message": {
            "content": "Fix the login bug in src/auth.py",
        },
    }


@pytest.fixture
def sample_assistant_entry():
    """Realistic JSONL assistant entry dict."""
    return {
        "type": "assistant",
        "timestamp": 1706000001000,
        "message": {
            "model": "claude-sonnet-4-20250514",
            "content": [
                {"type": "thinking", "thinking": "Let me look at the auth file."},
                {"type": "text", "text": "I'll fix the login bug."},
                {
                    "type": "tool_use",
                    "name": "Read",
                    "input": {"file_path": "/Users/testuser/Documents/myproject/src/auth.py"},
                },
            ],
            "usage": {
                "input_tokens": 500,
                "output_tokens": 100,
                "cache_read_input_tokens": 200,
            },
        },
    }


@pytest.fixture
def mock_anonymizer(monkeypatch):
    """Anonymizer with patched _detect_home_dir returning deterministic values."""
    monkeypatch.setattr(
        "clawjournal.redaction.anonymizer._detect_home_dir",
        lambda: ("/Users/testuser", "testuser"),
    )
    return Anonymizer()


@pytest.fixture(autouse=True)
def tmp_config(tmp_path, monkeypatch):
    """Monkeypatch CONFIG_FILE and CONFIG_DIR to tmp_path — for EVERY test.

    Autouse because this has burned us for real: a test that mocks ``load_config``
    but exercises a code path that calls the real ``save_config`` (e.g. the share
    path's ``_clear_stored_upload_token``) silently OVERWRITES the developer's own
    ``~/.clawjournal/config.json`` with fixture data. The real functions must never
    see the real home config under pytest.
    """
    config_dir = tmp_path / ".clawjournal"
    config_file = config_dir / "config.json"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", config_dir)
    monkeypatch.setattr("clawjournal.config.CONFIG_FILE", config_file)
    return config_file
