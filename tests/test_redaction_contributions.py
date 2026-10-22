"""Consent, privacy and split-isolation tests using fictional fixtures only."""
import copy
import json
from pathlib import Path
import sqlite3
import uuid
from argparse import ArgumentParser

import pytest

from clawjournal.redaction import contributions as c
from clawjournal.redaction import ongoing_contributions as ongoing
from clawjournal.redaction.evaluation import POLICY, Store, digest

TEXT = "🧪 Send to alice@private.invalid; contact alice@private.invalid. loss=0.1234"


@pytest.fixture
def store(tmp_path, monkeypatch):
    from clawjournal import config
    monkeypatch.setattr(config, "CONFIG_DIR", tmp_path / "profile")
    store = Store(tmp_path / "evaluation", initialize=True)
    yield store
    store.close()


def capture(store, text=TEXT, split="dev", group="source-person", status="human_reviewed"):
    cid = store.capture({"messages": [{"role": "user", "content": text}]}, group=group,
                        split=split, source_kind="synthetic", provenance="Fictional unit test",
                        preview=lambda s: (s, {"stage": "test"}))
    labels = []
    for value, decision, kind in (("alice@private.invalid", "redact", "email"), ("0.1234", "preserve", "measurement")):
        start = 0
        while (start := text.find(value, start)) >= 0:
            labels.append({"unit": "/messages/0/content", "start": start, "end": start + len(value),
                           "decision": decision, "entity_type": kind, "review_status": status,
                           "note": "A private note which must never enter the bundle"})
            start += len(value)
    saved = store.get(cid)
    store.import_corrections({"capture_id": cid, "pair_hash": saved["pair_hash"],
                              "revision": saved["revision"], "policy": POLICY, "annotations": labels})
    return cid


def prepare(store, **kwargs):
    cid = capture(store, **kwargs)
    return cid, c.prepare(store, cid, "/messages/0/content", 0, len(kwargs.get("text", TEXT)))[0]


def approval(store, pid):
    row, _, _ = c._get(store, pid)
    return {"proposal_id": pid, "snapshot_hash": row["snapshot_hash"],
            "consent_version": c.CONSENT, **dict.fromkeys(c.ATTESTATIONS, True)}


def bundle(store, pid, monkeypatch):
    c.approve(store, approval(store, pid))
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: None)
    return json.loads(c.export_bundle(store, pid).read_text())


def test_prepare_replaces_every_occurrence_and_preserves_unicode_offsets(store):
    cid, pid = prepare(store)
    row, _, p = c._get(store, pid)
    assert row["state"] == "pending"
    assert "alice@private.invalid" not in p["text"]
    assert "0.1234" in p["text"]
    replacements = [p["text"][a["start"]:a["end"]] for a in p["labels"] if a["decision"] == "redact"]
    assert replacements[0] == replacements[1]
    assert all(c._invented("email", value) for value in replacements)
    assert p["text"].startswith("🧪")
    assert c.validate_bundle({"example": p, "approval": {"consent_version": c.CONSENT,
           "purpose": c.PURPOSE, "payload_sha256": digest(p), **dict.fromkeys(c.ATTESTATIONS, True)}}) == p
    assert store.get(cid)["original"]["messages"][0]["content"] == TEXT
    with pytest.raises(ValueError, match="not been explicitly approved"):
        c.export_bundle(store, pid)


@pytest.mark.parametrize("kind,value", [
    ("person", "Jordan Testperson"),
    ("email", "jordan@private.invalid"),
    ("phone_number", "212-555-0174"),
    ("user_name", "jordan_test_user"),
    ("address", "318 Fictional Avenue, Testville"),
])
def test_supported_identifier_replacement_round_trip(store, monkeypatch, kind, value):
    text = "Contact: " + value
    cid = store.capture({"messages": [{"role": "user", "content": text}]},
                        group="fictional-contributor", split="dev", source_kind="synthetic",
                        provenance="Fictional fixture", preview=lambda s: (s, {}))
    saved = store.get(cid)
    store.import_corrections({"capture_id": cid, "pair_hash": saved["pair_hash"],
        "revision": saved["revision"], "policy": POLICY, "annotations": [{
            "unit": "/messages/0/content", "start": 9, "end": len(text),
            "decision": "redact", "entity_type": kind, "review_status": "human_reviewed",
        }]})
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(text))
    outgoing = bundle(store, pid, monkeypatch)
    example = c.validate_bundle(outgoing)
    assert value not in example["text"]
    label = example["labels"][0]
    assert c._invented(kind, example["text"][label["start"]:label["end"]])
    assert c.collect(store.root / "collection", outgoing, str(uuid.uuid4()))["status"] == "collected"


@pytest.mark.parametrize("field", c.ATTESTATIONS)
@pytest.mark.parametrize("value", [False, "true", 1, None])
def test_explicit_boolean_consent_required_for_every_attestation(store, field, value):
    _, pid = prepare(store)
    doc = approval(store, pid)
    doc[field] = value
    with pytest.raises(ValueError, match="Explicit"):
        c.approve(store, doc)
    assert c._get(store, pid)[0]["state"] == "pending"


def test_approval_bound_to_exact_proposal_and_revision(store, monkeypatch):
    cid, pid = prepare(store)
    doc = approval(store, pid)
    with pytest.raises(ValueError, match="does not match"):
        c.approve(store, {**doc, "snapshot_hash": "wrong"})
    b = bundle(store, pid, monkeypatch)
    assert b["example"]["contribution_id"] == pid
    page = store.root / "contributions" / (pid + ".html")
    exported = store.root / "contributions" / (pid + ".bundle.json")
    assert page.exists() and exported.exists()
    saved = store.get(cid)
    store.import_corrections({"capture_id": cid, "pair_hash": saved["pair_hash"], "revision": saved["revision"],
                              "policy": POLICY, "annotations": saved["annotations"]})
    assert not exported.exists() and not page.exists()
    with pytest.raises(ValueError, match="Labels changed"):
        c.approve(store, doc)
    with pytest.raises(ValueError, match="Labels changed"):
        c.export_bundle(store, pid)


def test_no_original_or_provenance_in_bundle(store, monkeypatch):
    _, pid = prepare(store)
    b = bundle(store, pid, monkeypatch)
    encoded = json.dumps(b)
    for forbidden in ("alice@private.invalid", "private note", "source-person", "Fictional unit test", "pair_hash", "snapshot_hash", "/messages/", str(store.root)):
        assert forbidden not in encoded
    assert b["example"]["annotation_scope"] == "selected-spans-not-exhaustive-NER"


def test_cli_collect_does_not_require_or_create_an_original_capture_store(store, monkeypatch, capsys):
    from clawjournal.cli_redaction_eval import register_parser, run
    _, pid = prepare(store)
    incoming = bundle(store, pid, monkeypatch)
    file = store.root / "incoming.json"
    file.write_text(json.dumps(incoming))
    absent_store = store.root / "must-not-be-created"
    parser = ArgumentParser()
    register_parser(parser.add_subparsers())
    args = parser.parse_args(["redaction-eval", "--store", str(absent_store),
        "contribute", "collect", str(file), "--collection", str(store.root / "collection"),
        "--contributor-id", str(uuid.uuid4())])
    run(args)
    assert json.loads(capsys.readouterr().out)["status"] == "collected"
    assert not absent_store.exists()


def test_withdrawal_is_terminal_and_deletes_managed_files(store, monkeypatch):
    _, pid = prepare(store)
    doc = approval(store, pid)
    bundle(store, pid, monkeypatch)
    c.withdraw(store, pid)
    assert not (store.root / "contributions" / (pid + ".bundle.json")).exists()
    with pytest.raises(ValueError, match="withdrawn"):
        c.approve(store, doc)
    with pytest.raises(ValueError, match="withdrawn"):
        c.export_bundle(store, pid)


def test_withdrawal_during_scanning_cannot_create_bundle(store, monkeypatch):
    _, pid = prepare(store)
    c.approve(store, approval(store, pid))
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: c.withdraw(store, pid))
    with pytest.raises(ValueError, match="withdrawn"):
        c.export_bundle(store, pid)
    assert not (store.root / "contributions" / (pid + ".bundle.json")).exists()


def test_retention_removes_contribution_pages_bundles_and_records(store, monkeypatch):
    cid, pid = prepare(store)
    bundle(store, pid, monkeypatch)
    store.conn.execute("UPDATE captures SET expires=0 WHERE id=?", (cid,)); store.conn.commit()
    assert store.prune() == 1
    assert list((store.root / "contributions").iterdir()) == []
    assert store.conn.execute("SELECT COUNT(*) FROM contribution_proposals").fetchone()[0] == 0


def test_requires_reviewed_labels_and_does_not_cut_entities(store):
    cid = capture(store, status="assistant_draft")
    with pytest.raises(ValueError, match="human-reviewed"):
        c.prepare(store, cid, "/messages/0/content", 0, len(TEXT))
    cid = capture(store, group="source-person")
    with pytest.raises(ValueError, match="cuts through"):
        c.prepare(store, cid, "/messages/0/content", TEXT.index("alice") + 2, len(TEXT))


def test_unlabelled_possible_identifiers_block_preparation(store):
    text = TEXT + " Also send to bob@private.invalid"
    cid = capture(store, text=text)
    with pytest.raises(ValueError, match="unlabelled"):
        c.prepare(store, cid, "/messages/0/content", 0, len(text))


def test_profile_binds_contributor_across_stores_and_refuses_rotation(store):
    _, pid = prepare(store)
    profile = c._get(store, pid)[2]["contributor_id"]
    assert c.contributor_profile("dev")["contributor"] == profile
    with pytest.raises(ValueError, match="different split"):
        c.contributor_profile("train")
    with pytest.raises(ValueError, match="different contributor"):
        c.contributor_profile("dev", str(uuid.uuid4()))


def test_review_escapes_source_and_leaves_consent_unchecked(store):
    text = TEXT + ' </script><script>alert("bad")</script>'
    cid, pid = prepare(store, text=text)
    page = c.review(store, pid).read_text()
    assert '<script>alert("bad")' not in page
    assert "\\u003c/script>" in page
    assert "connect-src 'none'" in page
    assert '<button id="approve" disabled>' in page
    assert 'type="checkbox" checked' not in page
    assert "innerHTML" not in page


def reidentify(b, *, split=None, text_suffix=""):
    b = copy.deepcopy(b)
    b["example"]["contribution_id"] = str(uuid.uuid4())
    b["example"]["contributor_id"] = str(uuid.uuid4())
    if split:
        b["example"]["split"] = split
    b["example"]["text"] += text_suffix
    b["approval"]["payload_sha256"] = digest(b["example"])
    return b


def test_collection_is_idempotent_and_keeps_each_contributor_in_one_split(store, tmp_path, monkeypatch):
    _, pid = prepare(store)
    b = bundle(store, pid, monkeypatch)
    directory, assigned = tmp_path / "collection", str(uuid.uuid4())
    assert c.collect(directory, b, assigned)["status"] == "collected"
    assert c.collect(directory, b, assigned)["status"] == "already-collected"
    with pytest.raises(ValueError, match="Contributor split conflict"):
        c.collect(directory, reidentify(b, split="test", text_suffix=" A different context."), assigned)
    with pytest.raises(ValueError, match="another assigned contributor"):
        c.collect(directory, b, str(uuid.uuid4()))
    conn = sqlite3.connect(directory / "collection.db")
    assert conn.execute("SELECT COUNT(*) FROM examples").fetchone()[0] == 1
    conn.close()


def test_equivalent_snippets_cannot_leak_across_splits(store, tmp_path, monkeypatch):
    _, pid = prepare(store)
    b = bundle(store, pid, monkeypatch)
    directory = tmp_path / "collection"
    c.collect(directory, b, str(uuid.uuid4()))
    with pytest.raises(ValueError, match="cross dataset splits"):
        c.collect(directory, reidentify(b, split="test"), str(uuid.uuid4()))
    _, second_pid = prepare(store)
    different_substitutes = bundle(store, second_pid, monkeypatch)
    different_substitutes = reidentify(different_substitutes, split="test")
    with pytest.raises(ValueError, match="cross dataset splits"):
        c.collect(directory, different_substitutes, str(uuid.uuid4()))


def test_bundle_rejects_extra_fields_tampering_and_actual_identifiers(store, monkeypatch):
    _, pid = prepare(store)
    b = bundle(store, pid, monkeypatch)
    altered = copy.deepcopy(b); altered["example"]["original"] = TEXT
    with pytest.raises(ValueError, match="schema"):
        c.validate_bundle(altered)
    altered = copy.deepcopy(b); altered["example"]["text"] += "tampered"
    with pytest.raises(ValueError, match="approval"):
        c.validate_bundle(altered)
    altered = copy.deepcopy(b)
    label = altered["example"]["labels"][0]
    start, end = label["start"], label["end"]
    value = "z" * (end - start)
    altered["example"]["text"] = altered["example"]["text"][:start] + value + altered["example"]["text"][end:]
    altered["approval"]["payload_sha256"] = digest(altered["example"])
    with pytest.raises(ValueError, match="invented"):
        c.validate_bundle(altered)


def test_export_requires_both_real_scanners_no_bypass(store):
    _, pid = prepare(store)
    c.approve(store, approval(store, pid))
    with pytest.raises(ValueError, match="bypass"):
        c.export_bundle(store, pid)  # test suite bypass flags are intentionally rejected


@pytest.mark.parametrize("outcome", ["block", "rewrite", "bypass", "missing"])
def test_gate_failure_or_rewrite_never_exports(store, monkeypatch, outcome):
    from types import SimpleNamespace
    from clawjournal.redaction import betterleaks, trufflehog
    from clawjournal.workbench import share_gate
    _, pid = prepare(store)
    c.approve(store, approval(store, pid))
    monkeypatch.setattr(betterleaks, "is_bypassed", lambda: False)
    monkeypatch.setattr(trufflehog, "is_bypassed", lambda: False)
    monkeypatch.setattr(betterleaks, "scan_text", lambda text: SimpleNamespace(binary_missing=False, scan_error=None, bypassed=False, findings=[]))
    def gate(path, manifest, **kwargs):
        if outcome == "rewrite":
            path.write_text("changed")
        return SimpleNamespace(bypassed=outcome == "bypass", blocking=outcome in ("block", "missing"), findings=[], gate_redactions=0)
    monkeypatch.setattr(share_gate, "run_share_gate", gate)
    with pytest.raises(ValueError, match="gate failed"):
        c.export_bundle(store, pid)
    assert not (store.root / "contributions" / (pid + ".bundle.json")).exists()
    assert not list((store.root / "contributions").glob(".contribution-scan-*"))


def permission_document(store):
    ongoing.settings_page(store)
    nonce = store.conn.execute("SELECT nonce FROM contribution_permission WHERE id=1").fetchone()[0]
    return {"nonce": nonce, "version": ongoing.VERSION, "scope": ongoing.SCOPE,
            "purpose": c.PURPOSE, "consent": True, "rights": True, "accept_automated_review": True}


def enable_ongoing(store):
    return ongoing.import_permission(store, permission_document(store))


def test_ongoing_defaults_off_and_opening_settings_never_enables_it(store):
    assert ongoing.status(store)["mode"] == "per-snippet"
    page = ongoing.settings_page(store).read_text()
    assert 'id="enable" disabled' in page
    assert "connect-src 'none'" in page
    assert ongoing.status(store)["mode"] == "per-snippet"
    with pytest.raises(ValueError, match="permission is off"):
        ongoing.process(store)


@pytest.mark.parametrize("field", ["consent", "rights", "accept_automated_review"])
@pytest.mark.parametrize("value", [False, 1, "true", None])
def test_ongoing_requires_explicit_acceptance(store, field, value):
    doc = permission_document(store)
    doc[field] = value
    with pytest.raises(ValueError, match="Explicit"):
        ongoing.import_permission(store, doc)
    assert ongoing.status(store)["mode"] == "per-snippet"


def test_permission_is_store_scoped_versioned_and_not_replayable(store, tmp_path):
    doc = permission_document(store)
    other = Store(tmp_path / "other", initialize=True)
    try:
        ongoing.settings_page(other)
        with pytest.raises(ValueError, match="different store"):
            ongoing.import_permission(other, doc)
    finally:
        other.close()
    with pytest.raises(ValueError, match="Explicit"):
        ongoing.import_permission(store, {**doc, "version": "outdated"})
    ongoing.import_permission(store, doc)
    ongoing.disable(store)
    with pytest.raises(ValueError, match="stale"):
        ongoing.import_permission(store, doc)


def test_only_future_captures_covered_not_old_drafts(store):
    cid = capture(store, status="assistant_draft")
    enable_ongoing(store)
    with pytest.raises(ValueError, match="only new"):
        c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    assert ongoing.process(store)["results"] == []


def test_ongoing_exports_drafts_without_claiming_human_review(store, monkeypatch):
    enable_ongoing(store)
    cid = capture(store, status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: None)
    b = json.loads(c.export_bundle(store, pid).read_text())
    assert c.validate_bundle(b)["context_review_status"] == "unreviewed"
    assert all(a["review_status"] == "assistant_draft" for a in b["example"]["labels"])
    assert b["approval"]["consent"] is True
    assert b["approval"]["reviewed_context"] is False
    assert b["approval"]["reviewed_labels"] is False
    assert "alice@private.invalid" not in json.dumps(b)
    assert all(a["review_status"] == "assistant_draft" for a in store.get(cid)["annotations"])
    with pytest.raises(ValueError, match="remain unreviewed"):
        c.approve(store, approval(store, pid))
    for field in ("reviewed_context", "reviewed_labels"):
        altered = copy.deepcopy(b); altered["approval"][field] = True
        with pytest.raises(ValueError, match="not human review"):
            c.validate_bundle(altered)
    assigned = str(uuid.uuid4())
    assert c.collect(store.root / "collection", b, assigned)["status"] == "collected"
    conn = sqlite3.connect(store.root / "collection" / "collection.db")
    collected = json.loads(conn.execute("SELECT payload FROM examples").fetchone()[0])
    conn.close()
    assert collected == b
    # Existing benchmark excludes draft labels, regardless of contribution consent.
    from clawjournal.redaction.evaluation import evaluate
    with pytest.raises(ValueError, match="No eligible labels"):
        evaluate(store, lambda text: [], {"engine": "fixture"}, split="dev")


def test_disable_deletes_automatic_bundles_and_reenable_cannot_revive_them(store, monkeypatch):
    _, manual = prepare(store)
    manual_path = store.root / "contributions" / (manual + ".bundle.json")
    bundle(store, manual, monkeypatch)
    enable_ongoing(store)
    cid = capture(store, text=TEXT + " New future capture.", status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    auto_path = c.export_bundle(store, pid)
    ongoing.disable(store)
    assert not auto_path.exists()
    assert manual_path.exists()
    with pytest.raises(ValueError, match="off"):
        c.export_bundle(store, pid)
    enable_ongoing(store)
    with pytest.raises(ValueError, match="permission changed"):
        c.export_bundle(store, pid)


def test_disable_during_scan_prevents_automatic_export(store, monkeypatch):
    enable_ongoing(store)
    cid = capture(store, status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: ongoing.disable(store))
    with pytest.raises(ValueError, match="off"):
        c.export_bundle(store, pid)
    assert not (store.root / "contributions" / (pid + ".bundle.json")).exists()


def test_automatic_still_requires_scanners_and_resolved_candidates(store):
    enable_ongoing(store)
    cid = capture(store, status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    with pytest.raises(ValueError, match="bypass"):
        c.export_bundle(store, pid)
    saved = store.get(cid)
    saved["annotations"][0]["decision"] = "uncertain"
    store.import_corrections({"capture_id": cid, "pair_hash": saved["pair_hash"], "revision": saved["revision"],
                             "policy": POLICY, "annotations": saved["annotations"]})
    with pytest.raises(ValueError, match="resolved"):
        c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    cid = capture(store, text=TEXT + " Additional unlabelled: other@private.invalid", status="assistant_draft")
    with pytest.raises(ValueError, match="unlabelled"):
        c.prepare(store, cid, "/messages/0/content", 0, len(TEXT) + 45, automatic=True)


def test_batch_is_bounded_repeatable_and_does_not_recreate_withdrawals(store, monkeypatch):
    enable_ongoing(store)
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: None)
    for i in range(4):
        capture(store, text=TEXT + f" Experiment {i}.", status="assistant_draft")
    result = ongoing.process(store, limit=2)
    assert len(result["results"]) == 2
    assert all(row["status"] == "exported-unreviewed" for row in result["results"])
    c.withdraw(store, result["results"][0]["proposal_id"])
    result2 = ongoing.process(store, limit=5)
    assert len(result2["results"]) == 2
    assert ongoing.process(store)["results"] == []
    with pytest.raises(ValueError, match="between 1 and 5"):
        ongoing.process(store, 10)


def test_held_windows_cannot_starve_later_captures(store, monkeypatch):
    enable_ongoing(store)
    def scan(b, d):
        if "Blocked" in b["example"]["text"]:
            raise ValueError("Contribution secret scan failed or found possible secrets; revise the snippet")
    monkeypatch.setattr(c, "_scan_candidate", scan)
    for i in range(5):
        capture(store, text=TEXT + f" Blocked {i}.", status="assistant_draft")
    assert [r["status"] for r in ongoing.process(store)["results"]] == ["held-for-review"] * 5
    fresh = capture(store, text=TEXT + " Fresh capture.", status="assistant_draft")
    first = ongoing.process(store)["results"][0]
    assert first["status"] == "exported-unreviewed"
    assert c._get(store, first["proposal_id"])[0]["capture_id"] == fresh
    # Held windows still retry after fresh ones, so a cleared failure recovers.
    monkeypatch.setattr(c, "_scan_candidate", lambda b, d: None)
    assert [r["status"] for r in ongoing.process(store)["results"]] == ["exported-unreviewed"] * 5
    assert ongoing.process(store)["results"] == []


def test_automatic_draft_cannot_override_detected_pii_as_preserve(store):
    enable_ongoing(store)
    text = "Contact: +1-212-555-0174"
    cid = store.capture({"messages": [{"role": "user", "content": text}]}, group="fixture",
        split="dev", source_kind="synthetic", provenance="Test fixture", preview=lambda s: (s, {}))
    saved = store.get(cid)
    doc = {"capture_id": cid, "pair_hash": saved["pair_hash"], "revision": saved["revision"],
        "policy": POLICY, "annotations": [{"unit": "/messages/0/content", "start": 9, "end": len(text),
        "decision": "preserve", "entity_type": "measurement", "review_status": "assistant_draft"}]}
    store.import_corrections(doc)
    with pytest.raises(ValueError, match="ambiguous match"):
        c.prepare(store, cid, "/messages/0/content", 0, len(text), automatic=True)


def test_ongoing_version_change_requires_fresh_consent(store, monkeypatch):
    enable_ongoing(store)
    cid = capture(store, status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    monkeypatch.setattr(ongoing, "VERSION", "a-new-purpose-version")
    assert ongoing.status(store)["mode"] == "per-snippet"
    with pytest.raises(ValueError, match="outdated"):
        c.export_bundle(store, pid)


def test_cli_ongoing_preferences_round_trip(store, capsys):
    from clawjournal.cli_redaction_eval import register_parser, run
    parser = ArgumentParser()
    register_parser(parser.add_subparsers())
    def invoke(*words):
        run(parser.parse_args(["redaction-eval", "--store", str(store.root), "contribute", *words]))
        return json.loads(capsys.readouterr().out)
    assert invoke("consent", "status")["mode"] == "per-snippet"
    assert Path(invoke("settings")["settings_file"]).is_file()
    doc = permission_document(store)
    path = store.root / "permission.json"
    path.write_text(json.dumps(doc))
    assert invoke("consent", "import", str(path))["mode"] == "ongoing"
    assert invoke("process") == {"results": [], "uploaded": False}
    assert invoke("consent", "disable")["mode"] == "per-snippet"


def test_revocation_from_another_connection_during_scan_wins(store, monkeypatch):
    enable_ongoing(store)
    cid = capture(store, status="assistant_draft")
    pid, _ = c.prepare(store, cid, "/messages/0/content", 0, len(TEXT), automatic=True)
    other = Store(store.root)
    try:
        monkeypatch.setattr(c, "_scan_candidate", lambda b, d: ongoing.disable(other))
        with pytest.raises(ValueError, match="off"):
            c.export_bundle(store, pid)
    finally:
        other.close()
    assert not (store.root / "contributions" / (pid + ".bundle.json")).exists()
