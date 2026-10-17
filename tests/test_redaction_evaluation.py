"""Regression checks for private storage, label integrity and metric semantics."""
import copy
import json
import os
from pathlib import Path
import sys

import pytest

from clawjournal.redaction.evaluation import (
    POLICY, Store, capture_preview, evaluate, metrics, text_units,
)


def fake_preview(session):
    session["messages"][0]["content"] = "[REDACTED]"
    return session, {"stage": "test-preview"}


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "private", initialize=True)
    yield store
    store.close()


def capture(store, text="🧪 Contact jane@example.org. loss=0.1234", **kwargs):
    return store.capture({"messages": [{"role": "user", "content": text}]},
                         group=kwargs.get("group", "contributor-a"), split=kwargs.get("split", "dev"),
                         source_kind="synthetic", provenance="Invented test fixture", preview=fake_preview)


def corrections(store, capture_id, annotations):
    saved = store.get(capture_id)
    return {"capture_id": capture_id, "pair_hash": saved["pair_hash"], "revision": saved["revision"],
            "policy": POLICY, "annotations": annotations}


def annotation(start, end, decision="redact", status="human_reviewed"):
    return {"unit": "/messages/0/content", "start": start, "end": end,
            "decision": decision, "entity_type": "email", "note": "fixture",
            "review_status": status}


def test_opt_in_disable_permissions_and_original_not_mutated(tmp_path):
    root = tmp_path / "private"
    with pytest.raises(ValueError, match="not enabled"):
        Store(root)
    store = Store(root, initialize=True)
    original = {"messages": [{"content": "original"}]}
    before = copy.deepcopy(original)
    cid = store.capture(original, group="a", split="dev", source_kind="owned",
                        provenance="My conversation", preview=fake_preview)
    assert original == before
    assert store.get(cid)["original"]["messages"][0]["content"] == "original"
    assert store.get(cid)["redacted"]["messages"][0]["content"] == "[REDACTED]"
    if os.name != "nt":
        assert root.stat().st_mode & 0o777 == 0o700
        assert (root / "evaluation.db").stat().st_mode & 0o777 == 0o600
    store.disable()
    with pytest.raises(ValueError, match="disabled"):
        capture(store)
    assert store.get(cid)
    store.close()
    reopened = Store(root, initialize=True)
    assert reopened.settings()["enabled"] == 0  # init must not silently re-enable
    reopened.close()


def test_dedup_split_and_source_isolation(store):
    cid = capture(store)
    expiry = store.list()[0]["expires"]
    assert capture(store) == cid
    assert store.list()[0]["expires"] == expiry
    with pytest.raises(ValueError, match="same split"):
        capture(store, text="different", split="test")
    with pytest.raises(ValueError, match="different group"):
        capture(store, group="b", split="test")
    assert len(store.list()) == 1


def test_quota_rollback(store):
    store.conn.execute("UPDATE settings SET quota=1")
    store.conn.commit()
    with pytest.raises(ValueError, match="quota"):
        capture(store)
    assert store.list() == []
    assert store.conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0] == 0


def test_retention_deletes_originals_labels_and_managed_page(store):
    cid = capture(store)
    store.import_corrections(corrections(store, cid, [annotation(10, 26)]))
    page = store.review(cid)
    store.conn.execute("UPDATE captures SET expires=0")
    store.conn.commit()
    assert store.prune() == 1
    assert not page.exists()
    assert store.list() == []
    assert store.conn.execute("SELECT COUNT(*) FROM corrections").fetchone()[0] == 0


def test_store_symlinks_rejected(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        Store(link, initialize=True)


def test_revision_and_payload_identity_protect_corrections(store):
    cid = capture(store)
    document = corrections(store, cid, [annotation(10, 26)])
    altered = {**document, "pair_hash": "wrong"}
    with pytest.raises(ValueError, match="match capture"):
        store.import_corrections(altered)
    assert store.import_corrections(document) == 1
    with pytest.raises(ValueError, match="Stale"):
        store.import_corrections(document)
    assert store.get(cid)["revision"] == 1


@pytest.mark.parametrize("bad", [
    [annotation(-1, 3)], [annotation(0, 500)], [annotation(True, 3)],
    [annotation(2, 2)], [annotation(1, 3), annotation(2, 4)],
    [annotation(1, 3, status="automatic")], [annotation(1, 3, decision="safe")],
    [{**annotation(1, 3), "entity_type": ""}], [None],
], ids=["negative", "out-of-bounds", "bool", "empty", "overlap", "status", "decision", "type", "nonobject"])
def test_invalid_annotation_import_is_atomic(store, bad):
    cid = capture(store)
    with pytest.raises(ValueError):
        store.import_corrections(corrections(store, cid, bad))
    assert store.get(cid)["revision"] == 0
    assert store.get(cid)["annotations"] == []


def test_review_escapes_conversation_html_and_unicode_offsets(store):
    text = '🧪 </script><script>alert("unsafe")</script> λ=1'
    cid = capture(store, text)
    start = text.index("λ")
    store.import_corrections(corrections(store, cid, [annotation(start, start + 3, "preserve")]))
    html = store.review(cid).read_text()
    assert '<script>alert("unsafe")' not in html
    assert "\\u003c/script>" in html
    assert "connect-src 'none'" in html
    assert "Array.from(prefix.toString()).length" in html
    assert "innerHTML" not in html
    assert text_units(store.get(cid)["original"])["/messages/0/content"][start:start + 3] == "λ=1"


def test_drafts_uncertainty_and_partial_detection_are_distinct(store):
    cid = capture(store, "email123 value456 draft789 uncertain")
    labels = [annotation(0, 8), annotation(9, 17, "preserve"),
              annotation(18, 26, status="assistant_draft"), annotation(27, 36, "uncertain")]
    store.import_corrections(corrections(store, cid, labels))
    predictor = lambda text: [{"start": 0, "end": 5, "label": "email"}]
    report = evaluate(store, predictor, {"engine": "fake"})
    assert report["targets"] == 2
    assert report["excluded"] == {"draft": 1, "uncertain": 1, "duplicate": 0}
    assert report["detection_metrics"]["TP"] == 1
    assert report["detection_metrics"]["TN"] == 1
    assert report["sensitive_span_coverage"] == {"total": 1, "fully_covered": 0, "partial": 1,
                                                "missed": 0, "full_coverage_rate": 0}
    assert evaluate(store, predictor, {}, include_drafts=True)["targets"] == 3
    repeat = evaluate(store, predictor, {"engine": "fake"})
    assert repeat == report
    assert "email123" not in json.dumps(report)


def test_metric_denominators_and_predictor_errors(store):
    result = metrics(0, 0, 0, 2)
    assert result["precision"] is None
    assert result["FNR"] is None
    assert result["TNR_specificity"] == 1
    cid = capture(store, "abc")
    with pytest.raises(ValueError, match="No eligible"):
        evaluate(store, lambda t: [], {})
    store.import_corrections(corrections(store, cid, [annotation(0, 3)]))
    with pytest.raises(ValueError, match="invalid offsets"):
        evaluate(store, lambda t: [{"start": -1, "end": 3}], {})


def test_recapturing_does_not_inflate_metrics_and_conflicts_fail(store):
    first = capture(store, "abc")
    second = store.capture({"messages": [{"role": "user", "content": "abc"}]},
                           group="contributor-a", split="dev", source_kind="synthetic",
                           provenance="A different redaction run", preview=fake_preview)
    store.import_corrections(corrections(store, first, [annotation(0, 3)]))
    store.import_corrections(corrections(store, second, [annotation(0, 3)]))
    report = evaluate(store, lambda t: [], {})
    assert report["targets"] == 1
    assert report["excluded"]["duplicate"] == 1
    store.import_corrections(corrections(store, second, [annotation(0, 3, "preserve")]))
    with pytest.raises(ValueError, match="conflicting"):
        evaluate(store, lambda t: [], {})


def test_preview_refuses_bypass(monkeypatch):
    monkeypatch.setenv("CLAWJOURNAL_SKIP_BETTERLEAKS", "1")
    with pytest.raises(ValueError, match="bypass"):
        capture_preview({"session_id": "test", "messages": []})


def test_preview_uses_copy_and_empty_findings_database(monkeypatch):
    from types import SimpleNamespace
    from clawjournal.redaction import betterleaks
    from clawjournal.workbench import index
    monkeypatch.delenv("CLAWJOURNAL_SKIP_BETTERLEAKS")
    monkeypatch.delenv("CLAWJOURNAL_SKIP_TRUFFLEHOG")
    monkeypatch.setattr(betterleaks, "scan_text", lambda t: SimpleNamespace(binary_missing=False, scan_error=None, bypassed=False))
    monkeypatch.setattr(betterleaks, "engine_fingerprint", lambda: "test")
    def apply(conn, value, **kwargs):
        assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
        value["messages"][0]["content"] = "redacted"
        return value, 1, []
    monkeypatch.setattr(index, "apply_share_redactions", apply)
    source = {"session_id": "test", "messages": [{"content": "original"}]}
    result, metadata = capture_preview(source)
    assert source["messages"][0]["content"] == "original"
    assert result["messages"][0]["content"] == "redacted"
    assert metadata["changes"] == 1


def test_cli_registration_and_no_default_capture(tmp_path, monkeypatch, capsys):
    from clawjournal.cli import main
    root = tmp_path / "store"
    monkeypatch.setattr(sys, "argv", ["clawjournal", "redaction-eval", "--store", str(root), "status"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code == 2
    assert not root.exists()
    monkeypatch.setattr(sys, "argv", ["clawjournal", "redaction-eval", "--store", str(root), "init"])
    main()
    assert json.loads(capsys.readouterr().out)["captures"] == 0
