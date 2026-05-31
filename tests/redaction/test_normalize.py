"""Tests for terminal-control-sequence normalization on the share path."""

from clawjournal.redaction.normalize import strip_terminal_control_sequences


class TestStripTerminalControlSequences:
    def test_strips_sgr_color_codes(self):
        # The exact false-positive shape that tripped TruffleHog's Azure detector.
        assert strip_terminal_control_sequences("\x1b[90mnull\x1b[0m\x1b[0m") == "null"

    def test_keeps_plain_text_and_standard_whitespace(self):
        text = "hello\tworld\nsecond line\r\nthird"
        assert strip_terminal_control_sequences(text) == text

    def test_strips_csi_cursor_and_erase_sequences(self):
        assert strip_terminal_control_sequences("a\x1b[2K\x1b[1Gb") == "ab"

    def test_strips_osc_title_sequence(self):
        # OSC 0 ; <title> BEL — used to set the terminal title.
        assert strip_terminal_control_sequences("x\x1b]0;my-title\x07y") == "xy"

    def test_strips_stray_c0_controls_but_keeps_tab_newline_cr(self):
        assert strip_terminal_control_sequences("a\x00\x07b\tc\nd\re") == "ab\tc\nd\re"

    def test_preserves_non_ascii_utf8(self):
        # Must not corrupt multibyte UTF-8 (C1 range bytes are part of those).
        assert strip_terminal_control_sequences("café — 日本語") == "café — 日本語"

    def test_empty_and_clean_strings_unchanged(self):
        assert strip_terminal_control_sequences("") == ""
        assert strip_terminal_control_sequences("nothing to strip") == "nothing to strip"

    def test_strips_8bit_c1_csi(self):
        # 8-bit CSI introducer (U+009B) instead of ESC[.
        assert strip_terminal_control_sequences("a\x9b31mb") == "ab"

    def test_strips_dcs_pm_apc_sos_payloads(self):
        # ESC P/^/_/X ... ST — the whole payload must go, not just the introducer.
        assert strip_terminal_control_sequences("a\x1bP1;2|payload\x1b\\b") == "ab"
        assert strip_terminal_control_sequences("a\x1b^pm-data\x1b\\b") == "ab"
        assert strip_terminal_control_sequences("a\x1b_apc-data\x1b\\b") == "ab"
        assert strip_terminal_control_sequences("a\x1bXsos-data\x1b\\b") == "ab"

    def test_strips_8bit_osc_and_dcs_with_st(self):
        assert strip_terminal_control_sequences("a\x9d0;title\x9cb") == "ab"  # 8-bit OSC + ST
        assert strip_terminal_control_sequences("a\x90dcs\x9cb") == "ab"      # 8-bit DCS + ST

    def test_strips_stray_c1_controls(self):
        # Lone C1 controls (NEL, ST) are not legitimate text content.
        assert strip_terminal_control_sequences("a\x85\x9cb") == "ab"

    def test_unterminated_osc_dcs_does_not_eat_trailing_text(self):
        # A payload-bearing escape with no BEL/ST terminator must NOT consume
        # the rest of the string — that would drop legitimate trailing content
        # on truncated terminal captures.
        assert strip_terminal_control_sequences("a\x1b]0;titleb") == "a]0;titleb"
        out = strip_terminal_control_sequences("a\x1bPpayload no-st KEEPME")
        assert "KEEPME" in out and "\x1b" not in out
