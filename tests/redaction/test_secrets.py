"""Tests for clawjournal.redaction.secrets — secret detection and redaction."""

import pytest

from clawjournal.redaction.secrets import (
    CONFIDENCE,
    REDACTED,
    SECRET_PLACEHOLDER,
    SECRET_PATTERNS,
    _apply_redaction_set,
    _check_user_allowlist,
    _has_mixed_char_types,
    _shannon_entropy,
    redact_custom_strings,
    redact_session,
    redact_text,
    scan_text,
)


# --- _shannon_entropy ---


class TestShannonEntropy:
    def test_empty_string(self):
        assert _shannon_entropy("") == 0.0

    def test_single_char(self):
        assert _shannon_entropy("a") == 0.0

    def test_repeated_char(self):
        assert _shannon_entropy("aaaa") == 0.0

    def test_two_equal_chars(self):
        # "ab" -> each has prob 0.5 -> entropy = 1.0
        assert _shannon_entropy("ab") == pytest.approx(1.0)

    def test_four_distinct_chars(self):
        # "abcd" -> each prob 0.25 -> entropy = 2.0
        assert _shannon_entropy("abcd") == pytest.approx(2.0)

    def test_high_entropy_random_string(self):
        # A realistic high-entropy string
        s = "aB3xZ9qR2mK7pL4wN8yJ5tF1hG6"
        assert _shannon_entropy(s) > 3.5

    def test_low_entropy_repetitive(self):
        s = "aaabbb"
        assert _shannon_entropy(s) < 1.5


# --- _has_mixed_char_types ---


class TestHasMixedCharTypes:
    def test_upper_only(self):
        assert _has_mixed_char_types("ABCDEF") is False

    def test_lower_only(self):
        assert _has_mixed_char_types("abcdef") is False

    def test_digit_only(self):
        assert _has_mixed_char_types("123456") is False

    def test_upper_lower_no_digit(self):
        assert _has_mixed_char_types("AbCdEf") is False

    def test_upper_digit_no_lower(self):
        assert _has_mixed_char_types("ABC123") is False

    def test_lower_digit_no_upper(self):
        assert _has_mixed_char_types("abc123") is False

    def test_mixed_all_three(self):
        assert _has_mixed_char_types("aB3xZ9") is True

    def test_empty_string(self):
        assert _has_mixed_char_types("") is False


# --- scan_text ---


class TestScanText:
    def test_empty_text(self):
        assert scan_text("") == []

    def test_no_secrets(self):
        assert scan_text("Hello, this is normal text.") == []

    def test_jwt_token(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        findings = scan_text(jwt)
        assert any(f["type"] == "jwt" for f in findings)

    def test_jwt_partial(self):
        partial = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9eyJzdWI"
        findings = scan_text(partial)
        assert any(f["type"] in ("jwt", "jwt_partial") for f in findings)

    def test_db_url(self):
        url = "postgres://myuser:s3cretP4ss@db.example.com:5432/mydb"
        findings = scan_text(url)
        assert any(f["type"] == "db_url" for f in findings)

    def test_anthropic_key(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        findings = scan_text(key)
        assert any(f["type"] == "anthropic_key" for f in findings)

    def test_openai_key(self):
        key = "sk-" + "a" * 48
        findings = scan_text(key)
        assert any(f["type"] == "openai_key" for f in findings)

    def test_hf_token(self):
        token = "hf_" + "a" * 30
        findings = scan_text(token)
        assert any(f["type"] == "hf_token" for f in findings)

    def test_github_token(self):
        token = "ghp_" + "a" * 36
        findings = scan_text(token)
        assert any(f["type"] == "github_token" for f in findings)

    def test_pypi_token(self):
        token = "pypi-" + "a" * 60
        findings = scan_text(token)
        assert any(f["type"] == "pypi_token" for f in findings)

    def test_npm_token(self):
        token = "npm_" + "a" * 36
        findings = scan_text(token)
        assert any(f["type"] == "npm_token" for f in findings)

    def test_aws_key(self):
        key = "AKIAIOSFODNN7EXAMPLE"
        findings = scan_text(key)
        assert any(f["type"] == "aws_key" for f in findings)

    def test_aws_secret(self):
        text = "aws_secret_access_key = wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"
        findings = scan_text(text)
        assert any(f["type"] == "aws_secret" for f in findings)

    def test_slack_token(self):
        token = "xoxb-" + "1234567890-" * 3 + "abcdef"
        findings = scan_text(token)
        assert any(f["type"] == "slack_token" for f in findings)

    def test_discord_webhook(self):
        url = "https://discord.com/api/webhooks/1234567890/abcdefghijklmnopqrstuvwxyz1234"
        findings = scan_text(url)
        assert any(f["type"] == "discord_webhook" for f in findings)

    def test_private_key(self):
        key = "-----BEGIN RSA PRIVATE KEY-----\nMIIEpAIB...\n-----END RSA PRIVATE KEY-----"
        findings = scan_text(key)
        assert any(f["type"] == "private_key" for f in findings)

    def test_cli_token_flag(self):
        text = "mycli --token abcdefghijklmnop"
        findings = scan_text(text)
        assert any(f["type"] == "cli_token_flag" for f in findings)

    def test_env_secret(self):
        text = 'SECRET="my_very_secret_value_here"'
        findings = scan_text(text)
        assert any(f["type"] == "env_secret" for f in findings)

    def test_generic_secret(self):
        text = 'api_key = "aB3xZ9qR2mK7pL4wN8yJ5tF"'
        findings = scan_text(text)
        assert any(f["type"] == "generic_secret" for f in findings)

    def test_bearer_token(self):
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        text = f"Authorization: Bearer {jwt}"
        findings = scan_text(text)
        assert any(f["type"] in ("bearer", "jwt") for f in findings)

    def test_ip_address(self):
        text = "Server at 203.0.113.42 is down"
        findings = scan_text(text)
        assert any(f["type"] == "ip_address" for f in findings)

    def test_url_token(self):
        text = "https://api.example.com?key=aB3xZ9qR2mK7"
        findings = scan_text(text)
        assert any(f["type"] == "url_token" for f in findings)

    def test_email(self):
        text = "Contact support@company.com for help"
        findings = scan_text(text)
        assert any(f["type"] == "email" for f in findings)

    def test_high_entropy_string(self):
        # Quoted string with high entropy, mixed chars, no dots, >= 40 chars
        s = "aB3xZ9qR2mK7pL4wN8yJ5tF1hG6cD0eW2vU8iOkX"
        assert len(s) >= 40
        assert _has_mixed_char_types(s)
        assert _shannon_entropy(s) >= 3.5
        assert s.count(".") <= 2
        text = f'key = "{s}"'
        findings = scan_text(text)
        assert any(f["type"] == "high_entropy" for f in findings)


# --- Allowlist ---


class TestAllowlist:
    def test_noreply_email(self):
        text = "From noreply@example.com"
        findings = scan_text(text)
        # noreply@ should be allowlisted
        assert not any(f["type"] == "email" and "noreply" in f["match"] for f in findings)

    def test_example_com_email(self):
        text = "user@example.com"
        findings = scan_text(text)
        assert not any(f["type"] == "email" and "example.com" in f["match"] for f in findings)

    def test_private_ip_192(self):
        text = "Host is at 192.168.1.100"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_private_ip_10(self):
        text = "Host is at 10.0.0.1"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_private_ip_172(self):
        text = "Host is at 172.16.0.1"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_pytest_decorator(self):
        text = "@pytest.mark.parametrize"
        findings = scan_text(text)
        assert not any(f["type"] == "email" for f in findings)

    def test_example_db_url(self):
        text = "postgres://user:pass@localhost:5432/mydb"
        findings = scan_text(text)
        assert not any(f["type"] == "db_url" for f in findings)

    def test_example_db_url_username_password(self):
        text = "postgres://username:password@localhost:5432/mydb"
        findings = scan_text(text)
        assert not any(f["type"] == "db_url" for f in findings)

    def test_google_dns_allowlisted(self):
        text = "DNS: 8.8.8.8"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_cloudflare_dns_allowlisted(self):
        text = "DNS: 1.1.1.1"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_anthropic_email(self):
        text = "noreply@anthropic.com"
        findings = scan_text(text)
        assert not any(f["type"] == "email" and "anthropic.com" in f["match"] for f in findings)

    def test_app_decorator_not_email(self):
        text = "@app.route('/api')"
        findings = scan_text(text)
        assert not any(f["type"] == "email" for f in findings)


# --- redact_text ---


class TestRedactText:
    def test_no_secrets(self):
        text = "Hello world, no secrets here."
        result, count, _log = redact_text(text)
        assert result == text
        assert count == 0

    def test_empty_text(self):
        text, count, _log = redact_text("")
        assert text == ""
        assert count == 0

    def test_single_secret(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        text = f"My key is {key}"
        result, count, _log = redact_text(text)
        assert "[REDACTED_ANTHROPIC_KEY]" in result
        assert key not in result
        assert count == 1

    def test_multiple_secrets(self):
        text = "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz and email: user@company.com"
        result, count, _log = redact_text(text)
        assert count >= 2
        assert "sk-ant-" not in result
        assert "user@company.com" not in result

    def test_overlapping_matches(self):
        # JWT contains both jwt and jwt_partial patterns - dedup should handle
        jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIiwiaWF0IjoxNTE2MjM5MDIyfQ.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        result, count, _log = redact_text(jwt)
        assert jwt not in result
        assert count >= 1

    def test_none_text(self):
        result, count, log = redact_text(None)
        assert result is None
        assert count == 0
        assert log == []


# --- redact_custom_strings ---


class TestRedactCustomStrings:
    def test_empty_text(self):
        result, count = redact_custom_strings("", ["secret"])
        assert result == ""
        assert count == 0

    def test_empty_strings_list(self):
        result, count = redact_custom_strings("hello secret", [])
        assert result == "hello secret"
        assert count == 0

    def test_short_string_skipped(self):
        result, count = redact_custom_strings("ab cd", ["ab"])
        assert result == "ab cd"
        assert count == 0

    def test_word_boundary_matching(self):
        result, count = redact_custom_strings("my secret_domain.com is here", ["secret_domain.com"])
        assert "[REDACTED_CUSTOM]" in result
        assert count == 1

    def test_multiple_replacements(self):
        result, count = redact_custom_strings(
            "foo myname bar myname baz", ["myname"]
        )
        assert "myname" not in result
        assert count == 2

    def test_none_text(self):
        result, count = redact_custom_strings(None, ["secret"])
        assert result is None
        assert count == 0

    def test_none_strings(self):
        result, count = redact_custom_strings("hello", None)
        assert result == "hello"
        assert count == 0

    def test_3_char_string_no_word_boundary(self):
        # len(target) == 3, uses escaped (no word boundary)
        result, count = redact_custom_strings("fooabc bar abc", ["abc"])
        # With no word boundary for 3-char, should match in "fooabc" as escaped substring
        assert count >= 1

    def test_cjk_adjacent_target_redacted(self):
        # `\b` never fires between CJK and ASCII word chars (#195)
        result, count = redact_custom_strings("只用myuser这个目录", ["myuser"])
        assert result == "只用[REDACTED_CUSTOM]这个目录"
        assert count == 1

    def test_cjk_after_path_target_redacted(self):
        result, count = redact_custom_strings("帮我看看/data/myuser里面", ["myuser"])
        assert result == "帮我看看/data/[REDACTED_CUSTOM]里面"
        assert count == 1

    def test_ascii_embedded_target_still_untouched(self):
        result, count = redact_custom_strings(
            "run mymyuser and myuser2024 now", ["myuser"]
        )
        assert result == "run mymyuser and myuser2024 now"
        assert count == 0


# --- _apply_redaction_set ---


class TestApplyRedactionSetBoundaries:
    def test_cjk_adjacent_short_secret_replaced(self):
        # Short alnum secrets go through the boundary-matched branch; `\b`
        # would skip them next to CJK text (#195)
        text, count = _apply_redaction_set("在tok3nvalue后面", {"tok3nvalue": "[X]"})
        assert text == "在[X]后面"
        assert count == 1

    def test_ascii_embedded_short_secret_still_untouched(self):
        text, count = _apply_redaction_set("xtok3nvaluey", {"tok3nvalue": "[X]"})
        assert text == "xtok3nvaluey"
        assert count == 0


# --- redact_session ---


class TestRedactSession:
    def test_empty_messages(self):
        session = {"messages": []}
        result, count, _log = redact_session(session)
        assert result["messages"] == []
        assert count == 0

    def test_redacts_content(self):
        session = {
            "messages": [
                {"content": "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"},
            ]
        }
        result, count, _log = redact_session(session)
        assert "[REDACTED_ANTHROPIC_KEY]" in result["messages"][0]["content"]
        assert count >= 1

    def test_redacts_thinking(self):
        session = {
            "messages": [
                {"thinking": "The key is sk-ant-api03-abcdefghijklmnopqrstuvwxyz"},
            ]
        }
        result, count, _log = redact_session(session)
        assert "[REDACTED_ANTHROPIC_KEY]" in result["messages"][0]["thinking"]
        assert count >= 1

    def test_redacts_tool_use_input(self):
        session = {
            "messages": [
                {
                    "tool_uses": [
                        {"input": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"},
                    ]
                },
            ]
        }
        result, count, _log = redact_session(session)
        assert "[REDACTED_ANTHROPIC_KEY]" in result["messages"][0]["tool_uses"][0]["input"]
        assert count >= 1

    def test_redacts_secrets_in_tool_use_input_dict(self):
        session = {
            "messages": [
                {
                    "tool_uses": [
                        {
                            "input": {
                                "file_path": "test.py",
                                "old_string": "key = 'sk-ant-api03-abcdefghijklmnopqrstuvwxyz'",
                                "new_string": "key = os.environ['API_KEY']",
                            },
                        }
                    ],
                },
            ]
        }
        result, count, _log = redact_session(session)
        inp = result["messages"][0]["tool_uses"][0]["input"]
        assert "sk-ant-" not in inp["old_string"]
        assert "[REDACTED_ANTHROPIC_KEY]" in inp["old_string"]
        assert count >= 1

    def test_custom_strings_redacted(self):
        session = {
            "messages": [
                {"content": "My company is Acme Corp and we use Acme Corp tools"},
            ]
        }
        result, count, _log = redact_session(session, custom_strings=["Acme Corp"])
        assert "Acme Corp" not in result["messages"][0]["content"]
        assert count >= 1

    def test_no_content_fields_skipped(self):
        session = {
            "messages": [
                {"role": "user"},  # no content, thinking, or tool_uses
            ]
        }
        result, count, _log = redact_session(session)
        assert count == 0

    def test_none_content_skipped(self):
        session = {
            "messages": [
                {"content": None, "thinking": None},
            ]
        }
        result, count, _log = redact_session(session)
        assert count == 0


# --- Confidence ---


class TestConfidence:
    def test_scan_text_includes_confidence(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        findings = scan_text(key)
        assert len(findings) >= 1
        assert "confidence" in findings[0]
        assert isinstance(findings[0]["confidence"], float)

    def test_high_confidence_patterns(self):
        """Known prefixed patterns should have confidence >= 0.90."""
        cases = [
            ("sk-ant-api03-abcdefghijklmnopqrstuvwxyz", "anthropic_key"),
            ("ghp_" + "a" * 36, "github_token"),
            ("hf_" + "a" * 30, "hf_token"),
        ]
        for text, expected_type in cases:
            findings = scan_text(text)
            match = next(f for f in findings if f["type"] == expected_type)
            assert match["confidence"] >= 0.90, f"{expected_type} should be high confidence"

    def test_medium_confidence_patterns(self):
        """Structural patterns should have 0.70 <= confidence < 0.90."""
        text = "postgres://myuser:s3cretP4ss@db.example.com:5432/mydb"
        findings = scan_text(text)
        match = next(f for f in findings if f["type"] == "db_url")
        assert 0.70 <= match["confidence"] < 0.90

    def test_low_confidence_patterns(self):
        """Heuristic patterns should have confidence < 0.70."""
        text = "Server at 203.0.113.42 is down"
        findings = scan_text(text)
        match = next(f for f in findings if f["type"] == "ip_address")
        assert match["confidence"] < 0.70

    def test_all_pattern_types_have_confidence(self):
        """Every pattern type in CONFIDENCE map should be a float in [0, 1]."""
        for ptype, conf in CONFIDENCE.items():
            assert 0.0 <= conf <= 1.0, f"{ptype} confidence {conf} out of range"

    def test_every_secret_pattern_has_confidence(self):
        """Every pattern in SECRET_PATTERNS must have an entry in CONFIDENCE."""
        for name, _pattern in SECRET_PATTERNS:
            assert name in CONFIDENCE, (
                f"Pattern '{name}' in SECRET_PATTERNS has no CONFIDENCE entry — "
                f"it would silently fall back to 0.5"
            )

    def test_redact_text_returns_log(self):
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        text = f"My key is {key}"
        _result, count, log = redact_text(text)
        assert count == 1
        assert len(log) == 1
        assert log[0]["type"] == "anthropic_key"
        assert log[0]["confidence"] == 0.98
        assert log[0]["original_length"] == len(key)

    def test_log_does_not_contain_match_text(self):
        """Log entries must not leak the actual secret."""
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        _result, _count, log = redact_text(key)
        for entry in log:
            assert "match" not in entry
            for v in entry.values():
                if isinstance(v, str):
                    assert key not in v

    def test_log_context_for_low_confidence(self):
        """Low-confidence findings should include context_before/context_after."""
        text = "Server at 203.0.113.42 is down"
        _result, _count, log = redact_text(text)
        ip_entry = next(e for e in log if e["type"] == "ip_address")
        assert "context_before" in ip_entry
        assert "context_after" in ip_entry

    def test_no_context_for_high_confidence(self):
        """High-confidence findings should NOT include context."""
        key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
        _result, _count, log = redact_text(key)
        entry = log[0]
        assert "context_before" not in entry
        assert "context_after" not in entry

    def test_redact_session_log_has_field(self):
        session = {
            "messages": [
                {"content": "Key: sk-ant-api03-abcdefghijklmnopqrstuvwxyz"},
            ]
        }
        _result, _count, log = redact_session(session)
        assert len(log) >= 1
        assert log[0]["field"] == "content"
        assert log[0]["message_index"] == 0

    def test_redact_session_log_tool_input(self):
        session = {
            "messages": [
                {
                    "tool_uses": [
                        {"input": "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"},
                    ]
                },
            ]
        }
        _result, _count, log = redact_session(session)
        assert any(e["field"] == "tool_input" for e in log)


# --- User Allowlist ---


class TestUserAllowlist:
    def test_exact_match_skips_finding(self):
        text = "Server at 203.0.113.42 is down"
        allowlist = [{"type": "exact", "text": "203.0.113.42"}]
        findings = scan_text(text, user_allowlist=allowlist)
        assert not any(f["match"] == "203.0.113.42" for f in findings)

    def test_exact_match_no_effect_on_other(self):
        text = "IPs: 203.0.113.42 and 198.51.100.1"
        allowlist = [{"type": "exact", "text": "203.0.113.42"}]
        findings = scan_text(text, user_allowlist=allowlist)
        assert any(f["match"] == "198.51.100.1" for f in findings)

    def test_pattern_match_skips(self):
        text = "Server at 203.0.113.42 is down"
        allowlist = [{"type": "pattern", "regex": r"203\.0\.113\.\d+"}]
        findings = scan_text(text, user_allowlist=allowlist)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_category_match_skips(self):
        text = "Server at 203.0.113.42 is down"
        allowlist = [{"type": "category", "match_type": "ip_address"}]
        findings = scan_text(text, user_allowlist=allowlist)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_allowlist_propagated_through_redact_session(self):
        session = {
            "messages": [
                {"content": "Server at 203.0.113.42 is down"},
            ]
        }
        allowlist = [{"type": "exact", "text": "203.0.113.42"}]
        result, count, log = redact_session(session, user_allowlist=allowlist)
        # The IP should NOT be redacted
        assert "203.0.113.42" in result["messages"][0]["content"]

    def test_allowlist_precedence_over_pattern(self):
        """Allowlist should prevent redaction even when pattern matches."""
        email = "important@partner.com"
        text = f"Contact {email} for details"
        allowlist = [{"type": "exact", "text": email}]
        findings = scan_text(text, user_allowlist=allowlist)
        assert not any(f["match"] == email for f in findings)

    def test_check_user_allowlist_empty(self):
        assert _check_user_allowlist("test", "email", None) is False
        assert _check_user_allowlist("test", "email", []) is False

    def test_invalid_regex_does_not_crash(self):
        """A malformed regex in user config should not crash the scan."""
        text = "Server at 203.0.113.42 is down"
        allowlist = [{"type": "pattern", "regex": "[unclosed"}]
        # Should not raise — the invalid entry is silently skipped
        findings = scan_text(text, user_allowlist=allowlist)
        assert any(f["type"] == "ip_address" for f in findings)


# --- A2: Stripe key pattern ---


class TestStripeKey:
    """A2: new `stripe_key` pattern matches the four real Stripe key
    shapes (`sk_live_…`, `pk_live_…`, `rk_live_…`, plus the `_test_`
    variants). Confidence pinned to 0.98 — the prefix is unambiguous."""

    def test_secret_key_live(self):
        text = "stripe key is sk_live_" + "A" * 30 + " do not commit"
        result, count, _ = redact_text(text)
        assert count == 1
        assert "sk_live_" not in result
        assert "[REDACTED_STRIPE_KEY]" in result

    def test_publishable_key_live(self):
        text = "config has pk_live_" + "B" * 28
        findings = scan_text(text)
        types = {f["type"] for f in findings}
        assert "stripe_key" in types

    def test_restricted_key_test(self):
        text = "rk_test_" + "9" * 24
        findings = scan_text(text)
        assert any(f["type"] == "stripe_key" for f in findings)

    def test_secret_key_test_variant(self):
        # Round-2: cover all 6 (sk|pk|rk) × (live|test) combinations.
        text = "sk_test_" + "Q" * 24
        findings = scan_text(text)
        assert any(f["type"] == "stripe_key" for f in findings)

    def test_publishable_key_test_variant(self):
        text = "pk_test_" + "R" * 24
        findings = scan_text(text)
        assert any(f["type"] == "stripe_key" for f in findings)

    def test_restricted_key_live_variant(self):
        text = "rk_live_" + "S" * 24
        findings = scan_text(text)
        assert any(f["type"] == "stripe_key" for f in findings)

    def test_short_tail_does_not_match(self):
        # 23 chars after the underscore — below the 24-char minimum.
        text = "sk_live_" + "X" * 23
        findings = scan_text(text)
        assert not any(f["type"] == "stripe_key" for f in findings)

    def test_wrong_prefix_does_not_match(self):
        # `sklive_…` (no underscore between sk and live) is not a Stripe
        # key shape.
        text = "sklive_xxxxxxxxxxxxxxxxxxxxxxxxx"
        findings = scan_text(text)
        assert not any(f["type"] == "stripe_key" for f in findings)

    def test_embedded_in_identifier_does_not_match(self):
        """Round-1 self-review: pin the `\\b` boundary at the start of
        the regex. `mysk_live_…` should NOT match because `\\b` requires
        a non-word→word transition before `s`, and `y` (in `my`) is a
        word char. Without the boundary, the regex would over-match and
        flag any identifier that happens to contain a Stripe-like
        suffix."""

        text = "let mysk_live_AAAAAAAAAAAAAAAAAAAAAAAAA = 1"
        findings = scan_text(text)
        assert not any(f["type"] == "stripe_key" for f in findings), (
            "Stripe regex should not match into a longer identifier"
        )

    def test_key_followed_by_underscore_word_still_matches(self):
        """Round-3: trailing `\\b` was the original terminator; it
        failed when the key was abutted by `_word` because `_` is a
        word char. Real text like an f-string `{sk_live_xxx}_id` would
        leak the secret. Replaced with `(?![A-Za-z0-9])` which
        terminates on `_` and other non-alphanumeric chars."""

        text = "let key = 'sk_live_" + "A" * 24 + "_metadata' # leak"
        findings = scan_text(text)
        assert any(f["type"] == "stripe_key" for f in findings), (
            f"Stripe key followed by _word should still match; "
            f"got {[f['type'] for f in findings]}"
        )

    def test_confidence_and_placeholder_registered(self):
        assert CONFIDENCE["stripe_key"] == 0.98
        assert SECRET_PLACEHOLDER["stripe_key"] == "[REDACTED_STRIPE_KEY]"


class TestStripeWebhookSecret:
    """Round-3: webhook signing secrets (`whsec_…`) are functionally
    distinct from API keys (used for signature verification, not auth)
    so they get their own pattern + placeholder."""

    def test_basic_match(self):
        text = "STRIPE_WEBHOOK_SECRET=whsec_" + "A" * 32
        findings = scan_text(text)
        assert any(f["type"] == "stripe_webhook_secret" for f in findings)

    def test_short_tail_does_not_match(self):
        text = "whsec_" + "B" * 23  # below 24-char minimum
        findings = scan_text(text)
        assert not any(
            f["type"] == "stripe_webhook_secret" for f in findings
        )

    def test_terminator_allows_underscore_continuation(self):
        text = "secret = whsec_" + "C" * 24 + "_label"
        findings = scan_text(text)
        assert any(f["type"] == "stripe_webhook_secret" for f in findings)

    def test_embedded_in_identifier_does_not_match(self):
        text = "mywhsec_" + "D" * 30
        findings = scan_text(text)
        assert not any(
            f["type"] == "stripe_webhook_secret" for f in findings
        )

    def test_confidence_and_placeholder_registered(self):
        assert CONFIDENCE["stripe_webhook_secret"] == 0.98
        assert (
            SECRET_PLACEHOLDER["stripe_webhook_secret"]
            == "[REDACTED_STRIPE_WEBHOOK_SECRET]"
        )


# --- A2: Bearer bound + generic bearer ---


class TestBearerBound:
    """A2: the JWT-shaped bearer regex now bounds each segment at
    {20,2048} (was {20,}); a separate `bearer_generic` pattern catches
    non-JWT-shaped bearers. The bound prevents polynomial backtracking
    on adversarial input."""

    def test_jwt_shaped_bearer_still_redacts(self):
        # Realistic JWT-shaped bearer; each segment well under 2048.
        # Either the `bearer` pattern OR the `jwt` pattern can match
        # (the inner JWT pattern wins via dedup ordering — both
        # outcomes are security-equivalent: the secret is gone).
        bearer = (
            "Bearer eyJ" + "A" * 30 + "." + "B" * 30 + "." + "C" * 30
        )
        result, count, _ = redact_text(bearer)
        assert count >= 1
        assert "A" * 30 not in result
        assert "[REDACTED_" in result

    def test_generic_bearer_redacts(self):
        # Opaque OAuth-style bearer — no JWT shape, JWT-only regex
        # would have missed this.
        bearer = "Authorization: Bearer " + "Z" * 40
        findings = scan_text(bearer)
        assert any(f["type"] == "bearer_generic" for f in findings)

    def test_bound_prevents_pathological_runtime(self):
        """Adversarial 8 KiB string mostly matching the JWT shape but
        terminated wrong. Without the {20,2048} bound, the unbounded
        {20,} runs amplified backtracking on each retry. With the bound,
        scan_text returns in well under a second."""

        import time
        # 8 KiB of valid bearer-tail chars, no terminating dot/segment —
        # forces the regex to attempt many partial matches.
        adversarial = "Bearer eyJ" + ("A" * 8000)
        started = time.perf_counter()
        scan_text(adversarial)
        elapsed = time.perf_counter() - started
        assert elapsed < 1.0, (
            f"scan_text took {elapsed:.3f}s on 8 KiB adversarial bearer "
            f"input; bound regression"
        )

    def test_generic_bearer_confidence_lower_than_jwt(self):
        # Generic bearers have higher FP risk; pinned at 0.75 vs JWT-
        # shaped's 0.85.
        assert CONFIDENCE["bearer_generic"] == 0.75
        assert CONFIDENCE["bearer"] == 0.85

    def test_uppercase_bearer_classifies_as_jwt_bearer(self):
        """Round-2: existing `bearer` regex was case-sensitive, so an
        upstream `Authorization: BEARER eyJ…` (uppercase) would fall
        through to `bearer_generic` at 0.75 instead of `bearer` at
        0.85. The `(?i:Bearer)` group accepts every case while keeping
        the body matching strict."""

        bearer = "BEARER eyJ" + "A" * 30 + "." + "B" * 30 + "." + "C" * 30
        findings = scan_text(bearer)
        types = {f["type"] for f in findings}
        assert "bearer" in types, (
            f"uppercase BEARER should match the JWT-shaped bearer regex; "
            f"got types={sorted(types)}"
        )

    def test_lowercase_bearer_also_matches(self):
        bearer = "bearer eyJ" + "A" * 30 + "." + "B" * 30 + "." + "C" * 30
        findings = scan_text(bearer)
        types = {f["type"] for f in findings}
        assert "bearer" in types

    def test_bearer_does_not_split_identifier(self):
        """Round-2: `\\b` prevents the regex from carving `Bearer xxx`
        out of an identifier-shaped span like `myBearer xxx`. Without
        the boundary the regex matched at offset 2 and left `my` in
        the text — not a privacy leak (the secret was still redacted),
        but a classification quality bug."""

        # Build a generic-bearer-shaped span (40 chars, no JWT shape).
        # Embedded inside an identifier `myBearer …`.
        text = "myBearer " + "Z" * 40
        findings = scan_text(text)
        # Neither bearer pattern should fire — the `Bearer` keyword sits
        # mid-identifier and the leading `\b` blocks the match.
        bearer_findings = [
            f for f in findings if f["type"] in ("bearer", "bearer_generic")
        ]
        assert bearer_findings == [], (
            f"\\b should block bearer-pattern match inside identifier; "
            f"got {bearer_findings}"
        )

    def test_jwt_shaped_bearer_fires_both_patterns_at_scan_level(self):
        """Round-1 self-review: the JWT-shaped bearer pattern AND the
        generic bearer pattern are strict supersets at the regex level
        (both match `Bearer <token>` shapes). At scan_text the user sees
        both findings; redact_text's dedup loop resolves to one
        replacement. Pin both behaviors so a future registry reorder
        can't silently drop one."""

        bearer = "Bearer eyJ" + "A" * 30 + "." + "B" * 30 + "." + "C" * 30
        findings = scan_text(bearer)
        types = {f["type"] for f in findings}
        # The inner JWT pattern also matches (eyJ shape inside the
        # bearer span). All three of bearer / bearer_generic / jwt fire
        # as candidate findings; dedup picks one for replacement.
        assert "bearer" in types
        assert "bearer_generic" in types

        # Exactly one redaction lands end-to-end.
        result, count, _ = redact_text(bearer)
        assert count == 1
        assert "A" * 30 not in result and "B" * 30 not in result


# --- A2: IP version-context guard ---


class TestIpVersionGuard:
    """A2: ip_address matches that are more plausibly version strings
    (preceded by 'version'/'commit'/etc, or part of a longer dotted-
    numeric run) are suppressed. Real public IPs without that context
    still redact."""

    def test_real_public_ip_still_redacts(self):
        # RFC5737 documentation prefix — not in ALLOWLIST, no version
        # context. Should still fire.
        text = "Server is at 203.0.113.5 — please update"
        findings = scan_text(text)
        assert any(
            f["type"] == "ip_address" and f["match"] == "203.0.113.5"
            for f in findings
        )

    def test_version_prefix_suppresses(self):
        text = "Bumped to version 1.2.3.4 last night"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings), (
            f"version-prefixed IP should be suppressed; "
            f"got {[f for f in findings if f['type'] == 'ip_address']}"
        )

    def test_release_prefix_suppresses(self):
        text = "release 2.0.1.4 ships tomorrow"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_commit_prefix_suppresses(self):
        text = "commit 198.51.100.7 introduced the regression"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_v_prefix_suppresses(self):
        text = "Tag v1.2.3.4 published"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_dotted_run_suppresses_both_slices(self):
        """In `1.2.3.4.5` the regex matches both `1.2.3.4` and `2.3.4.5`;
        both should be suppressed because the surrounding shape is a
        5-segment version, not two adjacent IPs."""

        text = "Build 1.2.3.4.5 deployed"
        findings = scan_text(text)
        ip_findings = [f for f in findings if f["type"] == "ip_address"]
        assert ip_findings == [], (
            f"dotted-run slices should both be suppressed; got {ip_findings}"
        )

    def test_known_public_dns_still_allowlisted(self):
        # ALLOWLIST has 8.8.8.8, 1.1.1.1 etc as not-secret. Verify the
        # version guard doesn't break that path either way (it should be
        # suppressed by ALLOWLIST first).
        text = "DNS is 8.8.8.8"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_redact_text_end_to_end_for_version(self):
        text = "version 1.2.3.4 and a real IP at 203.0.113.5"
        result, count, _ = redact_text(text)
        # Only the real IP should redact.
        assert "1.2.3.4" in result
        assert "203.0.113.5" not in result
        assert "[REDACTED_IP]" in result

    def test_short_keyword_with_separator_still_suppresses(self):
        """`tag 100.200.50.99` — `tag` precedes a real IP with a space
        separator. The version guard fires and suppresses (we can't
        tell a build tag from a real IP after a bare `tag`)."""

        text = "tag 100.200.50.99"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_v_prefix_with_separator_suppresses(self):
        text = "v 1.2.3.4"
        findings = scan_text(text)
        assert not any(f["type"] == "ip_address" for f in findings)

    def test_short_keyword_no_separator_unreachable_by_design(self):
        """Round-3 self-review: an over-fire concern was raised about
        `tag100.200.50.99` (no space between keyword and IP) silently
        suppressing a real IP. Empirically, the IP regex's own leading
        `\\b` doesn't fire when the previous char is a word character
        (the last char of the keyword). So the IP regex never matches
        in this position, the guard never runs, and the over-fire is
        unreachable. Pin this so a future widening of the IP regex's
        boundary policy makes the test fail loudly and forces a guard
        update."""

        text = "tag100.200.50.99"  # contiguous, no separator
        findings = scan_text(text)
        ip_findings = [f for f in findings if f["type"] == "ip_address"]
        assert ip_findings == [], (
            f"IP regex should not fire when preceded by a word char; "
            f"got {ip_findings}. If this fails, the guard regex needs "
            f"the round-3 separator-required tightening."
        )

    def test_ip_at_exact_end_of_string(self):
        """Round-3: pin the head-guard's index-bounds check. The match
        end might equal len(text); the guard must not IndexError."""

        text = "Server is at 203.0.113.5"  # IP at exact EOS
        findings = scan_text(text)
        assert any(
            f["type"] == "ip_address" and f["match"] == "203.0.113.5"
            for f in findings
        )

    def test_ip_in_cidr_notation_redacts(self):
        """Round-3: CIDR-suffixed IP (`/24`) should still redact — the
        `/` is non-word and non-digit, so neither guard heuristic
        suppresses. Pinning so a future tweak doesn't break netblock
        redaction."""

        text = "block 203.0.113.0/24 from outside"
        findings = scan_text(text)
        assert any(
            f["type"] == "ip_address" and f["match"] == "203.0.113.0"
            for f in findings
        )


class TestContextLeakFix:
    """Round-4: a high-conf secret near a medium-conf finding used to
    leak its truncated tail into the medium-conf entry's
    `context_before`/`context_after` log fields. The regex-based
    `_redact_high_confidence_only` couldn't re-match a tail because
    the prefix was clipped from the 40-char window. The fix
    (`_blank_high_conf_overlaps`) walks the full findings list and
    blanks any byte that overlaps a high-conf span in source
    coordinates, then runs the regex pass for any high-conf secret
    that fully fits inside the window."""

    def test_stripe_key_does_not_leak_into_neighbor_context(self):
        # Construct an input where:
        # - a stripe_key (high-conf, 0.98) sits at the start
        # - an email (medium-conf, 0.65) sits ~20 chars later
        # - the email's context_before captures the LAST 40 chars of
        #   text — which includes the tail of the stripe key. Without
        #   the fix, the truncated stripe key tail leaks as plaintext
        #   into the email's context_before.
        stripe = "sk_live_" + "A" * 40  # 48 chars total
        email = "alice@example.org"
        # email's context_before window = 40 chars before email's start.
        # If stripe sits at offset 0, email at offset 49 (one space),
        # email's context_before is positions 9-49 = `A`*39 + ` `.
        # The stripe key prefix `sk_live_` is at positions 0-7,
        # outside the 40-char window — so the regex sees only `A*39`
        # (no `sk_live_` prefix → no match → leak without the fix).
        text = f"{stripe} {email}"
        _, _, log = redact_text(text)
        email_entry = next(
            (e for e in log if e["type"] == "email"),
            None,
        )
        assert email_entry is not None, f"email entry not in log; log={log}"
        ctx = email_entry.get("context_before", "")
        assert "A" * 30 not in ctx, (
            f"stripe-key tail leaked into email's context_before: "
            f"ctx_before={ctx!r}"
        )
        # The fix should have replaced the overlap with the placeholder.
        assert "[REDACTED_STRIPE_KEY]" in ctx, (
            f"high-conf overlap should be placeholder-blanked; "
            f"ctx_before={ctx!r}"
        )

    def test_anthropic_key_does_not_leak_into_ip_context(self):
        # Anthropic key (high-conf 0.98) followed by a medium-conf
        # ip_address. Same shape as the stripe case.
        anthropic = "sk-ant-api03-" + "A" * 60
        ip = "203.0.113.5"
        text = f"{anthropic} ip={ip}"
        _, _, log = redact_text(text)
        ip_entry = next(
            (e for e in log if e["type"] == "ip_address"),
            None,
        )
        assert ip_entry is not None
        ctx = ip_entry.get("context_before", "")
        assert "sk-ant-api03" not in ctx
        assert "A" * 30 not in ctx

    def test_short_high_conf_secret_in_window_still_regex_redacted(self):
        """When a high-conf secret fits ENTIRELY inside the 40-char
        context window, the existing regex pass redacts it. The new
        span-blank pass is a no-op (no overlap because the high-conf
        finding's span is wholly inside the window). Both paths must
        cooperate, not double-redact or fight."""

        # Tiny stripe key (24-char tail) fits in 32-char window.
        stripe = "sk_live_" + "A" * 24
        email = "bob@example.org"
        text = f"start {stripe} {email}"
        _, _, log = redact_text(text)
        email_entry = next(
            (e for e in log if e["type"] == "email"),
            None,
        )
        assert email_entry is not None
        ctx = email_entry.get("context_before", "")
        # Either path is acceptable as long as the secret is gone.
        assert "sk_live_" not in ctx or "[REDACTED_STRIPE_KEY]" in ctx
        assert "A" * 24 not in ctx
