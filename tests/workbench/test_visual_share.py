import base64
import copy
import hashlib
from io import BytesIO
import json
import zipfile

import pytest

from clawjournal import attachments, attachment_visuals as visual, visual_assets
from clawjournal.workbench import index, review_snapshots, visual_share, share_gate
from clawjournal.redaction.scan_policy import GateReport

SETTINGS = {"custom_strings": [], "allowlist_entries": [], "extra_usernames": [], "blocked_domains": []}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(index, "CONFIG_DIR", tmp_path)
    with index.open_index() as db:
        yield db


@pytest.fixture
def example(conn):
    pytest.importorskip("PIL", minversion="13.0.0")
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (400, 200), "white").save(output, format="PNG")
    raw = {"session_id": "visual-share-test", "project": "invented", "source": "custom",
           "messages": [{"role": "user", "content": "Explain this plot", "extra": {"attachments": [
               {"file_name": "results.png", "mime_type": "image/png", "file_data": base64.b64encode(output.getvalue()).decode()}
           ]}}]}
    index.upsert_sessions(conn, [raw])
    detail = index.get_session_detail(conn, raw["session_id"])
    identity = attachments.inventory(detail)[0]["id"]
    page = visual._page(0, output.getvalue())
    result = visual.RedactedPage(page.digest, page.png, "loss=0.125 lambda=1", (), ("OCR is partial.",))
    scope = (raw["session_id"], detail["content_revision"], identity, visual_share.profile_hash(SETTINGS))
    approval = visual_share.approval_payload(scope, "results.png", (page,), {0: {"result": result}}, "invented scanner test")
    return detail, identity, approval, page


def test_visual_snapshot_exports_separate_exact_pages_and_final_zip_binds_them(conn, example):
    from clawjournal.workbench.daemon import _finalized_share_zip
    detail, identity, approval, page = example
    snapshot = review_snapshots.save_review_snapshot(conn, detail, attachment_ids=[identity], visual_attachments={identity: approval})
    share = index.create_share(conn, [detail["session_id"]], review_snapshot_ids={detail["session_id"]: snapshot})
    folder, manifest = index.export_share_to_disk(conn, share, index.get_share(conn, share))
    assert not manifest.get("blocked"), manifest
    sessions = [json.loads((folder / "sessions.jsonl").read_text())]
    record = sessions[0]["messages"][0]["extra"]["attachment_review"]["items"][0]
    assert record["message_index"] == 0 and record["pages"][0]["text"] == "loss=0.125 lambda=1"
    assert "png_base64" not in json.dumps(sessions) and "_review_visual" not in json.dumps(sessions)
    asset = folder / record["pages"][0]["asset_path"]
    assert asset.read_bytes() == page.png
    for name in ("trufflehog.post-pii.json", "secret-scan.post-pii.json"):
        (folder / name).write_text('{}')
    path, _ = _finalized_share_zip(folder)
    with zipfile.ZipFile(path) as archive:
        assert archive.read(record["pages"][0]["asset_path"]) == page.png
        assert not any("original" in n for n in archive.namelist())
    # Even a same-size, metadata-only alteration must invalidate the asset.
    asset.write_bytes(page.png[:-1] + bytes([page.png[-1] ^ 1]))
    with pytest.raises(visual_assets.VisualAssetError):
        _finalized_share_zip(folder)


def test_imported_approval_fields_cannot_authorize_visual_export(conn, example):
    detail, identity, approval, _page = example
    detail.update(_review_attachment_ids=[identity], _review_visual_attachments={identity: approval})
    snapshot = review_snapshots.save_review_snapshot(conn, detail)
    saved = review_snapshots.load_review_snapshot(conn, snapshot, detail["session_id"])
    assert saved["_review_visual_attachments"] == {} and saved["_review_attachment_ids"] == []


def test_completed_local_copy_preserves_reviewed_pages_and_refuses_missing_assets(conn, example, tmp_path):
    detail, identity, approval, page = example
    snapshot = review_snapshots.save_review_snapshot(conn, detail, attachment_ids=[identity], visual_attachments={identity: approval})
    share = index.create_share(conn, [detail["session_id"]], review_snapshot_ids={detail["session_id"]: snapshot})
    folder, manifest = index.export_share_to_disk(conn, share, index.get_share(conn, share))
    payload_hash = hashlib.sha256((folder / "sessions.jsonl").read_bytes()).hexdigest()
    conn.execute("UPDATE shares SET shared_at='2026-10-22', bundle_hash=?, manifest=? WHERE share_id=?",
                 (payload_hash, json.dumps(manifest), share))
    destination, copied = review_snapshots.copy_completed_share(conn, share, str(tmp_path / "copy"))
    assert not copied.get("blocked"), copied
    assert visual_share.verified_bundle_assets(destination) == {f"attachments/{page.digest}.png": page.png}
    (folder / f"attachments/{page.digest}.png").unlink()
    _destination, blocked = review_snapshots.copy_completed_share(conn, share, str(tmp_path / "missing"))
    assert blocked["block_reason"] == "completed_artifact_unavailable"
    assert not (tmp_path / "missing" / "sessions.jsonl").exists()


def test_changed_settings_and_changed_bytes_require_new_approval(conn, example):
    detail, identity, approval, _page = example
    with pytest.raises(attachments.AttachmentSelectionError, match="settings changed"):
        index.apply_share_redactions(conn, detail, attachment_ids=[identity], visual_attachments={identity: approval}, custom_strings=["loss"])
    bad = copy.deepcopy(approval); bad["pages"][0]["text"] = "alice@audit.test"
    with pytest.raises(attachments.AttachmentSelectionError, match="changed approved"):
        index.apply_share_redactions(conn, detail, attachment_ids=[identity], visual_attachments={identity: bad})
    bad = copy.deepcopy(approval); bad["pages"][0]["sha256"] = "0" * 64
    with pytest.raises(attachments.AttachmentSelectionError, match="bytes changed"):
        attachments.prepare(detail, [identity], visual_attachments={identity: bad})


def test_final_scan_text_rewrite_blocks_pixels_from_the_older_review(conn, example, monkeypatch):
    detail, identity, approval, _page = example
    snapshot = review_snapshots.save_review_snapshot(conn, detail, attachment_ids=[identity], visual_attachments={identity: approval})
    share = index.create_share(conn, [detail["session_id"]], review_snapshot_ids={detail["session_id"]: snapshot})
    def rewrite(path, manifest, **kwargs):
        path.write_text(path.read_text().replace("loss=0.125", "[REDACTED]"))
        return GateReport(str(path), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), engine="invented")
    monkeypatch.setattr(share_gate, "run_share_gate", rewrite)
    _folder, manifest = index.export_share_to_disk(conn, share, index.get_share(conn, share))
    assert manifest["blocked"] and manifest["block_reason"] == "attachment_review_changed"


def test_full_privacy_check_refuses_scanner_bypass_and_uses_export_redaction(conn, monkeypatch):
    with pytest.raises(attachments.AttachmentSelectionError, match="scanner-bypassed"):
        visual_share.check_texts(conn, "visual-test", "figure.png", ["alice@audit.test; loss=0.125"], SETTINGS)
    def checked(path, manifest, **kwargs):
        return GateReport(str(path), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest(), engine="invented scanner test")
    monkeypatch.setattr(share_gate, "run_share_gate", checked)
    name, texts, _engine = visual_share.check_texts(conn, "visual-test", "figure.png", ["alice@audit.test; loss=0.125"], SETTINGS)
    assert name == "figure.png" and "alice" not in texts[0] and "loss=0.125" in texts[0]


def test_additional_full_check_redaction_changes_pixels_then_rescans_to_stability(conn, example, monkeypatch):
    from PIL import Image
    _detail, _identity, _approval, page = example
    ocr = visual.OcrResult(page.digest, "alice@audit.test loss=0.125", (
        visual.Word("alice@audit.test", 0, 16, (10, 10, 180, 30), 99),
        visual.Word("loss=0.125", 17, 27, (10, 40, 180, 60), 99)), ())
    def recognize(p):
        with Image.open(BytesIO(p.png)) as image:
            if image.getpixel((10, 10)) == (0, 0, 0):
                return visual.OcrResult(p.digest, "loss=0.125", (visual.Word("loss=0.125", 0, 10, (10, 40, 180, 60), 99),), ())
        return visual.OcrResult(p.digest, ocr.text, ocr.words, ())
    monkeypatch.setattr(visual, "recognize_text", recognize)
    monkeypatch.setattr(visual_share, "check_texts", lambda conn, sid, name, texts, settings:
        (name, [t.replace("alice@audit.test", "[REDACTED]") for t in texts], "invented"))
    scans = {0: {"ocr": ocr, "result": visual.redact_page(page, ocr)}}
    _name, results, _engine = visual_share.prepare_pages(conn, "visual-test", "figure.png", (page,), scans, SETTINGS)
    assert "alice" not in results[0].text and "loss=0.125" in results[0].text
    with Image.open(BytesIO(results[0].png)) as image:
        assert image.getpixel((10, 10)) == (0, 0, 0)
        assert image.getpixel((10, 40)) == (255, 255, 255)
