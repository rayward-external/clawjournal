"""Check retained text as well as removed text in the built-in apply path.

Compatibility with the old regexes does not establish correct redaction.
The strict xfails below record observed, pre-existing coverage/output defects;
they are not fixes and must not be counted as passing security checks.
External detectors are isolated here, so these are not upload-gate tests.
"""
import hashlib
import random
import sqlite3
import subprocess
import sys

import pytest

from clawjournal.findings import apply_findings_to_text
from clawjournal.redaction import pii, secrets


EMAIL = "alice@redaction-audit.test"
TOKEN_BODY = "AbCdEf0123456789_-" * 2
TOKEN = "123456789:" + TOKEN_BODY
KEY = "-----BEGIN PRIVATE KEY-----\nSYNTHETIC_BODY\n-----END PRIVATE KEY-----"


@pytest.fixture
def render_builtin(monkeypatch):
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
    conn.execute("CREATE TABLE findings (session_id TEXT, entity_hash TEXT, status TEXT)")

    def render(text):
        blob, _ = secrets.apply_findings_to_blob(
            {"messages": [{"content": text}]}, conn, "synthetic-boundary-audit",
        )
        return blob["messages"][0]["content"]

    yield render
    conn.close()


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
    pytest.param("请联系ab@redaction-audit.test谢谢", "[REDACTED_EMAIL]", id="two-characters-next-to-chinese"),
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
    "result = numpy.array@torch.tensor", "value = obj.local()",
])
@pytest.mark.xfail(strict=True, reason="Existing email/hostname heuristics also match ordinary code")
def test_ordinary_code_is_not_redacted(render_builtin, text):
    assert render_builtin(text) == text


@pytest.mark.xfail(strict=True, reason="Existing entity-wide replacement also deletes an ordinary repeated word")
def test_truncated_email_does_not_delete_other_ordinary_text(render_builtin):
    assert render_builtin("abc@  abc abcdef") == "[REDACTED_EMAIL]@  abc abcdef"


@pytest.mark.xfail(strict=True, reason="Existing assignment regex removes part of the variable name")
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
    "a." * 100_000 + "local", "a" * 64 + ".local", "A" * 200_000 + "-laptop",
    "DB_HOST=" + "a" * 200_000,
    ".".join(["a" * 63] * 3 + ["b" * 56, "local"]),
], ids=["attached-email", "truncated-email", "quoted-email", "local-65", "domain-label-64",
        "long-token-tail", "long-token-id", "escaped-token", "named-token",
        "many-host-labels", "host-label-64", "uppercase-personal-host", "host-field", "host-254"])
def test_oversized_candidates_are_deferred_without_mutation(render_builtin, value):
    from clawjournal.redaction.boundaries import RedactionBoundaryError

    with pytest.raises(RedactionBoundaryError) as caught:
        render_builtin(value)
    assert value not in str(caught.value)
    # The direct redactor must follow the same rule as findings-backed export.
    with pytest.raises(RedactionBoundaryError):
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


@pytest.mark.parametrize("host", ["ALEX-LAPTOP", "Alexs-MacBook-Pro", "alex-laptop", "ALEX-PC"])
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
