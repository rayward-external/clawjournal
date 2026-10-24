"""Attachment choices must govern the bytes that leave the machine."""
import base64
import copy
import json
from io import BytesIO

import pytest

from clawjournal import attachments
from clawjournal.workbench import index, review_snapshots


def image_library():
    pytest.importorskip("PIL", minversion="13.0.0")
    from PIL import Image
    return Image


def image_trace():
    Image = image_library()
    from PIL import PngImagePlugin
    output = BytesIO()
    metadata = PngImagePlugin.PngInfo()
    metadata.add_text("Comment", "PRIVATE_IMAGE_METADATA")
    Image.new("RGB", (8, 4), "navy").save(output, format="PNG", pnginfo=metadata)
    return {"messages": [{"role": "user", "content": "Inspect this plot.", "extra": {"attachments": [
        {"file_name": "private-plot.png", "mime_type": "image/png",
         "file_data": base64.b64encode(output.getvalue()).decode()}
    ]}}]}


def test_image_preview_is_local_metadata_free_and_does_not_allow_sharing():
    Image = image_library()
    raw = image_trace()
    choice = attachments.inventory(raw)[0]
    assert choice["can_preview_image"] and not choice["can_include"]
    preview = attachments.image_preview(raw, choice["id"])
    assert b"PRIVATE_IMAGE_METADATA" not in preview
    with Image.open(BytesIO(preview)) as image:
        assert image.size == (8, 4) and not image.info
        assert image.getpixel((0, 0)) == (0, 0, 128, 255)
    with pytest.raises(attachments.AttachmentSelectionError):
        attachments.prepare(raw, [choice["id"]])
    assert "file_data" not in json.dumps(attachments.prepare(raw))


def test_image_preview_rejects_changed_corrupt_oversized_and_animated_sources(monkeypatch):
    Image = image_library()
    raw = image_trace()
    choice = attachments.inventory(raw)[0]
    with pytest.raises(attachments.AttachmentSelectionError, match="changed"):
        attachments.image_preview(raw, "old-id")
    with monkeypatch.context() as patch:
        patch.setattr(attachments, "MAX_IMAGE_BYTES", 10)
        with pytest.raises(attachments.AttachmentSelectionError, match="file limit"):
            attachments.image_preview(raw, choice["id"])
    with monkeypatch.context() as patch:
        patch.setattr(attachments, "MAX_IMAGE_PIXELS", 10)
        with pytest.raises(attachments.AttachmentSelectionError, match="megapixel"):
            attachments.image_preview(raw, choice["id"])
    item = raw["messages"][0]["extra"]["attachments"][0]
    item["file_data"] = base64.b64encode(b"<html>not an image</html>").decode()
    with pytest.raises(attachments.AttachmentSelectionError, match="decoded safely"):
        attachments.image_preview(raw, attachments.inventory(raw)[0]["id"])
    output = BytesIO()
    Image.new("RGB", (8, 4), "red").save(output, format="PNG", save_all=True,
                                       append_images=[Image.new("RGB", (8, 4), "blue")])
    item["file_data"] = base64.b64encode(output.getvalue()).decode()
    with pytest.raises(attachments.AttachmentSelectionError, match="Animated"):
        attachments.image_preview(raw, attachments.inventory(raw)[0]["id"])


def test_image_reference_never_becomes_a_preview():
    raw = {"messages": [{"role": "user", "content": [{"type": "image", "mime_type": "image/png",
                                                            "url": "https://example.invalid/private.png"}]}]}
    choice = attachments.inventory(raw)[0]
    assert choice["availability"] == "reference_only" and not choice["can_preview_image"]
    with pytest.raises(attachments.AttachmentSelectionError):
        attachments.image_preview(raw, choice["id"])


@pytest.mark.parametrize("format,mime", [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("WEBP", "image/webp")])
def test_supported_image_formats_render_as_scaled_png(format, mime):
    Image = image_library()
    raw = image_trace()
    output = BytesIO()
    Image.new("RGB", (3000, 1500), "navy").save(output, format=format)
    item = raw["messages"][0]["extra"]["attachments"][0]
    item.update(mime_type=mime, file_data=base64.b64encode(output.getvalue()).decode())
    preview = attachments.image_preview(raw, attachments.inventory(raw)[0]["id"])
    with Image.open(BytesIO(preview)) as image:
        assert image.format == "PNG" and image.size == (2048, 1024)


def test_image_preview_dependency_failure_is_actionable(monkeypatch):
    import sys
    raw = image_trace()
    choice = attachments.inventory(raw)[0]
    import PIL
    with monkeypatch.context() as patch:
        patch.setattr(PIL, "__version__", "12.3.0")
        with pytest.raises(attachments.AttachmentSelectionError, match="Pillow 13"):
            attachments.image_preview(raw, choice["id"])
    monkeypatch.setitem(sys.modules, "PIL", None)
    with pytest.raises(attachments.AttachmentSelectionError, match="Update ClawJournal"):
        attachments.image_preview(raw, choice["id"])


def test_text_from_visual_files_preserves_the_visual_context_gap_on_reimport():
    raw = {"messages": [{"role": "user", "extra": {"attachments": [
        {"file_name": "paper.pdf", "extracted_content": "Results: loss=0.125"}
    ]}}]}
    choice = attachments.inventory(raw)[0]
    assert choice["visual_context_missing"]
    prepared = attachments.prepare(raw, [choice["id"]])
    record = prepared["messages"][0]["extra"]["attachment_review"]["items"][0]
    assert record["visual_context_missing"] is True
    choice = attachments.inventory(prepared)[0]
    restored = attachments.prepare(prepared, [choice["id"]])
    assert restored["messages"][0]["extra"]["attachment_review"]["items"][0]["visual_context_missing"] is True


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(index, "INDEX_DB", tmp_path / "index.db")
    monkeypatch.setattr(index, "BLOBS_DIR", tmp_path / "blobs")
    monkeypatch.setattr(index, "CONFIG_DIR", tmp_path)
    db = index.open_index()
    yield db
    db.close()


def trace():
    return {"session_id": "attachment-test", "project": "synthetic", "source": "custom",
            "messages": [{"role": "user", "content": "Compare the attached results.", "extra": {
                "attachments": [{"file_name": "results.txt", "extracted_content": "loss=0.125; contact alice@audit.test"}],
                "files": [{"file_name": "results.txt", "file_uuid": "file-one"},
                          {"file_name": "private-image.png", "file_uuid": "file-two"}],
                "exported_content": [{"type": "text", "text": "private duplicate"}],
                "exported_text": "private duplicate",
            }}], "stats": {"user_messages": 1}}


def test_inventory_merges_alias_and_does_not_claim_references_are_files():
    items = attachments.inventory(trace())
    assert len(items) == 2
    assert items[0]["availability"] == "extracted_text" and items[0]["can_include"]
    assert items[1]["availability"] == "reference_only" and not items[1]["can_include"]
    assert items[0]["message_index"] == 0


def test_reimported_visual_references_offer_ocr_without_claiming_original_pixels_or_known_origin():
    raw = {"messages": [{"role": "user", "content": "Compare results.", "extra": {
        "attachment_review": {"schema_version": "clawjournal.attachments.v2", "items": [{
            "attachment_id": "attachment-1", "message_index": None, "availability": "original_file",
            "status": "included", "name": "results.png", "pages": [{
                "asset_path": "attachments/" + "0" * 64 + ".png", "text": "loss=0.125"}]}]}}}]}
    items = attachments.inventory(raw)
    assert len(items) == 1 and items[0]["availability"] == "extracted_text"
    assert items[0]["message_index"] is None and items[0]["visual_context_missing"]
    assert not items[0]["can_review_visual"]
    assert "loss=0.125" not in json.dumps(attachments.prepare(raw))
    included = attachments.prepare(raw, [items[0]["id"]])
    record = included["messages"][0]["extra"]["attachment_review"]["items"][0]
    assert record["message_index"] is None and record["visual_context_missing"] is True
    assert "loss=0.125" in record["text"] and "pages" not in record


def test_inline_document_text_requires_explicit_selection():
    raw = {"messages": [{"role": "user", "content": [
        {"type": "document", "text": "PRIVATE_DOCUMENT_TEXT"},
        {"type": "text", "text": "Ordinary conversation."},
    ]}]}
    choices = attachments.inventory(raw)
    assert len(choices) == 1 and choices[0]["can_include"]
    assert choices[0]["availability"] == "extracted_text"
    excluded = json.dumps(attachments.prepare(raw))
    assert "PRIVATE_DOCUMENT_TEXT" not in excluded
    assert "Ordinary conversation." in excluded
    selected = attachments.prepare(raw, [choices[0]["id"]])
    assert selected["messages"][0]["extra"]["attachment_review"]["items"][0]["text"] == "PRIVATE_DOCUMENT_TEXT"


def test_exclusion_removes_text_aliases_names_and_ids_without_mutating_original():
    raw = trace()
    before = copy.deepcopy(raw)
    prepared = attachments.prepare(raw)
    encoded = json.dumps(prepared)
    for private in ("loss=0.125", "alice@audit.test", "results.txt", "private-image.png", "file-one", "private duplicate"):
        assert private not in encoded
    assert raw == before
    items = prepared["messages"][0]["extra"]["attachment_review"]["items"]
    assert [i["status"] for i in items] == ["excluded", "unavailable"]


def test_changed_or_unavailable_content_cannot_be_selected():
    raw = trace()
    inventory = attachments.inventory(raw)
    with pytest.raises(attachments.AttachmentSelectionError):
        attachments.prepare(raw, [inventory[1]["id"]])
    raw["messages"][0]["extra"]["attachments"][0]["extracted_content"] += " changed"
    with pytest.raises(attachments.AttachmentSelectionError):
        attachments.prepare(raw, [inventory[0]["id"]])


def test_embedded_original_text_is_decoded_but_binary_and_external_paths_are_not_read():
    raw = {"messages": [{"role": "user", "content": [
        {"type": "file", "file_type": "text/plain", "file_data": base64.b64encode(b"lambda=1").decode()},
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"}},
        {"type": "file", "file_name": "/etc/passwd", "url": "https://example.invalid/private"},
    ]}]}
    items = attachments.inventory(raw)
    assert [i["availability"] for i in items] == ["original_file", "original_file", "reference_only"]
    assert [i["can_include"] for i in items] == [True, False, False]
    out = attachments.prepare(raw, [items[0]["id"]])
    assert "lambda=1" in json.dumps(out)
    assert "QUJD" not in json.dumps(out) and "/etc/passwd" not in json.dumps(out)


def test_selected_text_runs_through_existing_scanners_and_custom_rules(conn):
    raw = trace()
    raw["messages"][0]["extra"]["attachments"][0]["extracted_content"] += " secret-lab-value"
    selected = [attachments.inventory(raw)[0]["id"]]
    out, count, _ = index.apply_share_redactions(conn, raw, attachment_ids=selected,
                                               custom_strings=["secret-lab-value"])
    encoded = json.dumps(out)
    assert "loss=0.125" in encoded
    assert "alice@audit.test" not in encoded and "secret-lab-value" not in encoded
    assert count >= 2
    assert "private-image.png" not in encoded


def test_review_snapshot_pins_choices_and_packaging_uses_them(conn):
    raw = trace()
    index.upsert_sessions(conn, [raw])
    detail = index.get_session_detail(conn, raw["session_id"])
    selected = [attachments.inventory(detail)[0]["id"]]
    first = review_snapshots.save_review_snapshot(conn, detail)
    second = review_snapshots.save_review_snapshot(conn, detail, attachment_ids=selected)
    assert first != second
    sid = index.create_share(conn, [raw["session_id"]], review_snapshot_ids={raw["session_id"]: second})
    out, manifest = index.export_share_to_disk(conn, sid, index.get_share(conn, sid))
    assert not manifest.get("blocked")
    exported = (out / "sessions.jsonl").read_text()
    assert "loss=0.125" in exported and "alice@audit.test" not in exported
    assert "private duplicate" not in exported and "private-image.png" not in exported
    assert "_review_attachment_ids" not in exported
    assert json.loads(exported)["messages"][0]["extra"]["attachment_review"]["items"][0]["status"] == "included"


def test_imported_selection_is_not_authority_and_unattended_exports_omit_attachments(conn):
    raw = trace()
    raw["_review_attachment_ids"] = [attachments.inventory(raw)[0]["id"]]
    index.upsert_sessions(conn, [raw])
    detail = index.get_session_detail(conn, raw["session_id"])
    detail["_review_attachment_ids"] = raw["_review_attachment_ids"]
    snapshot = review_snapshots.save_review_snapshot(conn, detail)
    assert review_snapshots.load_review_snapshot(conn, snapshot, raw["session_id"])["_review_attachment_ids"] == []
    sid = index.create_share(conn, [raw["session_id"]])
    out, manifest = index.export_share_to_disk(conn, sid, index.get_share(conn, sid))
    assert not manifest.get("blocked")
    exported = (out / "sessions.jsonl").read_text()
    assert "loss=0.125" not in exported and '"excluded"' in exported


@pytest.mark.parametrize("value", [None, {}, ["image"]])
def test_malformed_type_does_not_crash_inventory(value):
    attachments.inventory({"messages": [{"role": "user", "content": [{"type": value}]}]})


def test_reimport_requires_fresh_selection_and_preserves_missing_context():
    raw = trace()
    selected = attachments.prepare(raw, [attachments.inventory(raw)[0]["id"]])
    choices = attachments.inventory(selected)
    assert len(choices) == 2
    default = attachments.prepare(selected)
    assert "loss=0.125" not in json.dumps(default)
    assert len(default["messages"][0]["extra"]["attachment_review"]["items"]) == 2
    fresh = attachments.prepare(selected, [choices[0]["id"]])
    assert "loss=0.125" in json.dumps(fresh)


def test_media_message_does_not_shift_later_message_coordinates():
    raw = {"messages": [{"role": "user", "type": "image", "data": "QUJD"},
                        {"role": "user", "content": "Next message", "files": [{"file_name": "missing.txt"}]}]}
    output = attachments.prepare(raw)
    assert len(output["messages"]) == 2
    assert output["messages"][1]["content"] == "Next message"
    assert output["messages"][1]["extra"]["attachment_review"]["items"][0]["message_index"] == 1


def test_scientific_tool_columns_named_files_or_images_are_not_attachments():
    raw = trace()
    tool = {"tool": "analyze", "input": {"files": ["observations.csv"], "images": 12},
            "output": {"type": "image", "files": [{"loss": 0.125}], "metadata": {"files": 4}}}
    raw["messages"][0]["tool_uses"] = [tool]
    assert len(attachments.inventory(raw)) == 2
    assert attachments.prepare(raw)["messages"][0]["tool_uses"] == [tool]


def test_saved_json_key_order_does_not_change_attachment_order_or_export_ids():
    raw = trace()
    raw["messages"][0]["content"] = [{"type": "file", "file_name": "earlier.txt", "extracted_content": "first"}]
    restored = json.loads(json.dumps(raw, sort_keys=True))
    choices = attachments.inventory(raw)
    assert choices == attachments.inventory(restored)
    selected = [c["id"] for c in choices if c["can_include"]]
    assert attachments.prepare(raw, selected) == attachments.prepare(restored, selected)
