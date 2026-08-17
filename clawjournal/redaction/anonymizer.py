"""Anonymize PII in Claude Code log data."""

import os
import re

# Typed redaction placeholders — consistent with pii.py:PLACEHOLDER_BY_TYPE
_USERNAME_PLACEHOLDER = "[REDACTED_USERNAME]"
_PATH_PLACEHOLDER = "[REDACTED_PATH]"


def _detect_home_dir() -> tuple[str, str]:
    home = os.path.expanduser("~")
    username = os.path.basename(home)
    return home, username


def anonymize_path(path: str, username: str, home: str | None = None) -> str:
    """Replace any path containing the user's home directory with [REDACTED_PATH]."""
    if not path:
        return path

    if home is None:
        home = os.path.expanduser("~")

    # If the path starts with any home-dir variant, redact the whole thing
    for base in (f"/Users/{username}", f"/home/{username}", home):
        if path.startswith(base):
            return _PATH_PLACEHOLDER

    # Also catch partial replacements
    if f"/Users/{username}/" in path or f"/home/{username}/" in path:
        return _PATH_PLACEHOLDER

    return path


def anonymize_text(text: str, username: str) -> str:
    """Replace home-directory paths and bare usernames in free-form text."""
    if not text or not username:
        return text

    escaped = re.escape(username)

    # Replace full paths: /Users/<username>/... or /home/<username>/... → [REDACTED_PATH]
    text = re.sub(rf"/Users/{escaped}/[^\s\"'`,;)}}\]]*", _PATH_PLACEHOLDER, text)
    text = re.sub(rf"/home/{escaped}/[^\s\"'`,;)}}\]]*", _PATH_PLACEHOLDER, text)
    text = re.sub(rf"/Users/{escaped}(?=/|[^a-zA-Z0-9_-]|$)", _PATH_PLACEHOLDER, text)
    text = re.sub(rf"/home/{escaped}(?=/|[^a-zA-Z0-9_-]|$)", _PATH_PLACEHOLDER, text)

    # Catch hyphen-encoded paths: -Users-jsmith- or -Users-jsmith/
    text = re.sub(rf"-Users-{escaped}(?=-|/|$)", _PATH_PLACEHOLDER, text)
    text = re.sub(rf"-home-{escaped}(?=-|/|$)", _PATH_PLACEHOLDER, text)

    # Also handle underscore-to-hyphen encoding: jane_doe → jane-doe
    if "_" in username:
        hyphen_variant = username.replace("_", "-")
        hyphen_escaped = re.escape(hyphen_variant)
        text = re.sub(rf"-Users-{hyphen_escaped}(?=-|/|$)", _PATH_PLACEHOLDER, text)
        text = re.sub(rf"-home-{hyphen_escaped}(?=-|/|$)", _PATH_PLACEHOLDER, text)

    # Catch temp paths like /private/tmp/claude-501/-Users-jsmith/
    text = re.sub(rf"claude-\d+/-Users-{escaped}[^\s]*", _PATH_PLACEHOLDER, text)

    # Final pass: replace bare username in remaining contexts (ls output, prose, etc.)
    # Only if username is >= 4 chars to avoid false positives
    if len(username) >= 4:
        # ASCII-only boundaries: `\b` treats CJK as word characters, so a
        # username adjacent to CJK prose would never match.
        text = re.sub(
            rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
            _USERNAME_PLACEHOLDER,
            text,
        )

    return text


class Anonymizer:
    """Stateful anonymizer that consistently replaces usernames and paths.

    `enabled=False` makes `text()` and `path()` pass-through. Ingest uses the
    disabled variant so raw content sits in local blobs (local views show
    real paths); egress paths (share export, AI scoring) construct an
    enabled instance at the boundary.
    """

    def __init__(
        self,
        extra_usernames: list[str] | None = None,
        *,
        enabled: bool = True,
    ):
        self.home, self.username = _detect_home_dir()
        self.username_hash = _USERNAME_PLACEHOLDER  # kept for API compat
        self.enabled = enabled

        self._extra: list[str] = []
        for name in (extra_usernames or []):
            name = name.strip()
            if name and name != self.username:
                self._extra.append(name)

    def path(self, file_path: str) -> str:
        if not self.enabled:
            return file_path
        result = anonymize_path(file_path, self.username, self.home)
        result = anonymize_text(result, self.username)
        for name in self._extra:
            result = _replace_username(result, name)
        return result

    def text(self, content: str) -> str:
        if not self.enabled:
            return content
        result = anonymize_text(content, self.username)
        for name in self._extra:
            result = _replace_username(result, name)
        return result


def _replace_username(text: str, username: str) -> str:
    if not text or not username or len(username) < 3:
        return text
    escaped = re.escape(username)
    text = re.sub(escaped, _USERNAME_PLACEHOLDER, text, flags=re.IGNORECASE)
    return text
