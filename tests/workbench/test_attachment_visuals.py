"""Invented visual evidence: preserve a chart, remove identifiers from pixels."""
import base64
from io import BytesIO
import shutil
import subprocess

import pytest

from clawjournal import attachment_visuals as visual, attachments


@pytest.fixture
def page():
    pytest.importorskip("PIL", minversion="13.0.0")
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (400, 200), "white").save(output, format="PNG")
    return visual.Page(0, output.getvalue(), 400, 200)


def trace(raw, mime="application/pdf"):
    return {"messages": [{"role": "user", "extra": {"attachments": [
        {"mime_type": mime, "file_data": base64.b64encode(raw).decode()}
    ]}}]}


def render(raw, mime="application/pdf"):
    session = trace(raw, mime)
    return visual.render_attachment(session, attachments.inventory(session)[0]["id"])


HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"


def ocr(page, tokens=("loss=0.125", "alice@audit.test", "lambda=1"), confidence=96):
    return visual.parse_ocr_tsv(page, HEADER + "".join(
        f"5\t1\t1\t1\t{i + 1}\t1\t10\t{10 + i * 30}\t150\t20\t{confidence}\t{token}\n"
        for i, token in enumerate(tokens)
    ))


def test_scanned_email_masks_pixels_and_ocr_but_preserves_scientific_results(page):
    from PIL import Image
    result = ocr(page)
    redacted = visual.redact_page(page, result, visual.candidate_spans(result))
    assert redacted.source_page_digest == page.digest
    assert "alice@audit.test" not in redacted.text
    assert "loss=0.125" in redacted.text and "lambda=1" in redacted.text
    assert redacted.warnings  # OCR is never a full privacy assessment.
    with Image.open(BytesIO(redacted.png)) as image:
        assert image.mode == "RGB" and not image.info
        assert image.getpixel((10, 40)) == (0, 0, 0)
        assert image.getpixel((10, 10)) == (255, 255, 255)


def test_manual_mask_removes_intersecting_ocr_text_and_custom_terms(page):
    result = ocr(page)
    redacted = visual.redact_page(page, result, manual_boxes=[(9, 9, 70, 29)])
    assert "loss" not in redacted.text and "alice@audit.test" in redacted.text
    spans = visual.candidate_spans(result, custom_strings=["lambda=1"])
    redacted = visual.redact_page(page, result, spans)
    assert "lambda=1" not in redacted.text and "alice@audit.test" not in redacted.text


@pytest.mark.parametrize("box", [(0, 0, 401, 20), (-1, 0, 20, 20), (0, 0, 0, 20),
                                  (True, 0, 20, 20), (0, 0, 1.5, 20), (0, 0, 20)])
def test_bad_manual_masks_fail_instead_of_silently_clipping(page, box):
    with pytest.raises(attachments.AttachmentSelectionError):
        visual.redact_page(page, ocr(page), manual_boxes=[box])


def test_stale_page_and_invalid_text_ranges_are_rejected(page):
    result = ocr(page)
    changed = visual.Page(0, page.png + b"changed", 400, 200)
    with pytest.raises(attachments.AttachmentSelectionError, match="changed"):
        visual.redact_page(changed, result)
    for span in [(0, 1000), (True, 4), (4, 4), (-1, 2), (10, 11), (0,), None]:
        with pytest.raises(attachments.AttachmentSelectionError):
            visual.redact_page(page, result, [span])


def test_blank_and_low_confidence_ocr_keep_explicit_warnings(page):
    blank = visual.parse_ocr_tsv(page, HEADER)
    assert not blank.words and any("No text" in w for w in blank.warnings)
    assert any("low confidence" in w for w in ocr(page, confidence=45).warnings)


@pytest.mark.parametrize("tsv", ["not tsv", HEADER + "5\t1\n",
    HEADER + "5\t1\t1\t1\t1\t1\t390\t0\t150\t20\t98\tprivate\n",
    HEADER + "5\t1\t1\t1\t1\t1\t10\t0\t150\t20\tnan\tprivate\n"])
def test_bad_ocr_data_fails_closed(page, tsv):
    with pytest.raises(attachments.AttachmentSelectionError):
        visual.parse_ocr_tsv(page, tsv)


def test_ocr_dependency_timeout_and_failure_are_not_clean_results(page, monkeypatch):
    monkeypatch.setattr(visual.shutil, "which", lambda _: None)
    with pytest.raises(attachments.AttachmentSelectionError, match="Tesseract"):
        visual.recognize_text(page)
    monkeypatch.setattr(visual.shutil, "which", lambda _: "/invented/tesseract")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("tesseract", 30)
    monkeypatch.setattr(visual.subprocess, "run", timeout)
    with pytest.raises(attachments.AttachmentSelectionError, match="timed out"):
        visual.recognize_text(page)
    monkeypatch.setattr(visual.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess([], 1))
    with pytest.raises(attachments.AttachmentSelectionError, match="failed"):
        visual.recognize_text(page)


def test_visual_renderer_preserves_full_image_dimensions(page):
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (3000, 800), "white").save(output, format="PNG")
    rendered, = render(output.getvalue(), "image/png")
    assert (rendered.width, rendered.height) == (3000, 800)


def test_visual_renderer_does_not_fetch_references_or_accept_stale_ids():
    session = {"messages": [{"extra": {"attachments": [
        {"mime_type": "application/pdf", "url": "https://example.invalid/private.pdf"}
    ]}}]}
    for selected in ("old", attachments.inventory(session)[0]["id"]):
        with pytest.raises(attachments.AttachmentSelectionError):
            visual.render_attachment(session, selected)


def pdf_bytes(page_count=1, width=400):
    """Tiny valid PDF fixture with visible text/figure and private metadata."""
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    pages = []
    for _ in range(page_count):
        number = len(objects) + 1
        pages.append(f"{number} 0 R")
        objects.append((f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} 300] "
                        f"/Resources << /Font << /F1 3 0 R >> >> /Contents {number + 1} 0 R >>").encode())
        stream = (b"BT /F1 18 Tf 30 250 Td (alice@audit.test) Tj 0 -40 Td (loss=0.125 lambda=1) Tj ET "
                  b"0 0 1 RG 3 w 30 40 m 100 100 l 200 140 l 300 160 l S")
        objects.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objects[1] = (f"<< /Type /Pages /Kids [{' '.join(pages)}] /Count {page_count} >>").encode()
    objects.append(b"<< /Author (PRIVATE_PDF_METADATA) >>")
    result = b"%PDF-1.4\n"
    offsets = [0]
    for n, obj in enumerate(objects, 1):
        offsets.append(len(result))
        result += f"{n} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(result)
    result += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    result += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets[1:])
    result += (f"trailer\n<< /Size {len(offsets)} /Root 1 0 R /Info {len(objects)} 0 R >>\n"
               f"startxref\n{xref}\n%%EOF\n").encode()
    return result


def test_real_pdf_renderer_removes_metadata_keeps_every_page_and_does_not_select(page):
    pytest.importorskip("pypdfium2")
    from PIL import Image
    session = trace(pdf_bytes(2))
    selected = attachments.inventory(session)[0]["id"]
    pages = visual.render_attachment(session, selected)
    assert [p.index for p in pages] == [0, 1]
    assert all((p.width, p.height) == (800, 600) for p in pages)
    for p in pages:
        assert b"PRIVATE_PDF_METADATA" not in p.png
        with Image.open(BytesIO(p.png)) as image:
            assert not image.info and image.mode == "RGB"
            assert image.getpixel((200, 400)) == (0, 0, 255)  # plotted result
    assert attachments.prepare(session)["messages"][0]["extra"]["attachment_review"]["items"][0]["status"] == "unavailable"


@pytest.mark.parametrize("raw", [b"not a PDF", pdf_bytes(21), pdf_bytes(width=100000)])
def test_pdf_renderer_rejects_corrupt_overlong_and_oversized_documents(page, raw):
    pytest.importorskip("pypdfium2")
    with pytest.raises(attachments.AttachmentSelectionError, match="completely"):
        render(raw)


def test_real_ocr_and_pixel_redaction_of_pdf(page):
    pytest.importorskip("pypdfium2")
    if not shutil.which("tesseract"):
        pytest.skip("Local Tesseract is not installed")
    rendered, = render(pdf_bytes())
    recognized = visual.recognize_text(rendered)
    assert "alice@audit.test" in recognized.text
    result = visual.redact_page(rendered, recognized, visual.candidate_spans(recognized))
    assert "alice@audit.test" not in result.text and "0.125" in result.text
    rescanned = visual.recognize_text(visual._page(0, result.png))
    assert "alice" not in rescanned.text and "0.125" in rescanned.text


def test_pdf_dependency_failure_and_worker_timeout_are_actionable(page, monkeypatch):
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "5.13.0")
    with pytest.raises(attachments.AttachmentSelectionError, match="visual-attachments"):
        render(pdf_bytes())
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "5.14.0")
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired("renderer", 45)
    monkeypatch.setattr(visual.subprocess, "run", timeout)
    with pytest.raises(attachments.AttachmentSelectionError, match="timed out"):
        render(pdf_bytes())


def test_pdf_byte_and_ocr_output_limits_fail_closed(page, monkeypatch):
    import importlib.metadata
    monkeypatch.setattr(importlib.metadata, "version", lambda _: "5.14.0")
    monkeypatch.setattr(visual, "MAX_PDF_BYTES", 10)
    with pytest.raises(attachments.AttachmentSelectionError, match="file limit"):
        render(pdf_bytes())
    monkeypatch.setattr(visual, "MAX_OCR_BYTES", 10)
    with pytest.raises(attachments.AttachmentSelectionError, match="output exceeds"):
        ocr(page)
