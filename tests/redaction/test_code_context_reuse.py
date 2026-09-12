"""Retain original code evidence without reparsing every redaction result."""
import copy
import sqlite3

import pytest

from clawjournal import findings
from clawjournal.redaction import code_context as cc, secrets
from clawjournal.redaction.boundaries import RedactionBoundaryError, ensure_text_boundaries
from clawjournal.redaction.replacements import replace_spans


def finding(value, kind="email", source="rule", replacement=None):
    return {"entity_text": value, "entity_type": kind, "source": source,
            "confidence": 0.9, "replacement": replacement or findings.replacement_for_type(kind)}


def test_many_findings_parse_source_a_constant_number_of_times(monkeypatch):
    text = "".join(f'x = "person{n:03d}@example.com"\n' for n in range(300))
    items = [finding(f"person{n:03d}@example.com") for n in range(300)]
    calls = []
    original = cc._parse
    def record(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(cc, "_parse", record)
    result, count = findings.apply_findings_to_text(text, items)
    assert result == 'x = "[REDACTED_EMAIL]"\n' * 300
    assert count == 300
    assert len(calls) <= 3
    assert all(source == text for source in calls)


@pytest.mark.parametrize("route", ["findings", "map"])
def test_many_host_boundary_checks_reuse_detection_and_shift_offsets(route, monkeypatch):
    text = "".join(f'x = "host{n:03d}.local.example.com"\n' for n in range(300))
    calls = []
    original = cc._parse
    def record(source):
        calls.append(source)
        return original(source)
    monkeypatch.setattr(cc, "_parse", record)
    if route == "findings":
        output = findings.apply_findings_to_text(text, [finding(f"host{n:03d}.local", "private_url") for n in range(300)])
        assert len(calls) <= 5
    else:
        output = secrets._apply_redaction_set(text, {f"host{n:03d}.local": "[REDACTED_URL]" for n in range(300)})
        assert len(calls) <= 3
    # Retain the established private-suffix policy and every ordinary suffix.
    assert output == ('x = "[REDACTED_URL].example.com"\n' * 300, 300)


@pytest.mark.parametrize("route", ["findings", "map"])
def test_non_host_replacement_rechecks_newly_exposed_host_boundaries(route):
    longest = "longhostwithmanyletters000.local"
    prefix = "ABCDEFGHIJKLMNOPQRSTUVWXY"
    text = f'x = "{longest}.example.com"\ny = "{prefix}db.local.example.com"\n'
    if route == "findings":
        result = findings.apply_findings_to_text(text, [finding(longest, "private_url"),
                finding(prefix, source="ai"), finding("db.local", "private_url")])
    else:
        result = secrets._apply_redaction_set(text, {longest: "[REDACTED_URL]",
                 prefix: "[REDACTED_CUSTOM]", "db.local": "[REDACTED_URL]"})
    replacement = "[REDACTED_EMAIL]" if route == "findings" else "[REDACTED_CUSTOM]"
    assert result == (f'x = "[REDACTED_URL].example.com"\ny = "{replacement}[REDACTED_URL].example.com"\n', 3)


@pytest.mark.parametrize("route", ["findings", "map"])
def test_previous_replacements_move_but_do_not_reparse_code(route, monkeypatch):
    email = "numpy.array@torch.tensor"
    text = 'label = "verylongperson@example.com"\nimport numpy\nimport torch\nvalue = '+email+'\nobj.local()\ncontact = "'+email+'"\n'
    expected = text.replace("verylongperson@example.com", "[REDACTED_EMAIL]").replace('"'+email+'"', '"[REDACTED_EMAIL]"')
    if route == "findings":
        actual, count = findings.apply_findings_to_text(text, [finding("verylongperson@example.com"), finding(email), finding("obj.local", "private_url")])
    else:
        calls = []
        original = cc._parse
        def record(source):
            calls.append(source)
            return original(source)
        monkeypatch.setattr(cc, "_parse", record)
        actual, count = secrets._apply_redaction_set(text, {"verylongperson@example.com": "[REDACTED_EMAIL]", email: "[REDACTED_EMAIL]", "obj.local": "[REDACTED_URL]"})
        assert calls == [text]
    assert actual == expected
    assert count == 2


@pytest.mark.parametrize("source", ["rule", "ai"])
def test_original_code_evidence_survives_an_earlier_edit_that_breaks_syntax(source):
    # Removing the end of a string must not make an unrelated genuine method
    # call disappear merely because the mutated text no longer parses.
    text = 'x = "long-marker"\nobj.local()\ncontact = "obj.local"\n'
    items = [finding('long-marker"', "custom_sensitive", source=source), finding("obj.local", "private_url")]
    expected = text.replace('long-marker"', "[REDACTED]").replace('"obj.local"', '"[REDACTED_URL]"')
    assert findings.apply_findings_to_text(text, items) == (expected, 2)


def test_credential_edit_inside_a_protected_interval_invalidates_it():
    text = "obj.local(); other.local()"
    context = cc.code_context(text)
    result, count = replace_spans(text, [(0, 3, "[REDACTED]")], context=context)
    assert count == 1
    assert not context.protects(0, len("[REDACTED].local"))
    start = result.index("other.local")
    assert context.protects(start, start + len("other.local"))


@pytest.mark.parametrize("length", [65535, 65536, 65537, 200000])
@pytest.mark.parametrize("wrapper", ["source", "fenced", "string"])
def test_code_evidence_does_not_silently_disappear_at_64k(length, wrapper):
    code = "import numpy\nimport torch\nvalue = numpy.array@torch.tensor\nobj.local()\n"
    body = code + "#" * (length - len(code)) + "\n"
    text = body if wrapper == "source" else ("```python\n" + body + "```" if wrapper == "fenced" else 'data = """\n```python\n' + body + '```\n"""')
    context = cc.code_context(text)
    for value in ("numpy.array@torch.tensor", "obj.local"):
        start = text.index(value)
        assert context.protects(start, start + len(value)) == (wrapper != "string")


@pytest.mark.parametrize("length", [65535, 65536, 65537, 200000])
def test_long_code_output_preserves_calls_and_redacts_literal_copies(length):
    prefix = 'obj.local()\ncontact = "obj.local"\n'
    text = prefix + "#" * (length - len(prefix))
    result, count = findings.apply_findings_to_text(text, [finding("obj.local", "private_url")])
    assert result == text.replace('"obj.local"', '"[REDACTED_URL]"')
    assert count == 1


def test_long_source_keeps_preceding_imports_and_unicode_offsets():
    text = '标签 = "😀"\r\nimport numpy\r\nimport torch\r\n' + '# ordinary\r\n' * 7000 + 'value = numpy.array@torch.tensor\r\n'
    context = cc.code_context(text)
    start = text.index('numpy.array@torch.tensor')
    assert context.protects(start, start + len('numpy.array@torch.tensor'))


def test_parse_budget_defers_instead_of_disabling_protection(monkeypatch):
    monkeypatch.setattr(cc, "_MAX_PARSE_CHARS", 100)
    text = "obj.local()\n" + "#" * 101
    with pytest.raises(RedactionBoundaryError) as raised:
        ensure_text_boundaries(text)
    assert raised.value.rule == "code_context_budget"
    assert "obj.local" not in str(raised.value)


@pytest.mark.parametrize("limit", ["_MAX_PARSE_TOKENS", "_MAX_STATEMENT_TOKENS"])
def test_complexity_budget_is_explicit(limit, monkeypatch):
    monkeypatch.setattr(cc, limit, 5)
    text = "obj.local()\n" + "#" * 66000 + "\nx = a + b + c + d\n"
    with pytest.raises(RedactionBoundaryError) as raised:
        cc.code_context(text)
    assert raised.value.rule == "code_context_budget"


@pytest.mark.parametrize("error", [MemoryError, RecursionError])
def test_parser_resource_failure_never_returns_empty_protection(error, monkeypatch):
    def fail(*args):
        raise error("SYNTHETIC_PRIVATE_SOURCE")
    monkeypatch.setattr(cc.ast, "parse", fail)
    with pytest.raises(RedactionBoundaryError) as raised:
        cc.code_context("obj.local()")
    assert raised.value.rule == "code_context_budget"
    assert "SYNTHETIC_PRIVATE_SOURCE" not in str(raised.value)


def test_parse_budget_blocks_blob_before_mutation_or_external_scans(monkeypatch):
    monkeypatch.setattr(cc, "_MAX_PARSE_CHARS", 100)
    for scanner in ("betterleaks", "trufflehog"):
        monkeypatch.setattr(f"clawjournal.redaction.{scanner}.{scanner}_secret_map_from_blob",
                            lambda *a, **kw: pytest.fail("No external scan after failed preflight"))
    blob = {"messages": [{"content": "obj.local()\n" + "#" * 101}]}
    original = copy.deepcopy(blob)
    with sqlite3.connect(":memory:") as conn:
        with pytest.raises(RedactionBoundaryError) as raised:
            secrets.apply_findings_to_blob(blob, conn, "synthetic-context-budget")
    assert raised.value.rule == "code_context_budget"
    assert blob == original


@pytest.mark.parametrize("padding", [0, 70000])
@pytest.mark.parametrize("closed", [False, True])
@pytest.mark.parametrize("prefix", ["", "r", "f", "rf", "b"])
def test_fences_inside_strings_do_not_gain_exemptions_after_a_parse_error(padding, closed, prefix):
    text = '变量 = ' + prefix + '"""\n```python\nimport numpy\nimport torch\nvalue = numpy.array@torch.tensor\nobj.local()\n```\n'
    text += '"""\nnot python here\n' if closed else ''
    text += '#' * padding
    expected = text.replace("numpy.array@torch.tensor", "[REDACTED_EMAIL]").replace("obj.local", "[REDACTED_URL]")
    result = findings.apply_findings_to_text(text, [finding("numpy.array@torch.tensor"), finding("obj.local", "private_url")])
    assert result == (expected, 2)


def test_incomplete_lexical_analysis_cannot_whitelist_a_fence():
    text = '  x = 1\n y = 2\n```python\nobj.local()\n```'
    with pytest.raises(RedactionBoundaryError) as raised:
        cc.code_context(text)
    assert raised.value.rule == "code_context_syntax"


def test_early_token_error_cannot_whitelist_later_fences(monkeypatch):
    def interrupted_tokens(_readline):
        raise cc.tokenize.TokenError("unterminated string literal", (1, 4))
    monkeypatch.setattr(cc.tokenize, "generate_tokens", interrupted_tokens)
    with pytest.raises(RedactionBoundaryError) as raised:
        list(cc._fenced_sources('x = "\n```python\nobj.local()\n```'))
    assert raised.value.rule == "code_context_syntax"


def test_malformed_single_quote_before_string_data_never_exempts_its_fence():
    text = 'broken = "\npayload = """\n```python\nobj.local()\n```\n"""\n'
    try:
        result = findings.apply_findings_to_text(text, [finding("obj.local", "private_url")])
    except RedactionBoundaryError as exc:
        # Python versions differ in whether tokenization continues after the
        # first malformed string. Stopping safely must keep the trace local.
        assert exc.rule == "code_context_syntax"
    else:
        assert result == (text.replace("obj.local", "[REDACTED_URL]"), 1)
