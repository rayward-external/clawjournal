"""Check retained text as well as removed text in the built-in apply path.

Compatibility with the old regexes does not establish correct redaction.
Check both preservation and fail-closed handling of ambiguous input.
External detectors are isolated here, so these are not upload-gate tests.
"""
import hashlib
import itertools
import random
import re
import sqlite3
import subprocess
import sys

import pytest

from clawjournal.findings import apply_findings_to_text, apply_findings_to_session
from clawjournal.redaction import pii, secrets


EMAIL = "alice@redaction-audit.test"
TOKEN_BODY = "AbCdEf0123456789_-" * 2
TOKEN = "123456789:" + TOKEN_BODY
KEY = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_BODY\n-----END PRIVATE KEY-----"


@pytest.fixture
def builtin_conn(monkeypatch):
    # Test the production replacement function, including its merged maps
    # and repeated passes. No external scanners, live validation or uploads.
    monkeypatch.setattr(
        "clawjournal.redaction.betterleaks.betterleaks_secret_map_from_blob",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "clawjournal.redaction.trufflehog.trufflehog_secret_map_from_blob",
        lambda *_args, **_kwargs: {},
    )
    for module in (pii, secrets):
        monkeypatch.setattr(module, "hash_entity", lambda text: hashlib.sha256(text.encode()).hexdigest())
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE findings (session_id TEXT, entity_hash TEXT, status TEXT)")
    yield conn
    conn.close()


@pytest.fixture
def render_builtin(builtin_conn):
    def render(text):
        blob, _ = secrets.apply_findings_to_blob(
            {"messages": [{"content": text}]}, builtin_conn, "synthetic-boundary-audit",
        )
        return blob["messages"][0]["content"]

    return render


@pytest.mark.parametrize("before,after", [
    ("请联系：", "；谢谢"), ("前🙂", "🙂后"),
    ("Contact<", ">Thanks"), ("Contact(", ")Thanks"),
    ("Contact[", "]Thanks"), ('email="', '";done'),
    ("Contact:", ";Thanks"), ("前，", "，后"),
    ("First\n", "\nLast"), ("First\t", "\tLast"),
])
def test_email_without_spaces_preserves_real_delimiters(render_builtin, before, after):
    text = before + EMAIL + after
    matches = [m for m in pii.scan_text_for_pii(text) if m["type"] == "email"]
    assert [(m["match"], m["start"], m["end"]) for m in matches] == [
        (EMAIL, len(before), len(before) + len(EMAIL)),
    ]
    assert render_builtin(text) == before + "[REDACTED_EMAIL]" + after


def test_long_text_around_delimited_email_is_retained(render_builtin):
    # Assert the complete result, not just absence of the address. A blanket
    # delete would satisfy a check that only searched for remaining secrets.
    before, after = "A" * 200_000 + "<", ">" + "B" * 200_000
    assert render_builtin(before + EMAIL + after) == before + "[REDACTED_EMAIL]" + after


@pytest.mark.parametrize("sensitive,replacement", [
    (TOKEN, "[REDACTED]"),
    ("db01.local", "[REDACTED_URL]"),
    (KEY, "[REDACTED_PRIVATE_KEY]"),
])
@pytest.mark.parametrize("before,after", [("before<", ">after"), ("前：", "；后")])
def test_other_candidates_preserve_surrounding_text(render_builtin, sensitive, replacement, before, after):
    assert render_builtin(before + sensitive + after) == before + replacement + after


@pytest.mark.parametrize("text", [
    "Ordinary text with no address.",
    "这是一段普通内容，不应删除。",
    "The words local, internal, corp, lan and intranet are ordinary.",
    "local_variable = 12345678",
    "public.locality and public.internalization",
    "a.b.c and 12345678 are ordinary values.",
    "BEGIN and END describe a range.",
    "-----BEGIN PUBLIC KEY-----\nSYNTHETIC_BODY\n-----END PUBLIC KEY-----",
    "[REDACTED_EMAIL] [REDACTED_PRIVATE_KEY]",
])
def test_ordinary_controls_remain_identical(render_builtin, text):
    assert render_builtin(text) == text


def test_current_and_previous_rules_produce_same_attached_email_output(render_builtin, monkeypatch):
    # This is an explicit compatibility check, not a claim that either result
    # is correct. With letters touching the address, neither matcher knows
    # which letters the author intended as surrounding prose.
    samples = [
        "Contact" + EMAIL + "Thanks", "请联系" + EMAIL + "谢谢",
        "Contact<" + EMAIL + ">Thanks",
    ]
    current = [render_builtin(text) for text in samples]
    with monkeypatch.context() as old:
        old.setattr(pii, "_content_matches", lambda pattern, text: pattern.finditer(text))
        old.setattr(secrets, "_secret_matches", lambda pattern, text: pattern.finditer(text))
        assert [render_builtin(text) for text in samples] == current


def test_legacy_review_removes_email_adjacent_to_chinese():
    text = "请联系" + EMAIL + "谢谢"
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert any(f["entity_text"] == EMAIL for f in findings)
    assert EMAIL not in apply_findings_to_text(text, findings)[0]


@pytest.mark.parametrize("text,expected", [
    pytest.param("x@redaction-audit.test", "[REDACTED_EMAIL]", id="one-character-local-part"),
    pytest.param("请联系ab@redaction-audit.test谢谢", "请联系[REDACTED_EMAIL]谢谢", id="two-characters-next-to-chinese"),
    pytest.param("o'connor@redaction-audit.test", "[REDACTED_EMAIL]", id="apostrophe-leaves-name-fragment"),
])
def test_email_coverage_gaps_remove_the_whole_address(render_builtin, text, expected):
    assert render_builtin(text) == expected


@pytest.mark.parametrize("token", [TOKEN.replace(":", "%3A"), TOKEN.replace(":", r"\u003a"), "123456:" + TOKEN_BODY])
def test_telegram_representation_gaps_remove_sensitive_value(render_builtin, token):
    # These are raw log strings; a literal escape is not an already-decoded
    # JSON value. The surrounding prose must survive replacement as well.
    assert render_builtin("before<" + token + ">after") == "before<[REDACTED]>after"


def test_known_internal_hostname_adjacent_to_chinese(render_builtin):
    assert render_builtin("连接db01.local完成") == "连接[REDACTED_URL]完成"


def test_markerless_private_key_keeps_previous_behavior(render_builtin):
    # Synthetic all-zero seed representation, never used for authentication.
    value = "A" * 43
    text = '{"kty":"OKP","crv":"Ed25519","d":"' + value + '"}'
    output = render_builtin(text)
    assert output == text  # Coverage expansion is deliberately outside this PR.


@pytest.mark.parametrize("text", [
    "import numpy\nimport torch\nresult = numpy.array@torch.tensor", "value = obj.local()",
])
def test_ordinary_code_is_not_redacted(render_builtin, text):
    text = "obj = object()\n" + text
    assert render_builtin(text) == text


def test_bare_matrix_expression_is_redacted_without_aborting_share(render_builtin):
    # Without specific array evidence the normal email rule still applies.
    assert render_builtin("result = numpy.array@torch.tensor") == "result = [REDACTED_EMAIL]"


def test_truncated_email_does_not_delete_other_ordinary_text(render_builtin):
    assert render_builtin("abc@  abc abcdef") == "[REDACTED_EMAIL]@  abc abcdef"


def test_named_telegram_fragment_preserves_its_field_label(render_builtin):
    assert render_builtin("TELEGRAM_BOT_TOKEN=" + TOKEN_BODY) == "TELEGRAM_BOT_TOKEN=[REDACTED_ENV_SECRET]"


# Representative formats adapted with synthetic values from:
# https://github.com/JoshData/python-email-validator/blob/main/tests/test_syntax.py
# https://scrubadub.readthedocs.io/en/stable/_modules/scrubadub/detectors/email.html
# https://github.com/gitleaks/gitleaks/blob/master/cmd/generate/config/rules/telegram.go
# https://github.com/validatorjs/validator.js/blob/master/src/lib/isFQDN.js
# https://github.com/trufflesecurity/trufflehog/issues/2507
# Detection tests deliberately differ from address validation: rejecting an
# invalid/oversized value is never permission to share that text unchanged.

@pytest.mark.parametrize("address", [
    "a@audit.test", "ab@audit.test", "o'connor@audit.test", "alice%tag@audit.test",
    "user+mailbox/department=shipping@audit.test", "UPPER@AUDIT.TEST",
    "one.two@sub.audit.test", "test@xn--bcher-kva.de", "test@audit.xn--p1ai",
    '"quoted local part"@audit.test', r'"escaped.\".name"@audit.test',
    '"name@inside"@audit.test', "user@[192.0.2.1]", "user@[IPv6:2001:db8::1]",
    "ñoñó@audit.test", "δοκιμή@audit.test", "用户@例子.公司", "user@例子.test",
    "s\u0323\u0307@audit.test", "a" * 64 + "@audit.test",
    "a@" + "b" * 63 + ".test",
])
def test_representative_email_formats_remove_full_value(render_builtin, address):
    text = "before<" + address + ">after"
    assert render_builtin(text) == "before<[REDACTED_EMAIL]>after"


@pytest.mark.parametrize("encoded", [
    "alice%40audit.test", r"alice\u0040audit.test", "alice&#64;audit.test",
    "alice&#x40;audit.test", "alice@audit%2Etest", r"alice@audit\u002etest",
    "alice%40audit%2Etest",
])
def test_encoded_email_offsets_still_refer_to_original_text(render_builtin, encoded):
    text = "前🙂<" + encoded + ">🙂后"
    assert render_builtin(text) == "前🙂<[REDACTED_EMAIL]>🙂后"
    for match in pii.scan_text_for_pii(text):
        assert text[match["start"]:match["end"]] == match["match"]


@pytest.mark.parametrize("bot_id", ["12345", "123456", "12345678", "1234567890123456", "١٢٣٤٥٦٧٨"])
@pytest.mark.parametrize("separator", [":", "%3A", "%3a", r"\u003a", r"\u003A", "&#58;", "&#x3a;"])
def test_telegram_lengths_and_encoded_separators(render_builtin, bot_id, separator):
    token = bot_id + separator + "A" + "bC0123456789_-aBcD" * 2
    assert render_builtin("before<" + token + ">after") == "before<[REDACTED]>after"


@pytest.mark.parametrize("label", ["TELEGRAM_BOT_TOKEN", "TELEGRAM_API_TOKEN", "telegram_token", "telegramBotToken"])
def test_named_telegram_fragment_without_colon_is_removed(render_builtin, label):
    output = render_builtin(label + '="' + TOKEN_BODY + '"; ordinary_tail')
    assert TOKEN_BODY not in output
    assert output.endswith("; ordinary_tail")


@pytest.mark.parametrize("host", [
    "db01.local", "db01.internal", "db01.corp", "db01.lan", "db01.intranet", "db01.localnet",
    "DB01.LOCAL", "a" * 63 + ".local", ".".join(["a" * 63] * 3 + ["b" * 55, "local"]),
])
def test_internal_domain_suffixes_and_legal_length_edges(render_builtin, host):
    assert render_builtin("before<" + host + ">after") == "before<[REDACTED_URL]>after"


@pytest.mark.parametrize("text,host", [
    ("DB_HOST=db01", "db01"), ("REDIS_HOST=cache.example.net", "cache.example.net"),
    ('{"PGHOST":"database.example.net"}', "database.example.net"),
    ("ssh db01", "db01"), ("ssh user@db01", "db01"),
])
def test_host_context_covers_names_without_private_suffixes(render_builtin, text, host):
    output = render_builtin(text)
    assert host not in output
    assert "[REDACTED_URL]" in output


@pytest.mark.parametrize("text", [
    "DB_HOST=localhost", "ssh localhost", "ordinary db01 reference",
    "https://www.example.net/docs", "value = matrix @ vector",
    'type="clm12345:AgencyIdentificationCodeContentType"',
    "prefix%3Aordinary suffix", "1234:" + TOKEN_BODY,
])
def test_additional_rules_keep_ordinary_controls(render_builtin, text):
    assert render_builtin(text) == text


@pytest.mark.parametrize("value", [
    "A" * 200_000 + EMAIL + "B" * 200_000,
    "A" * 200_000 + "@ ", '"' + "A" * 1000 + '"@audit.test',
    "a" * 65 + "@audit.test", "user@" + "b" * 64 + ".test",
    "12345678:" + "A" * 200_000, "1" * 200_000 + ":" + TOKEN_BODY,
    "12345678%3A" + "A" * 200_000, "TELEGRAM_BOT_TOKEN=" + "A" * 200_000,
    "a." * 100_000 + "local", "a" * 64 + ".local", "\u212a" * 32 + "-laptop",
    "DB_HOST=" + "a" * 200_000,
    ".".join(["a" * 63] * 3 + ["b" * 56, "local"]),
], ids=["attached-email", "truncated-email", "quoted-email", "local-65", "domain-label-64",
        "long-token-tail", "long-token-id", "escaped-token", "named-token",
        "many-host-labels", "host-label-64", "kelvin-personal-host", "host-field", "host-254"])
def test_oversized_candidates_are_deferred_without_mutation(render_builtin, value):
    from clawjournal.redaction.boundaries import RedactionBoundaryError

    with pytest.raises(RedactionBoundaryError) as caught:
        render_builtin(value)
    assert value not in str(caught.value)
    # Export rejects ambiguous input. Local ingest preserves it for review.
    with pytest.raises(RedactionBoundaryError):
        secrets.redact_text(value, strict=True)
    secrets.redact_text(value)


def test_blob_preflight_finishes_before_any_field_is_mutated(render_builtin, monkeypatch):
    import copy
    from clawjournal.redaction.boundaries import RedactionBoundaryError

    blob = {"messages": [{"content": EMAIL}, {"content": "A" * 1000 + EMAIL}]}
    original = copy.deepcopy(blob)
    with pytest.raises(RedactionBoundaryError):
        secrets.apply_findings_to_blob(blob, None, "synthetic")
    assert blob == original


def test_private_key_blocks_are_not_subject_to_email_or_host_limits(render_builtin):
    key = "-----BEGIN RSA PRIVATE KEY-----\n" + "SYNTHETIC" * 1000 + "\n-----END EC PRIVATE KEY-----"
    assert render_builtin("before<" + key + ">after") == "before<[REDACTED_PRIVATE_KEY]>after"


def test_mixed_decoded_and_escaped_candidates_preserve_positions(render_builtin):
    text = "start<alice%40audit.test> middle<" + TOKEN.replace(":", r"\u003a") + "> end<db01.local> done"
    assert render_builtin(text) == "start<[REDACTED_EMAIL]> middle<[REDACTED]> end<[REDACTED_URL]> done"


def test_no_reply_skip_does_not_allow_a_later_engine_to_remove_a_huge_run(render_builtin):
    from clawjournal.redaction.boundaries import RedactionBoundaryError

    with pytest.raises(RedactionBoundaryError):
        render_builtin("noreply@audit." + "a" * 1000)


def test_short_telegram_id_in_documented_bot_url(render_builtin):
    text = "https://api.telegram.org/bot12345:" + TOKEN_BODY + "/getMe"
    assert render_builtin(text) == "https://api.telegram.org/bot[REDACTED]/getMe"


@pytest.mark.parametrize("host", ["ALEX-LAPTOP", "Alexs-MacBook-Pro", "alex-laptop", "ALEX-PC",
                                  "richards-macbook-pro", "alice-desktop-01", "Ilgins-MacBook-Air"])
def test_personal_host_case_preserves_surrounding_text(render_builtin, host):
    assert render_builtin("前：" + host + "；后") == "前：[REDACTED_DEVICE_ID]；后"


def test_seeded_mixed_formats_keep_exact_ordinary_text(render_builtin):
    # Expected output is assembled from independent annotated pieces. Merely
    # comparing old/new regex matches would not detect collateral deletion.
    sensitive = [
        (EMAIL, "[REDACTED_EMAIL]"), ("x@audit.test", "[REDACTED_EMAIL]"),
        ("o'connor@audit.test", "[REDACTED_EMAIL]"),
        ('"a b"@audit.test', "[REDACTED_EMAIL]"),
        ("用户@例子.公司", "[REDACTED_EMAIL]"),
        ("alice&#64;audit.test", "[REDACTED_EMAIL]"),
        (TOKEN, "[REDACTED]"), (TOKEN.replace(":", "%3a"), "[REDACTED]"),
        ("db01.INTERNAL", "[REDACTED_URL]"), ("db01%2Elocal", "[REDACTED_URL]"),
        ("ALEX-LAPTOP", "[REDACTED_DEVICE_ID]"), (KEY, "[REDACTED_PRIVATE_KEY]"),
    ]
    wrappers = [("before<", ">after"), ("前：", "；后"), ("line\n", "\nend"), ("text(", ")done")]
    for seed in (225, 911, 2026):
        rng = random.Random(seed)
        for _ in range(100):
            actual, expected = [], []
            for _ in range(rng.randint(1, 5)):
                value, placeholder = rng.choice(sensitive)
                before, after = rng.choice(wrappers)
                actual.append(before + value + after)
                expected.append(before + placeholder + after)
            assert render_builtin(" | ".join(actual)) == " | ".join(expected)


def test_long_boundary_preflight_has_a_deadline():
    code = r'''
from clawjournal.redaction.boundaries import ensure_text_boundaries, RedactionBoundaryError
for text in ["A" * 200_000, r'\"@' * 30_000, "a." * 100_000]:
    ensure_text_boundaries(text)
for text in ["A" * 200_000 + "alice@audit.test", "12345678:" + "A" * 200_000, "a." * 100_000 + "local"]:
    try:
        ensure_text_boundaries(text)
    except RedactionBoundaryError:
        pass
    else:
        raise AssertionError("An ambiguous candidate was allowed")
'''
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10, capture_output=True, text=True)


# Occurrence-aware regressions: syntax evidence (Semgrep/detect-secrets),
# source-span replacement (Presidio), and value-only captures (Gitleaks).
@pytest.mark.parametrize("text", [
    "value = obj.local()", "value = obj.internal()", "obj.corp()",
    "value = obj.local().internal()", "值 = obj.local()",
    'value = obj.local("ordinary argument")',
    "```python\nvalue = obj.local()\n```",
    "import numpy as np\nimport torch as th\nresult = np.array@th.tensor",
    "```python\nimport numpy\nimport torch\nresult = numpy.array@torch.tensor\n```",
])
def test_code_occurrences_survive_builtins_and_review(render_builtin, text):
    text = text.replace("```python\n", "```python\nobj = object()\n") if text.startswith("```") else "obj = object()\n" + text
    assert render_builtin(text) == text
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert apply_findings_to_text(text, findings)[0] == text
    assert not [f for f in pii.scan_text_for_pii(text) if f["type"] in {"email", "private_url"}]
    assert not [f for f in secrets.scan_text(text) if f["type"] == "email"]


@pytest.mark.parametrize("text,expected", [
    ('value = obj.local("obj.local")', 'value = obj.local("[REDACTED_URL]")'),
    ('值 = obj.local("db01.local")', '值 = obj.local("[REDACTED_URL]")'),
    ('value = obj.local("alice@audit.test")', 'value = obj.local("[REDACTED_EMAIL]")'),
    ('obj.local()  # db01.local', 'obj.local()  # [REDACTED_URL]'),
    ('obj.local()  # alice@audit.test', 'obj.local()  # [REDACTED_EMAIL]'),
    ('value = obj["db01.local"].local()', 'value = obj["[REDACTED_URL]"].local()'),
    ('value = factory("db01.local").local()', 'value = factory("[REDACTED_URL]").local()'),
    ('value = f"{obj.local()} db01.local"', 'value = f"{obj.local()} [REDACTED_URL]"'),
    ('contact = "alice.smith@audit.test"', 'contact = "[REDACTED_EMAIL]"'),
    ('contact = alice@audit.test', 'contact = [REDACTED_EMAIL]'),
    ('DB_HOST=db01.local()', 'DB_HOST=[REDACTED_URL]()'),
    ('host = db01.local()', 'host = [REDACTED_URL]()'),
    ('before<one.two@sub.audit.test>after', 'before<[REDACTED_EMAIL]>after'),
])
def test_code_context_never_exempts_secrets_in_strings_comments_or_arguments(render_builtin, text, expected):
    text, expected = "obj = object()\n" + text, "obj = object()\n" + expected
    assert render_builtin(text) == expected


def test_same_email_in_code_and_literal_is_replaced_only_in_literal(render_builtin):
    text = 'import numpy\nimport torch\nresult = numpy.array@torch.tensor\ncontact = "numpy.array@torch.tensor"'
    expected = text.replace('"numpy.array@torch.tensor"', '"[REDACTED_EMAIL]"')
    assert render_builtin(text) == expected
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert apply_findings_to_text(text, findings)[0] == expected


def test_fenced_code_inside_a_string_is_still_sensitive_text(render_builtin):
    text = 'payload = """\n```python\nimport numpy\nimport torch\nresult = numpy.array@torch.tensor\nobj.local()\n```\n"""'
    expected = text.replace("numpy.array@torch.tensor", "[REDACTED_EMAIL]").replace("obj.local", "[REDACTED_URL]")
    assert render_builtin(text) == expected


@pytest.mark.parametrize("text", [
    "result = numpy.array@torch.tensor",
    "contact = alice.smith@example.com",
    "import numpy\nresult = numpy.array@torch.tensor",
    "result = numpy.array@torch.tensor\nimport numpy\nimport torch",
])
def test_ambiguous_matrix_or_email_uses_normal_redaction(render_builtin, text):
    result = render_builtin(text)
    assert 'numpy.array@torch.tensor' not in result
    assert 'alice.smith@example.com' not in result
    assert '[REDACTED_EMAIL]' in result


def test_multiline_matrix_comments_still_scan(render_builtin):
    text = "import numpy\nimport torch\nresult = (numpy.array@\n# alice@audit.test\ntorch.tensor)"
    output = render_builtin(text)
    assert "alice@audit.test" not in output
    assert "# [REDACTED_EMAIL]" in output


@pytest.mark.parametrize("text,expected", [
    ("abc@  abc abcdef", "[REDACTED_EMAIL]@  abc abcdef"),
    ("前🙂abc@\nabc", "前🙂[REDACTED_EMAIL]@\nabc"),
    ("abc@\tABC@\nabc ABC", "[REDACTED_EMAIL]@\t[REDACTED_EMAIL]@\nabc ABC"),
    ("abc@ abcdef@ abcdef abc", "[REDACTED_EMAIL]@ [REDACTED_EMAIL]@ abcdef abc"),
    ("abc@ abc@audit.test abc", "[REDACTED_EMAIL]@ [REDACTED_EMAIL] abc"),
])
def test_partial_email_replacements_keep_original_occurrence_boundaries(render_builtin, text, expected):
    assert render_builtin(text) == expected
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert apply_findings_to_text(text, findings)[0] == expected
    assert render_builtin(expected) == expected


def test_partial_email_does_not_spread_to_other_fields(builtin_conn):
    import json
    blob = {
        "display_title": "abc", "ai_learning_summary": "abc",
        "ai_scoring_detail": json.dumps({"reasoning": "abc", "summary": "abc@ abc"}),
        "messages": [{"content": "abc@ abc", "thinking": "abc", "author": "abc",
                      "tool_uses": [{"input": {"text": "abc@ abc"}, "output": ["abc"]}],
                      "extra": {"text": "abc@ abc"}}],
    }
    output, count = secrets.apply_findings_to_blob(blob, builtin_conn, "synthetic-boundary-audit")
    assert count == 4
    assert output["display_title"] == output["ai_learning_summary"] == "abc"
    assert json.loads(output["ai_scoring_detail"]) == {"reasoning": "abc", "summary": "[REDACTED_EMAIL]@ abc"}
    assert output["messages"][0] == {
        "content": "[REDACTED_EMAIL]@ abc", "thinking": "abc", "author": "abc",
        "tool_uses": [{"input": {"text": "[REDACTED_EMAIL]@ abc"}, "output": ["abc"]}],
        "extra": {"text": "[REDACTED_EMAIL]@ abc"},
    }


@pytest.mark.parametrize("text,expected", [
    ("abc@ abc", "abc@ abc"),
    ("abc@ ABC@ abc ABC", "abc@ [REDACTED_EMAIL]@ abc ABC"),
])
def test_ignored_partial_email_uses_existing_entity_hash(builtin_conn, render_builtin, text, expected):
    builtin_conn.execute("INSERT INTO findings VALUES (?, ?, ?)",
                         ("synthetic-boundary-audit", pii.hash_entity("abc"), "ignored"))
    assert render_builtin(text) == expected


@pytest.mark.parametrize("prefix,quote", [
    ("TELEGRAM_BOT_TOKEN=", ""), ("TELEGRAM_BOT_TOKEN = ", '"'),
    ("export TELEGRAM_BOT_TOKEN=", "'"), ("APP_PASSWORD=", '"'),
    ("MY_SECRET=", ""), ("secret_key: ", '"'),
])
@pytest.mark.parametrize("value", [TOKEN_BODY, TOKEN_BODY * 2])
def test_assignment_redactors_keep_label_separator_and_quotes(render_builtin, prefix, quote, value):
    text = prefix + quote + value + quote
    expected_prefix = prefix + quote
    for result in (
        render_builtin(text), secrets.redact_text(text)[0],
        secrets.redact_session({"messages": [{"content": text}]})[0]["messages"][0]["content"],
    ):
        assert result.startswith(expected_prefix)
        assert not quote or result.endswith(quote)
        assert value not in result
        assert "[REDACTED" in result


def test_captured_secret_still_redacts_unlabelled_copies_in_other_fields(builtin_conn):
    blob = {"display_title": TOKEN_BODY, "ai_learning_summary": TOKEN_BODY,
            "messages": [{"content": 'MY_SECRET="' + TOKEN_BODY + '"',
                          "tool_uses": [{"input": {"token": TOKEN_BODY}}]}]}
    output, _ = secrets.apply_findings_to_blob(blob, builtin_conn, "synthetic-boundary-audit")
    assert output["display_title"] == output["ai_learning_summary"] == "[REDACTED_ENV_SECRET]"
    assert output["messages"][0]["content"] == 'MY_SECRET="[REDACTED_ENV_SECRET]"'
    assert output["messages"][0]["tool_uses"][0]["input"]["token"] == "[REDACTED_ENV_SECRET]"


def test_assignment_ignore_keeps_legacy_full_match_hash(builtin_conn, render_builtin):
    text = "MY_SECRET=" + TOKEN_BODY
    match = next(f for f in secrets.scan_text(text) if f["type"] == "env_secret")
    assert match["match"] == "SECRET=" + TOKEN_BODY
    builtin_conn.execute("INSERT INTO findings VALUES (?, ?, ?)",
                         ("synthetic-boundary-audit", secrets.hash_entity(match["match"]), "ignored"))
    assert render_builtin(text + " " + TOKEN_BODY) == text + " " + TOKEN_BODY


@pytest.mark.parametrize("separator", ["\n", "\r\n", "\u2028", "\u2029", "\x85", "\v", "\f"])
def test_unicode_line_separators_do_not_shift_syntax_protection(render_builtin, separator):
    prefix = '"前' + separator + '后"; ' if separator not in {"\n", "\r\n"} else '# 前' + separator
    prefix = 'obj = object()\n' + prefix
    text = prefix + 'value = obj.local("obj.local")'
    assert render_builtin(text) == prefix + 'value = obj.local("[REDACTED_URL]")'


def test_review_merging_keeps_distinct_partial_email_occurrences():
    text = "abc@audit.test abc@ ABC@ abc ABC"
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    session = {"session_id": "synthetic", "messages": [{"content": text}]}
    result, _ = apply_findings_to_session(session, findings)
    assert result["messages"][0]["content"] == "[REDACTED_EMAIL] [REDACTED_EMAIL]@ [REDACTED_EMAIL]@ abc ABC"


@pytest.mark.parametrize("target", ["alice [at] audit [dot] test", "alice AT audit DOT test", "alice"])
def test_ai_email_finding_without_at_keeps_explicit_redaction(target):
    findings = [{"entity_text": target, "entity_type": "email", "source": "ai", "reason": "Explicit AI finding"}]
    assert apply_findings_to_text("before<" + target + ">after", findings)[0] == "before<[REDACTED_EMAIL]>after"


def test_explicit_ai_finding_takes_precedence_over_code_heuristic():
    target = "numpy.array@torch.tensor"
    text = "import numpy\nimport torch\nresult = " + target
    findings = [{"entity_text": target, "entity_type": "email", "source": "ai", "reason": "Explicit AI finding"}]
    assert apply_findings_to_text(text, findings)[0] == text.replace(target, "[REDACTED_EMAIL]")


def test_review_merging_does_not_drop_a_distinct_shorter_address():
    text = "bob@audit.test abob@audit.test"
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    session = {"session_id": "synthetic", "messages": [{"content": text}]}
    result, _ = apply_findings_to_session(session, findings)
    assert result["messages"][0]["content"] == "[REDACTED_EMAIL] [REDACTED_EMAIL]"


def test_whole_private_key_wins_over_fragments_even_in_one_pass(builtin_conn):
    key = "-----BEGIN PRIVATE KEY-----\nabc@ \nSYNTHETIC\n-----END PRIVATE KEY-----"
    result, _ = secrets.apply_findings_to_blob(
        {"messages": [{"content": key + " abc@ abc"}]}, builtin_conn,
        "synthetic-boundary-audit", max_passes=1,
    )
    assert result["messages"][0]["content"] == "[REDACTED_PRIVATE_KEY] [REDACTED_EMAIL]@ abc"


def test_captured_value_equal_to_variable_name_keeps_label(render_builtin):
    text = "PASSWORD=PASSWORD PASSWORD"
    expected = "PASSWORD=[REDACTED_ENV_SECRET] [REDACTED_ENV_SECRET]"
    assert render_builtin(text) == expected
    assert secrets.redact_session({"messages": [{"content": text}]})[0]["messages"][0]["content"] == expected


def test_entropy_finding_elsewhere_cannot_remove_assignment_quotes(render_builtin):
    value = TOKEN_BODY * 2
    text = 'PASSWORD="' + value + '"\n"' + value + '"'
    output = render_builtin(text)
    assert output == 'PASSWORD="[REDACTED_ENV_SECRET]"\n"[REDACTED_ENV_SECRET]"'


def test_seeded_occurrence_cases_preserve_complete_ordinary_text(render_builtin):
    for seed in (225, 224, 911):
        rng = random.Random(seed)
        for _ in range(50):
            word = "word" + str(rng.randrange(10_000))
            method = rng.choice(["local", "internal", "corp", "lan", "intranet", "localnet"])
            text = f'obj = object()\nvalue = obj.{method}("{word}@ {word} {word}@audit.test obj.{method}")'
            expected = f'obj = object()\nvalue = obj.{method}("[REDACTED_EMAIL]@ {word} [REDACTED_EMAIL] [REDACTED_URL]")'
            assert render_builtin(text) == expected


def test_repeated_code_and_assignment_contexts_have_a_deadline():
    code = r'''
from clawjournal.redaction.secrets import redact_session
from clawjournal.redaction.pii import scan_text_for_pii
from clawjournal.redaction.replacements import replace_email_fragments
source = 'obj = object()\n' + 'value = obj.local("db01.local")\n' * 1_000
matches = scan_text_for_pii(source)
assert len(matches) == 1_000
assert all(source[m["start"] - 1] == '"' for m in matches)
source = "PASSWORD=PASSWORD\n" * 3_000
result = redact_session({"messages": [{"content": source}]})[0]["messages"][0]["content"]
assert result == "PASSWORD=[REDACTED_ENV_SECRET]\n" * 3_000
source = "abc@ abc " * 100_000
result, count = replace_email_fragments(source, {"abc": "[REDACTED_EMAIL]"})
assert count == 100_000
assert result == "[REDACTED_EMAIL]@ abc " * 100_000
'''
    subprocess.run([sys.executable, "-c", code], check=True, timeout=10, capture_output=True, text=True)


@pytest.mark.parametrize("value,code", [
    ("obj.local", "value = obj.local()"),
    ("numpy.array@torch.tensor", "import numpy\nimport torch\nvalue = numpy.array@torch.tensor"),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_known_password_overrides_code_exemptions_across_fields(builtin_conn, value, code, reverse):
    messages = [{"content": 'PASSWORD="' + value + '"'}, {"content": code}]
    if reverse:
        messages.reverse()
    blob = {"messages": messages, "ai_learning_summary": code}
    output, _ = secrets.apply_findings_to_blob(blob, builtin_conn, "synthetic-boundary-audit")
    assert all(value not in m["content"] for m in output["messages"])
    assert value not in output["ai_learning_summary"]
    assert any(m["content"] == 'PASSWORD="[REDACTED_ENV_SECRET]"' for m in output["messages"])


@pytest.mark.parametrize("text", [
    'import os\nDB_HOST=os.getenv("DATABASE_HOST")\nprint(os.name)',
    'DB_HOST=config["db_host"]\nconfiguration = config.copy()',
    '{"DB_HOST": config.database_host, "description": "configuration is ready"}',
    '{"telegramBotToken": settings.telegram_token, "description": "settings is ready"}',
    'telegram_bot_token = get_token()',
    'DB_HOST = process.env.DB_HOST',
])
def test_contextual_rules_preserve_configuration_expressions(render_builtin, text):
    assert render_builtin(text) == text
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert apply_findings_to_text(text, findings)[0] == text


@pytest.mark.parametrize("field,value", [("DB_HOST", "config"), ("telegramBotToken", "settings")])
def test_quoted_values_are_still_sensitive_even_when_they_look_like_variables(render_builtin, field, value):
    text = '{"' + field + '": "' + value + '"}'
    assert value not in render_builtin(text)


def test_config_expression_protection_does_not_cover_arguments(render_builtin):
    text = 'DB_HOST = os.getenv("DATABASE_HOST", "db01.local")'
    assert render_builtin(text) == 'DB_HOST = os.getenv("DATABASE_HOST", "[REDACTED_URL]")'


@pytest.mark.parametrize("ordinary", ["db.locality", "mydb.locality", "mydb.local", "db.local_extra"])
def test_host_replacement_does_not_delete_unrelated_substrings(builtin_conn, ordinary):
    # mydb.local is a distinct sensitive hostname and must be removed as a
    # whole candidate. The other samples are ordinary non-host matches.
    blob = {"messages": [{"content": "server=db.local"}, {"content": ordinary}]}
    output, _ = secrets.apply_findings_to_blob(blob, builtin_conn, "synthetic-boundary-audit")
    expected = "[REDACTED_URL]" if ordinary == "mydb.local" else "[REDACTED_URL]_extra" if ordinary == "db.local_extra" else ordinary
    assert output["messages"][1]["content"] == expected


@pytest.mark.parametrize("separator", ["%40", r"\u0040", "&#64;", "&#x40;"])
def test_encoded_partial_email_uses_scoped_replacement(render_builtin, separator):
    text = "before<alice" + separator + " \nalice remains ordinary"
    expected = "before<[REDACTED_EMAIL]" + separator + " \nalice remains ordinary"
    assert render_builtin(text) == expected
    findings = pii._content_findings_for_text("synthetic", 0, "content", text)
    assert apply_findings_to_text(text, findings)[0] == expected
    assert render_builtin(expected) == expected


@pytest.mark.parametrize("text", [
    'const DB_HOST=process.env.DB_HOST;',
    'Configuration example:\n{"telegramBotToken": settings.telegram_token}',
    'DB_HOST=config%2Edatabase_host',
    'telegramBotToken=settings%2Etelegram_token',
])
def test_reference_prefixes_do_not_abort_or_get_redacted(render_builtin, text):
    # Decoding data must not invent a code exemption for a complete secret
    # assignment already detected by the legacy env rule.
    expected = ("telegramBotToken=[REDACTED_ENV_SECRET]"
                if text == "telegramBotToken=settings%2Etelegram_token" else text)
    assert render_builtin(text) == expected
    tail = " <alice@audit.test>"
    assert render_builtin(text + tail) == expected + " <[REDACTED_EMAIL]>"


def test_long_parsed_reference_keeps_the_same_protection_as_short_source(render_builtin):
    text = 'DB_HOST=config["db_host"]\n' + '# ordinary\n' * 7_000
    assert render_builtin(text) == text


@pytest.mark.parametrize("text,expected", [
    ('DB_HOST="database_backend"', 'DB_HOST="[REDACTED_URL]"'),
    ('DB_HOST=database_backend', 'DB_HOST=[REDACTED_URL]'),
    ('DB_HOST="config.database_host"', 'DB_HOST="[REDACTED_URL]"'),
    ('https://db.local.example.com/status', 'https://[REDACTED_URL].example.com/status'),
])
def test_host_boundaries_never_discard_an_explicitly_detected_value(render_builtin, text, expected):
    assert render_builtin(text) == expected


def test_encoded_partial_email_still_obeys_length_budget(render_builtin):
    from clawjournal.redaction.boundaries import RedactionBoundaryError
    with pytest.raises(RedactionBoundaryError):
        render_builtin('a' * 65 + '%40 ')


def test_ignored_encoded_partial_email_keeps_entity_decision(builtin_conn, render_builtin):
    builtin_conn.execute('INSERT INTO findings VALUES (?, ?, ?)',
                         ('synthetic-boundary-audit', pii.hash_entity('alice'), 'ignored'))
    assert render_builtin('alice%40 alice') == 'alice%40 alice'


@pytest.mark.parametrize("order", list(itertools.permutations(range(3))))
@pytest.mark.parametrize("one_field", [False, True])
def test_credential_priority_is_independent_of_match_and_field_order(builtin_conn, order, one_field):
    import copy
    value = "numpy.array@torch.tensor"
    pieces = ['contact = "' + value + '"', 'PASSWORD="' + value + '"',
              'import numpy\nimport torch\nvalue = ' + value]
    texts = [pieces[n] for n in order]
    if one_field:
        texts = ['\n'.join(texts)]
    blob = {"messages": [{"content": text} for text in texts]}
    for output in (
        secrets.redact_session(copy.deepcopy(blob))[0],
        secrets.apply_findings_to_blob(copy.deepcopy(blob), builtin_conn, "synthetic-boundary-audit")[0],
    ):
        assert all(value not in message["content"] for message in output["messages"])


def test_known_hostname_copies_ignore_case_but_preserve_longer_words(builtin_conn):
    blob = {"messages": [{"content": "DB_HOST=database17"},
                         {"content": "Connect to DATABASE17. Preserve mydatabase17 and DATABASE17_backup."}]}
    output, _ = secrets.apply_findings_to_blob(blob, builtin_conn, "synthetic-boundary-audit")
    assert output["messages"][1]["content"] == "Connect to [REDACTED_URL]. Preserve mydatabase17 and [REDACTED_URL]_backup."


@pytest.mark.parametrize("source,value", [
    ('client --password obj.local', 'obj.local'),
    ('Authorization: Bearer longvalueforaudit.local', 'longvalueforaudit.local'),
    ('https://audit.test/?token=longvalueforaudit.local', 'longvalueforaudit.local'),
])
@pytest.mark.parametrize("reverse", [False, True])
def test_other_credential_sources_also_override_code_heuristics(builtin_conn, source, value, reverse):
    import copy
    messages = [{"content": source}, {"content": 'value = ' + value + '()'}]
    if reverse:
        messages.reverse()
    for output in (
        secrets.redact_session({"messages": copy.deepcopy(messages)})[0],
        secrets.apply_findings_to_blob({"messages": copy.deepcopy(messages)}, builtin_conn, "synthetic-boundary-audit")[0],
    ):
        assert all(value not in message["content"] for message in output["messages"])


@pytest.mark.parametrize("field", ['PASSWORD', 'MY_SECRET', 'API_KEY', 'AUTH_KEY', 'ACCESS_TOKEN', 'DB_PASSWORD'])
def test_generic_secret_values_cannot_gain_configuration_reference_exemptions(render_builtin, field):
    # Parentheses can be literal password characters in configuration data.
    # A Python parse alone must not exempt arbitrary credential assignments.
    assert render_builtin(field + '=SecretPass123()') == field + '=[REDACTED_ENV_SECRET]'


@pytest.mark.parametrize("value,expected", [
    ("alex-laptop-alexandermontgomery", "[REDACTED_DEVICE_ID]"),
    ("richardwilliamsmithjohnsonthe3rd1985-laptop", "[REDACTED_DEVICE_ID]"),
    ("alex-laptop-" + "f" * 17, "[REDACTED_DEVICE_ID]"),
    ("alex-laptop-" + "f" * 10, "[REDACTED_DEVICE_ID]"),
    ("alex-laptop" + "A" * 30, "[REDACTED_DEVICE_ID]"),
    ("host " + "n" * 50 + "-laptop-" + "t" * 5 + " ok", "host [REDACTED_DEVICE_ID] ok"),
    ("A" * 200_000 + "-laptop", "A" * 199_968 + "[REDACTED_DEVICE_ID]"),
    ("image sha " + "a" * 64 + "-server ok", "image sha " + "a" * 32 + "[REDACTED_DEVICE_ID] ok"),
    ("deploy app-server-" + "f" * 80 + " done", "deploy [REDACTED_DEVICE_ID]" + "f" * 64 + " done"),
    ("run Codex-Desktop-" + "0123456789abcdef" * 5 + " done",
     "run [REDACTED_DEVICE_ID]" + "0123456789abcdef" * 4 + " done"),
    ("Before " + "ordinaryprose" * 6 + "alex-laptop after",
     "Before " + ("ordinaryprose" * 6)[:50] + "[REDACTED_DEVICE_ID] after"),
    ("p" * 40 + "-laptop-" + "t" * 30, "p" * 8 + "[REDACTED_DEVICE_ID]" + "t" * 14),
    ("alex-laptop" + "b" * 70, "[REDACTED_DEVICE_ID]" + "b" * 54),
], ids=["dashed-name-tail", "long-name", "dashed-tail-17", "dashed-tail-10", "undelimited-tail-30",
        "budget-63", "uppercase-personal-host", "hash-prefix", "hex-tail-with-dash",
        "mixed-case-product-tail", "glued-prose", "both-runs-long", "attached-tail-70"])
def test_personal_host_candidates_within_budget_stay_complete_and_longer_ones_are_bounded(render_builtin, value, expected):
    # Issue #230: a long run next to a device keyword refused the whole trace.
    # Up to 63 bytes the replacement is unchanged; beyond it, only a bounded
    # core around the keyword is replaced and the surplus run stays.
    from clawjournal.redaction.boundaries import ensure_text_boundaries

    assert render_builtin(value) == expected
    ensure_text_boundaries(value)
    secrets.redact_text(value, strict=True)


_OLD_PERSONAL_HOST = r"([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)"
_KEYWORD = re.compile(r"-(?:macbook|imac|laptop|desktop|pc|workstation|server)", re.I)


@pytest.mark.parametrize("flags,old_pattern", [
    (re.I, "(?<![A-Za-z0-9_])" + _OLD_PERSONAL_HOST + "(?![A-Za-z0-9_])"),
    (0, r"\b" + _OLD_PERSONAL_HOST + r"\b"),
], ids=["candidate-scan", "legacy-rule"])
def test_personal_host_matches_equal_the_original_rule_up_to_the_budget(flags, old_pattern):
    # The scanning rules are the #225 rules, unchanged. The adapter must return
    # their exact spans up to the budget and one bounded span beyond it.
    from clawjournal.redaction import candidate_formats, pii
    from clawjournal.redaction.boundaries import ensure_safe_replacement
    from clawjournal.redaction.candidate_formats import PERSONAL_HOST_BUDGET, personal_host_matches

    old = re.compile(old_pattern, flags)
    new, core = ((candidate_formats._PERSONAL_HOST, candidate_formats._PERSONAL_HOST_CORE) if flags
                 else (pii._PERSONAL_HOST_RULE, pii._PERSONAL_HOST_RULE_CORE))
    assert new.pattern == old.pattern and new.flags == old.flags
    rng = random.Random(230)
    letters = "abcdefghijklmnopqrstuvwxyz" + ("ABCDEF" if flags else "")
    alphabet = letters + "0123456789"
    keywords = ("macbook", "imac", "laptop", "desktop", "pc", "workstation", "server")

    def run(minimum):
        length = rng.choice((rng.randint(minimum, 12), rng.randint(minimum, 70), rng.randint(minimum, 150)))
        return rng.choice(letters) + "".join(rng.choice(alphabet) for _ in range(length - 1))

    for _ in range(4000):
        text = " ".join(
            rng.choice(("", "/", "(", "id:", "_", "9", "\u4e2d")) + run(1) + rng.choice(("", "s")) + "-"
            + rng.choice(keywords) + rng.choice(("", "-", "-" + run(1), run(1)))
            + rng.choice(("", ".", ")", "-x", "_", "\u4e2d", "- x"))
            for _ in range(rng.randint(1, 3))
        )
        old_spans = [m.span(1) for m in old.finditer(text)]
        new_spans = [m.span(1) for m in personal_host_matches(new, core, text)]
        for start, end in old_spans:
            if end - start <= PERSONAL_HOST_BUDGET:
                assert (start, end) in new_spans, (text, (start, end), new_spans)
            else:
                inside = [span for span in new_spans if start <= span[0] < span[1] <= end]
                assert len(inside) == 1, (text, (start, end), new_spans)
                assert inside[0][1] - inside[0][0] <= PERSONAL_HOST_BUDGET
        for start, end in new_spans:
            assert any(s <= start < end <= e for s, e in old_spans), (text, (start, end), old_spans)
            candidate = text[start:end]
            assert _KEYWORD.search(candidate)
            ensure_safe_replacement(candidate, "personal_hostname")


@pytest.mark.parametrize("text", [
    "foo_" + "b" * 40 + "-laptop", "1" + "b" * 39 + "-laptop", "foo_alex-laptop",
], ids=["underscore-long-run", "digit-first-long-run", "underscore-short-run"])
def test_runs_without_a_valid_start_keep_the_original_behaviour(render_builtin, text):
    # The rules still require a word boundary and a leading letter, exactly
    # as before; the bounded core applies only inside an oversized match.
    assert render_builtin(text) == text
