"""Visual drafts must stay local, bounded, and tied to exact source/settings."""
from io import BytesIO

import pytest

from clawjournal import attachment_visuals as visual
from clawjournal.workbench import attachment_reviews as reviews


@pytest.fixture
def fixture(monkeypatch):
    pytest.importorskip("PIL", minversion="13.0.0")
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (400, 200), "white").save(output, format="PNG")
    page = visual.Page(0, output.getvalue(), 400, 200)
    ocr = visual.OcrResult(page.digest, "alice@audit.test loss=0.125", (
        visual.Word("alice@audit.test", 0, 16, (10, 10, 180, 30), 99),
        visual.Word("loss=0.125", 17, 27, (10, 40, 180, 60), 99),
    ), ("Review every page visually.",))
    monkeypatch.setattr(visual, "render_attachment", lambda *args: (page,))
    monkeypatch.setattr(visual, "recognize_text", lambda *args: ocr)
    return reviews.VisualReviews(), ("session", "revision", "attachment", "settings")


def test_scan_correction_and_manual_mask_are_version_bound_and_not_authority(fixture):
    store, scope = fixture
    draft = store.start({}, scope)
    assert draft["sharing_authorized"] is False
    assert draft["pages"][0]["status"] == "unscanned"
    initial_version = draft["pages"][0]["version"]
    with pytest.raises(reviews.VisualReviewError, match="Scan"):
        store.page(draft["review_id"], scope, 0, "redacted", initial_version)
    scanned = store.update(draft["review_id"], scope, 0, version=initial_version)
    page = scanned["pages"][0]
    assert "alice" not in page["redacted_text"] and "loss=0.125" in page["redacted_text"]
    assert page["selected_candidate_ids"] == [0]
    assert store.page(draft["review_id"], scope, 0, "redacted", page["version"]).startswith(b"\x89PNG")
    with pytest.raises(reviews.VisualReviewError, match="changed"):
        store.update(draft["review_id"], scope, 0, version=initial_version, selected=[])
    corrected = store.update(draft["review_id"], scope, 0, version=page["version"], selected=[])
    assert "alice" in corrected["pages"][0]["redacted_text"]
    manual = store.update(draft["review_id"], scope, 0, version=corrected["pages"][0]["version"], manual=[[5, 5, 185, 35]])
    assert "alice" not in manual["pages"][0]["redacted_text"]
    assert manual["sharing_authorized"] is False


@pytest.mark.parametrize("position", [0, 1, 2, 3])
def test_cannot_use_draft_for_different_source_revision_attachment_or_settings(fixture, position):
    store, scope = fixture
    draft = store.start({}, scope)
    changed = list(scope); changed[position] = "different"
    with pytest.raises(reviews.VisualReviewError, match="changed"):
        store.describe(draft["review_id"], tuple(changed))


def test_cache_eviction_expiry_and_memory_limits_are_explicit(fixture, monkeypatch):
    store, scope = fixture
    monkeypatch.setattr(reviews, "MAX_DRAFTS", 1)
    first = store.start({}, scope)
    second = store.start({}, scope)
    with pytest.raises(reviews.VisualReviewError, match="expired"):
        store.describe(first["review_id"], scope)
    monkeypatch.setattr(reviews, "DRAFT_TTL", -1)
    with pytest.raises(reviews.VisualReviewError, match="expired"):
        store.describe(second["review_id"], scope)
    monkeypatch.setattr(reviews, "MAX_CACHE_BYTES", 1)
    with pytest.raises(reviews.VisualReviewError, match="memory limit"):
        store.start({}, scope)


def test_failed_ocr_does_not_create_redacted_preview_or_poison_processing_lock(fixture, monkeypatch):
    store, scope = fixture
    draft = store.start({}, scope)
    def fail(*args):
        raise visual.AttachmentSelectionError("OCR failed")
    monkeypatch.setattr(visual, "recognize_text", fail)
    with pytest.raises(visual.AttachmentSelectionError, match="OCR failed"):
        store.update(draft["review_id"], scope, 0, version=draft["pages"][0]["version"])
    assert store.describe(draft["review_id"], scope)["pages"][0]["status"] == "unscanned"
    assert store.start({}, scope)


def test_busy_processor_returns_actionable_failure(fixture):
    store, scope = fixture
    with store._processing:
        with pytest.raises(reviews.VisualReviewError, match="Another attachment"):
            store.start({}, scope)


def test_clear_removes_drafts_and_prevents_inflight_render_from_recreating_them(fixture, monkeypatch):
    store, scope = fixture
    draft = store.start({}, scope)
    original_render = visual.render_attachment
    def render_then_clear(*args):
        result = original_render(*args)
        store.clear()
        return result
    monkeypatch.setattr(visual, "render_attachment", render_then_clear)
    with pytest.raises(reviews.VisualReviewError, match="cleared"):
        store.start({}, scope)
    with pytest.raises(reviews.VisualReviewError, match="expired"):
        store.describe(draft["review_id"], scope)


@pytest.mark.parametrize("choices", [[True], [0, 0], [7], "all"])
def test_invalid_candidate_choices_do_not_modify_preview(fixture, choices):
    store, scope = fixture
    draft = store.start({}, scope)
    scanned = store.update(draft["review_id"], scope, 0, version=draft["pages"][0]["version"])
    with pytest.raises(reviews.VisualReviewError, match="choices"):
        store.update(draft["review_id"], scope, 0, version=scanned["pages"][0]["version"], selected=choices)
    assert store.describe(draft["review_id"], scope) == scanned


def test_full_check_and_explicit_approval_are_required_and_edits_revoke_approval(fixture, monkeypatch):
    from clawjournal.workbench import visual_share
    store, scope = fixture
    draft = store.start({}, scope)
    key = draft["review_id"]
    with pytest.raises(reviews.VisualReviewError, match="Scan every page"):
        store.prepare(key, scope, [], conn=None, name="results.png", settings={})
    scanned = store.update(key, scope, 0, version=draft["pages"][0]["version"])
    versions = [p["version"] for p in scanned["pages"]]
    with pytest.raises(reviews.VisualReviewError, match="full privacy check"):
        store.approve(key, scope, versions, [0])
    monkeypatch.setattr(visual_share, "prepare_pages", lambda conn, sid, name, pages, scans, settings:
                        (name, [scans[p.index]["result"] for p in pages], "invented scanner"))
    checked = store.prepare(key, scope, versions, conn=None, name="results.png", settings={})
    versions = [p["version"] for p in checked["pages"]]
    for inspected in ([], [True], [0, 0], [1]):
        with pytest.raises(reviews.VisualReviewError, match="explicitly review"):
            store.approve(key, scope, versions, inspected)
    with pytest.raises(reviews.VisualReviewError, match="changed"):
        store.approve(key, scope, ["old-version"], [0])
    approved = store.approve(key, scope, versions, [0])
    assert approved["approved"] is True and approved["sharing_authorized"] is False
    assert store.export_approved(key, scope)["pages"][0]["text"] == checked["pages"][0]["redacted_text"]
    changed = store.update(key, scope, 0, version=versions[0], manual=[[190, 100, 200, 110]])
    assert not changed["approved"] and not changed["privacy_checked"]
    with pytest.raises(reviews.VisualReviewError):
        store.export_approved(key, scope)
