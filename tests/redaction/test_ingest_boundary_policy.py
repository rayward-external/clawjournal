"""Local indexing survives ambiguous text; export still requires safe bounds."""
import json

import pytest

from clawjournal.findings import apply_findings_to_text
from clawjournal.parsing import parser
from clawjournal.redaction import code_context, pii, secrets
from clawjournal.redaction.anonymizer import Anonymizer
from clawjournal.redaction.boundaries import RedactionBoundaryError
from clawjournal.workbench import daemon, index


PAT = "github_pat_11ABCDEFG0" + "abcdefghij" * 7
CJK = "请把测试报告和完整的日志文件一起发送到我的工作邮箱"
AMBIGUOUS = [
    f"git clone https://x-access-token:{PAT}@github.com/acme/synthetic-app.git",
    CJK + "alice@audit.test",
    "DATABASE_URL=postgresql://postgres:" + "Synthetic123_" * 15 + "@db.audit.test:5432/app",
    "cp ./" + "synthetic-filename-" * 8 + "@archive.audit.test /tmp",
]


@pytest.mark.parametrize("text", AMBIGUOUS, ids=["pat-clone", "chinese", "database-url", "long-filename"])
def test_ambiguous_commands_remain_indexable_but_cannot_bypass_export(text):
    local, _count, _log = secrets.redact_text(text)
    parsed = parser._parse_tool_input("Bash", {"command": text}, Anonymizer(enabled=False))
    assert parsed["command"] == local
    assert local
    if text.startswith(CJK):
        assert local.startswith(CJK)
    with pytest.raises(RedactionBoundaryError):
        secrets.redact_text(text, strict=True)


def test_local_deferral_does_not_drop_a_separate_known_secret():
    key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    text = "prefix " + "A" * 1000 + "@audit.test> " + key + " ordinary tail"
    result, count, _log = secrets.redact_text(text)
    assert result == "prefix " + "A" * 936 + "[REDACTED_EMAIL]> [REDACTED_ANTHROPIC_KEY] ordinary tail"
    assert count == 2
    assert any(entry.get("boundary_limited") for entry in _log)
    with pytest.raises(RedactionBoundaryError):
        secrets.redact_text(text, strict=True)


@pytest.mark.parametrize("text", [
    "the quick brown fox jumps over the lazy dog " * 250 + " note: done",
    "files: " + " ".join(f"module_{i}_impl" for i in range(1600)),
    "x = " + " ".join(f"w{i}" for i in range(1800)),
], ids=["prose", "file-list", "invalid-assignment"])
def test_long_word_lists_do_not_overflow_parser_or_skip_sensitive_tails(text):
    assert secrets.scan_text(text) == []
    assert secrets.redact_text(text, strict=True)[0] == text
    tail = " <alice@audit.test>"
    findings = pii.scan_text_for_pii(text + tail)
    assert any(f["match"] == "alice@audit.test" for f in findings)
    key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    assert secrets.redact_text(text + " " + key, strict=True)[0] == text + " [REDACTED_ANTHROPIC_KEY]"


@pytest.mark.parametrize("source", [
    "def stream():\n    yield from values\n",
    "from package import thing as alias\n",
    "value = key not in mapping\n",
    "match subject:\n    case value:\n        pass\n",
    "async def run():\n    return await operation()\n",
])
def test_keyword_sequences_are_still_valid_code(source):
    assert code_context._parse(source) is not None


def test_parser_failure_does_not_return_raw_local_text(monkeypatch):
    def fail(_source):
        raise MemoryError("synthetic parser overflow")
    monkeypatch.setattr(code_context, "_ast_parse", fail)
    text = 'obj.local(); contact = "alice@audit.test"'
    expected = text.replace("alice@audit.test", "[REDACTED_EMAIL]")
    for strict in (False, True):
        result, count, log = secrets.redact_text(text, strict=strict)
        assert (result, count) == (expected, 1)
        assert log[0]["type"] == "email"


def test_unrelated_provider_finding_does_not_block_ordinary_text():
    finding = {"entity_text": "b" * 300 + "@audit.test", "entity_type": "email",
               "source": "ai", "confidence": 0.9}
    assert apply_findings_to_text("ordinary content", [finding]) == ("ordinary content", 0)
    with pytest.raises(RedactionBoundaryError):
        apply_findings_to_text(finding["entity_text"], [finding])


def test_strict_scan_indexes_whole_project_with_reference_prose(tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    project = projects / "synthetic-project"
    project.mkdir(parents=True)
    monkeypatch.setattr(parser, "PROJECTS_DIR", projects)
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(index, "CONFIG_DIR", tmp_path / "config")
    monkeypatch.setattr(daemon, "load_config", lambda: {"findings_engines": ["regex_secrets", "regex_pii"]})
    monkeypatch.setattr(daemon, "discover_projects", lambda **kw: [
        {"source": "claude", "dir_name": project.name, "locator": None},
    ])
    for sid, command in [("bad", AMBIGUOUS[1]), ("good", "echo ordinary text")]:
        # A reference in prose is not a host value and must not interrupt
        # findings for this session or later sessions in the project.
        content = "Notes:\nDB_HOST=cfg['db_host']" if sid == "bad" else "Ordinary result"
        entries = [
            {"type": "user", "timestamp": 1706000000000,
             "message": {"content": command}, "cwd": str(project)},
            {"type": "assistant", "timestamp": 1706000001000,
             "message": {"model": "m", "content": [
                 {"type": "text", "text": content},
                 {"type": "tool_use", "id": "test-tool", "name": "Bash", "input": {"command": command}},
             ], "usage": {"input_tokens": 1, "output_tokens": 1}}},
        ]
        (project / f"{sid}.jsonl").write_text("".join(json.dumps(e) + "\n" for e in entries))
    report = daemon.Scanner().scan_once_strict(["claude"])
    assert report["ok"], report
    conn = index.open_index()
    try:
        rows = conn.execute("SELECT session_id, hold_state FROM sessions").fetchall()
        assert len(rows) == 2
        by_id = {row["session_id"]: row["hold_state"] for row in rows}
        assert by_id["bad"] != "pending_review"
        assert by_id["good"] != "pending_review"
    finally:
        conn.close()


@pytest.mark.parametrize("workers", [1, 2])
@pytest.mark.parametrize("entity_type", ["email", "device_id"])
def test_final_pii_boundary_failure_keeps_session_identity_and_file_bytes(tmp_path, monkeypatch, workers, entity_type):
    text = "x" * 70 + ("@audit.test" if entity_type == "email" else "")
    sessions = [{"session_id": sid, "messages": [{"role": "user", "content": content}]}
                for sid, content in [("bad", text), ("good", "ordinary content")]]
    path = tmp_path / "sessions.jsonl"
    original = "".join(json.dumps(s) + "\n" for s in sessions)
    path.write_text(original)
    def review(session, **kw):
        findings = [{"session_id": "bad", "entity_text": text, "entity_type": entity_type,
                     "source": "ai", "confidence": 0.9}] if session["session_id"] == "bad" else []
        return findings, "full"
    monkeypatch.setattr(pii, "review_session_pii_hybrid", review)
    monkeypatch.setenv("CLAWJOURNAL_UPLOAD_PII_WORKERS", str(workers))
    error, manifest = daemon.finalize_share_export_for_upload(tmp_path, {}, conn=None, ai_pii=True)
    assert error["status"] == 422
    assert error["block_reason"] == "redaction-boundary"
    assert [s["session_id"] for s in error["blocked_sessions"]] == ["bad"]
    assert manifest["blocked"]
    assert path.read_text() == original
    assert text not in json.dumps(error)


def test_limited_local_email_span_cannot_evict_an_overlapping_credential():
    key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    text = key + "A" * 1000 + "@audit.test"
    result, count, _ = secrets.redact_text(text)
    assert key not in result
    assert result == "[REDACTED_ANTHROPIC_KEY]@audit.test"
    assert count == 1
