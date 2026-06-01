"""Text normalization for the share/export path.

Terminal output captured in agent sessions carries ANSI/VT escape sequences
(colors, cursor moves, OSC window-title, DCS/PM/APC payloads) and stray
control bytes. These are rendering artifacts, not content: they bloat shared
traces and trip TruffleHog's broad detectors as false positives — e.g. the
generic Azure detector matched a base64-decoded ``\x1b[0m`` run and helped
block an otherwise-clean share. Stripping them on the export path keeps shared
bundles clean.
"""

import re

# ANSI / VT escape sequences (ECMA-48), covering both the 7-bit (ESC-prefixed)
# and 8-bit (C1) introducer forms. Sequences with payloads (CSI/OSC/DCS/…) are
# matched whole; the payload character classes are negated (bounded) to avoid
# catastrophic backtracking. The OSC/DCS terminators are REQUIRED: an
# unterminated sequence (e.g. a truncated capture) must not greedily consume
# the rest of the string and drop legitimate trailing text — the stray ESC
# introducer is left to the control scrub instead. Order matters: payload-
# bearing forms are tried before the single-byte Fe escape.
_ANSI_ESCAPE_RE = re.compile(
    r"(?:"
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]"                      # CSI (7- and 8-bit)
    r"|(?:\x1b\]|\x9d)[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)"  # OSC ... BEL/ST (required)
    r"|(?:\x1b[PX^_]|[\x90\x98\x9e\x9f])[^\x1b\x9c]*(?:\x1b\\|\x9c)"  # DCS/SOS/PM/APC ... ST (required)
    r"|\x1b[@-Z\\^_]"                                        # other single-byte Fe escapes
    r")"
)

# Stray control characters left over after escape stripping: C0 (excluding tab
# 0x09, newline 0x0a, carriage return 0x0d), DEL (0x7f), and C1 (0x80-0x9f).
# Operating on a decoded ``str`` (code points, not bytes), so stripping C1
# does not corrupt multibyte UTF-8 — those characters are higher code points.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


def strip_terminal_control_sequences(text: str) -> str:
    """Remove ANSI/VT escape sequences and stray control characters from text.

    Tab, newline, and carriage return are preserved, as is all printable and
    multibyte-UTF-8 content. Non-string input is returned unchanged so callers
    can apply this defensively to mixed values.
    """
    if not isinstance(text, str) or not text or not _CONTROL_RE.search(text):
        return text
    text = _ANSI_ESCAPE_RE.sub("", text)
    text = _CONTROL_RE.sub("", text)
    return text
