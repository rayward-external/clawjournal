"""Local visual review primitives; rendering/scanning never authorizes export.

Only embedded bytes are accepted. PDFs become opaque page pixels in a bounded
worker, dropping metadata, hidden layers and embedded files. OCR is a fallible
aid for the researcher, not a claim that every private detail has been found.
The sharing path must separately bind approval to these exact redacted bytes.
"""
from __future__ import annotations

import base64
import csv
from dataclasses import dataclass
import hashlib
import io
import math
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .attachments import AttachmentSelectionError, IMAGE_MIMES, _collect, _encoded_file, image_preview

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 20
MAX_PAGE_PIXELS = 20_000_000
MAX_RENDERED_BYTES = 50 * 1024 * 1024
MAX_OCR_BYTES = 2 * 1024 * 1024
MAX_WORDS = 30_000
PDF_TIMEOUT = 45
OCR_TIMEOUT = 30


@dataclass(frozen=True)
class Page:
    index: int
    png: bytes
    width: int
    height: int

    @property
    def digest(self):
        return hashlib.sha256(self.png).hexdigest()


@dataclass(frozen=True)
class Word:
    text: str
    start: int
    end: int
    # Pixel rectangle, right and bottom exclusive, relative to rendered page.
    box: tuple[int, int, int, int]
    confidence: float


@dataclass(frozen=True)
class OcrResult:
    page_digest: str
    text: str
    words: tuple[Word, ...]
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class RedactedPage:
    source_page_digest: str
    png: bytes
    text: str
    masks: tuple[tuple[int, int, int, int], ...]
    warnings: tuple[str, ...]


def _pillow():
    try:
        import PIL
        from PIL import Image
        if int(PIL.__version__.split('.')[0]) < 13:
            raise ImportError
        return Image
    except ImportError as exc:
        raise AttachmentSelectionError("Visual review requires Python 3.11 or newer and Pillow 13.") from exc


def _page(index, png):
    Image = _pillow()
    with Image.open(io.BytesIO(png), formats=["PNG"]) as image:
        if image.width * image.height > MAX_PAGE_PIXELS:
            raise AttachmentSelectionError("Rendered page exceeds the 20 megapixel limit.")
        return Page(index, png, image.width, image.height)


def render_attachment(session, attachment_id):
    """Return all pages, or fail the entire file; never silently truncate it."""
    entry = next((e for e in _collect(session) if e["id"] == attachment_id), None)
    if entry is None:
        raise AttachmentSelectionError("The attachment changed. Refresh the attachment list.")
    mime, encoded = _encoded_file(entry["item"])
    if mime in IMAGE_MIMES:
        return (_page(0, image_preview(session, attachment_id, full_resolution=True)),)
    if mime != "application/pdf" or not isinstance(encoded, str) or not encoded:
        raise AttachmentSelectionError("No supported embedded image or PDF is available.")
    _pillow()
    try:
        from importlib.metadata import version
        parts = tuple(int(p) for p in version("pypdfium2").split('.')[:2])
        if parts < (5, 14) or parts >= (6, 0):
            raise ImportError
    except (ImportError, ValueError) as exc:
        raise AttachmentSelectionError("PDF review requires ClawJournal's visual-attachments dependencies (pypdfium2 5.14 or newer in version 5).") from exc
    if len(encoded) > 4 * ((MAX_PDF_BYTES + 2) // 3):
        raise AttachmentSelectionError("PDF exceeds the 20 MiB file limit.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise AttachmentSelectionError("The embedded PDF is not valid base64.") from exc
    if len(raw) > MAX_PDF_BYTES:
        raise AttachmentSelectionError("PDF exceeds the 20 MiB file limit.")
    # A fresh process avoids PDFium's thread-unsafe native state and lets us
    # terminate a corrupt/expensive document without hanging the HTTP daemon.
    with tempfile.TemporaryDirectory(prefix="clawjournal-visual-") as directory:
        root = Path(directory)
        (root / "input.pdf").write_bytes(raw)
        try:
            process = subprocess.run(
                [sys.executable, "-m", "clawjournal.attachment_visuals", "--render-pdf", str(root)],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=PDF_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AttachmentSelectionError("PDF rendering timed out. No pages were approved.") from exc
        if process.returncode:
            raise AttachmentSelectionError("PDF could not be rendered completely. It may be encrypted, damaged, or exceed the page/size limits.")
        files = sorted(root.glob("page-*.png"))
        if not files or len(files) > MAX_PAGES or sum(p.stat().st_size for p in files) > MAX_RENDERED_BYTES:
            raise AttachmentSelectionError("Rendered PDF exceeds the local review limits.")
        return tuple(_page(i, p.read_bytes()) for i, p in enumerate(files))


def _render_pdf_worker(root):
    """Internal child-process entry point; root is a private generated directory."""
    import pypdfium2 as pdfium
    Image = _pillow()
    with pdfium.PdfDocument((root / "input.pdf").read_bytes()) as document:
        if not 0 < len(document) <= MAX_PAGES:
            raise AttachmentSelectionError("PDF page limit exceeded.")
        document.init_forms()
        total_bytes = 0
        for i in range(len(document)):
            page = document[i]
            try:
                width, height = page.get_size()
                if not all(math.isfinite(v) and v > 0 for v in (width, height)):
                    raise AttachmentSelectionError("Invalid page dimensions.")
                if math.ceil(width * 2) * math.ceil(height * 2) > MAX_PAGE_PIXELS:
                    raise AttachmentSelectionError("PDF page pixel limit exceeded.")
                bitmap = page.render(scale=2, draw_annots=True)
                try:
                    pixels = bitmap.to_pil().convert("RGB")
                    clean = Image.new("RGB", pixels.size, "white")
                    clean.paste(pixels)
                    output = root / f"page-{i:04d}.png"
                    clean.save(output, format="PNG")
                    total_bytes += output.stat().st_size
                    if total_bytes > MAX_RENDERED_BYTES:
                        raise AttachmentSelectionError("Rendered PDF byte limit exceeded.")
                finally:
                    bitmap.close()
            finally:
                page.close()


def parse_ocr_tsv(page, tsv):
    """Keep offsets and pixel boxes together; malformed OCR must not look clean."""
    if len(tsv.encode("utf-8")) > MAX_OCR_BYTES:
        raise AttachmentSelectionError("Text recognition output exceeds the local review limit.")
    words = []
    text = ""
    prior_line = None
    warnings = ["Automatic text recognition currently reads English. It can miss words, handwriting, other languages, faces, and other private details. Check every page yourself."]
    try:
        reader = csv.DictReader(io.StringIO(tsv), delimiter="\t", quoting=csv.QUOTE_NONE)
        required = {"level", "page_num", "block_num", "par_num", "line_num", "left", "top", "width", "height", "conf", "text"}
        if not required <= set(reader.fieldnames or ()):
            raise ValueError
        for row in reader:
            if None in row or None in row.values():
                raise ValueError
            if row["level"] != "5":
                continue
            token = row["text"].strip()
            if not token:
                continue
            x, y, w, h = (int(row[k]) for k in ("left", "top", "width", "height"))
            confidence = float(row["conf"])
            if (min(x, y) < 0 or min(w, h) <= 0 or x + w > page.width or y + h > page.height
                    or not math.isfinite(confidence) or not 0 <= confidence <= 100):
                raise ValueError
            line = tuple(row[k] for k in ("page_num", "block_num", "par_num", "line_num"))
            if text:
                text += " " if line == prior_line else "\n"
            start = len(text)
            text += token
            words.append(Word(token, start, len(text), (x, y, x + w, y + h), confidence))
            if len(words) > MAX_WORDS:
                raise ValueError
            prior_line = line
    except (ValueError, KeyError, TypeError, csv.Error) as exc:
        raise AttachmentSelectionError("Text recognition could not reliably locate words on the page; review cannot continue.") from exc
    if not words:
        warnings.append("No text was recognized. This does not mean the page contains no private information.")
    elif any(w.confidence < 80 for w in words):
        warnings.append("Some text was recognized with low confidence; check these areas carefully.")
    return OcrResult(page.digest, text, tuple(words), tuple(warnings))


def recognize_text(page):
    """Run local English OCR, never send pixels to an external model/service."""
    executable = shutil.which("tesseract")
    if not executable:
        raise AttachmentSelectionError("To read text from images, install the local text recognition tool Tesseract with English language data.")
    with tempfile.TemporaryDirectory(prefix="clawjournal-ocr-") as directory:
        root = Path(directory)
        (root / "page.png").write_bytes(page.png)
        try:
            process = subprocess.run(
                [executable, str(root / "page.png"), str(root / "recognized"), "-l", "eng", "--psm", "11", "tsv"],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=OCR_TIMEOUT, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AttachmentSelectionError("Local text recognition timed out. This page has not been scanned.") from exc
        output = root / "recognized.tsv"
        if process.returncode or not output.is_file() or output.stat().st_size > MAX_OCR_BYTES:
            raise AttachmentSelectionError("Local text recognition failed. This page has not been scanned.")
        try:
            tsv = output.read_text(encoding="utf-8")
        except UnicodeError as exc:
            raise AttachmentSelectionError("Local text recognition returned invalid text.") from exc
        return parse_ocr_tsv(page, tsv)


def candidate_spans(ocr, custom_strings=()):
    """Initial deterministic suggestions only; full share scans remain required."""
    from .redaction.boundaries import ensure_text_boundaries
    from .redaction.secrets import scan_text
    ensure_text_boundaries(ocr.text)
    spans = [(f.get("replacement_start", f["start"]), f.get("replacement_end", f["end"]))
             for f in scan_text(ocr.text)]
    for value in custom_strings:
        if not isinstance(value, str) or not value:
            continue
        offset = 0
        while (start := ocr.text.find(value, offset)) >= 0:
            spans.append((start, start + len(value)))
            offset = start + 1
    return tuple(sorted(set(spans)))


def redact_page(page, ocr, spans=(), manual_boxes=()):
    """Burn masks into new opaque pixels, removing corresponding OCR words too.

    Input OCR is server-produced and tied to the exact page. Callers must not
    accept client-supplied OCR or use these bytes as approval by themselves.
    """
    if ocr.page_digest != page.digest:
        raise AttachmentSelectionError("The page changed. Scan it again before redacting.")
    boxes = []
    for span in spans:
        if not isinstance(span, (tuple, list)) or len(span) != 2:
            raise AttachmentSelectionError("Invalid text redaction range.")
        start, end = span
        if type(start) is not int or type(end) is not int or not 0 <= start < end <= len(ocr.text):
            raise AttachmentSelectionError("Invalid text redaction range.")
        matched = [w.box for w in ocr.words if start < w.end and end > w.start]
        if not matched:
            raise AttachmentSelectionError("Text redaction has no matching page coordinates.")
        boxes.extend(matched)
    boxes.extend(manual_boxes)
    masks = []
    for box in boxes:
        if (not isinstance(box, (tuple, list)) or len(box) != 4
                or any(type(v) is not int for v in box)):
            raise AttachmentSelectionError("Invalid page redaction rectangle.")
        x0, y0, x1, y1 = box
        if not (0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height):
            raise AttachmentSelectionError("Redaction rectangle is outside the page.")
        # Cover antialiased glyph edges as well as OCR's tight word box.
        masks.append((max(0, x0 - 2), max(0, y0 - 2), min(page.width, x1 + 2), min(page.height, y1 + 2)))
    masks = sorted(set(masks))
    Image = _pillow()
    with Image.open(io.BytesIO(page.png), formats=["PNG"]) as pixels:
        clean = Image.new("RGB", pixels.size, "white")
        if "A" in pixels.getbands():
            clean.paste(pixels, mask=pixels.getchannel("A"))
        else:
            clean.paste(pixels)
        for box in masks:
            clean.paste((0, 0, 0), box)
        output = io.BytesIO()
        clean.save(output, format="PNG")
    text = ocr.text
    for word in reversed(ocr.words):
        x0, y0, x1, y1 = word.box
        if any(x0 < bx1 and x1 > bx0 and y0 < by1 and y1 > by0 for bx0, by0, bx1, by1 in masks):
            text = text[:word.start] + "[REDACTED]" + text[word.end:]
    return RedactedPage(page.digest, output.getvalue(), text, tuple(masks), ocr.warnings)


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "--render-pdf":
        raise SystemExit(2)
    try:
        _render_pdf_worker(Path(sys.argv[2]))
    except Exception:
        # Never print a native decoder diagnostic containing source information.
        raise SystemExit(1)
