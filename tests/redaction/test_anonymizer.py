"""Tests for clawjournal.redaction.anonymizer — PII anonymization."""

import pytest

from clawjournal.redaction.anonymizer import (
    Anonymizer,
    _replace_username,
    _USERNAME_PLACEHOLDER,
    _PATH_PLACEHOLDER,
    anonymize_path,
    anonymize_text,
)


# --- anonymize_path ---


class TestAnonymizePath:
    def test_empty_path(self):
        assert anonymize_path("", "alice") == ""

    def test_home_path_redacted(self):
        result = anonymize_path(
            "/Users/alice/Documents/myproject/src/main.py",
            "alice", home="/Users/alice",
        )
        assert result == _PATH_PLACEHOLDER

    def test_downloads_redacted(self):
        result = anonymize_path(
            "/Users/alice/Downloads/file.zip",
            "alice", home="/Users/alice",
        )
        assert result == _PATH_PLACEHOLDER

    def test_bare_home_redacted(self):
        result = anonymize_path(
            "/Users/alice/somedir/file.py",
            "alice", home="/Users/alice",
        )
        assert result == _PATH_PLACEHOLDER

    def test_linux_home_path_redacted(self):
        result = anonymize_path(
            "/home/alice/Documents/project/file.py",
            "alice", home="/home/alice",
        )
        assert result == _PATH_PLACEHOLDER

    def test_path_not_under_home_preserved(self):
        result = anonymize_path(
            "/var/log/syslog",
            "alice", home="/Users/alice",
        )
        assert result == "/var/log/syslog"


# --- anonymize_text ---


class TestAnonymizeText:
    def test_empty_text(self):
        assert anonymize_text("", "alice") == ""

    def test_empty_username(self):
        assert anonymize_text("hello alice", "") == "hello alice"

    def test_none_text(self):
        assert anonymize_text(None, "alice") is None

    def test_users_path_replaced(self):
        result = anonymize_text(
            "File at /Users/alice/project/main.py",
            "alice",
        )
        assert _PATH_PLACEHOLDER in result
        assert "alice" not in result

    def test_home_path_replaced(self):
        result = anonymize_text(
            "File at /home/alice/project/main.py",
            "alice",
        )
        assert _PATH_PLACEHOLDER in result
        assert "alice" not in result

    def test_hyphen_encoded_path(self):
        result = anonymize_text(
            "-Users-alice-Documents-myproject",
            "alice",
        )
        assert _PATH_PLACEHOLDER in result
        assert "alice" not in result

    def test_temp_path(self):
        result = anonymize_text(
            "/private/tmp/claude-501/-Users-alice-Documents-proj/foo",
            "alice",
        )
        assert "alice" not in result
        assert _PATH_PLACEHOLDER in result

    def test_bare_username_replaced(self):
        result = anonymize_text(
            "Hello alice, welcome back",
            "alice",
        )
        assert "alice" not in result
        assert _USERNAME_PLACEHOLDER in result

    def test_bare_username_adjacent_cjk_replaced(self):
        # `\b` never fires between CJK and ASCII word chars (#195)
        result = anonymize_text("请把alice的文件发我", "alice")
        assert "alice" not in result
        assert _USERNAME_PLACEHOLDER in result

    def test_short_username_not_replaced_bare(self):
        # Usernames < 4 chars should NOT be replaced as bare words
        result = anonymize_text(
            "Hello bob, welcome back",
            "bob",
        )
        assert "bob" in result  # bare replacement skipped for short username

    def test_short_username_path_still_replaced(self):
        # Even short usernames should be replaced in path contexts
        result = anonymize_text(
            "File at /Users/bob/project/main.py",
            "bob",
        )
        assert _PATH_PLACEHOLDER in result


# --- Anonymizer class ---


class TestAnonymizer:
    def test_path_method(self, mock_anonymizer):
        result = mock_anonymizer.path("/Users/testuser/Documents/myproject/main.py")
        assert "testuser" not in result
        assert result == _PATH_PLACEHOLDER

    def test_text_method(self, mock_anonymizer):
        result = mock_anonymizer.text("Hello testuser, your home is /Users/testuser")
        assert "testuser" not in result

    def test_deterministic(self, mock_anonymizer):
        r1 = mock_anonymizer.path("/Users/testuser/Documents/proj/a.py")
        r2 = mock_anonymizer.path("/Users/testuser/Documents/proj/a.py")
        assert r1 == r2

    def test_extra_usernames(self, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.redaction.anonymizer._detect_home_dir",
            lambda: ("/Users/testuser", "testuser"),
        )
        anon = Anonymizer(extra_usernames=["github_handle"])
        result = anon.text("by github_handle on GitHub")
        assert "github_handle" not in result

    def test_extra_usernames_dedup(self, monkeypatch):
        monkeypatch.setattr(
            "clawjournal.redaction.anonymizer._detect_home_dir",
            lambda: ("/Users/testuser", "testuser"),
        )
        anon = Anonymizer(extra_usernames=["testuser", "other"])
        assert len(anon._extra) == 1  # only "other"


# --- _replace_username ---


class TestReplaceUsername:
    def test_case_insensitive(self):
        result = _replace_username("Hello ALICE and Alice", "alice")
        assert "ALICE" not in result
        assert "Alice" not in result
        assert _USERNAME_PLACEHOLDER in result

    def test_short_username_skipped(self):
        result = _replace_username("Hello ab and AB", "ab")
        assert result == "Hello ab and AB"

    def test_empty_text(self):
        assert _replace_username("", "alice") == ""

    def test_empty_username(self):
        assert _replace_username("hello", "") == "hello"

    def test_none_text(self):
        assert _replace_username(None, "alice") is None
