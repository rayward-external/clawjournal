"""Full privacy checks and exact-byte records for reviewed visual attachments.

Binary bytes never pass through text replacement. Recognized text is checked by
the existing export pipeline; additional text redactions become burned-in pixel
masks, then the resulting pixels are recognized and checked again.
"""
from __future__ import annotations

import base64
from dataclasses import replace
from difflib import SequenceMatcher
import hashlib
import json
from pathlib import Path
import tempfile

from .. import attachment_visuals as visual
from ..attachments import AttachmentSelectionError

SCHEMA = "clawjournal.visual-approval.v1"


def profile_hash(settings):
    policy = {key: settings.get(key) or [] for key in
              ("custom_strings", "allowlist_entries", "extra_usernames", "blocked_domains")}
    return hashlib.sha256(json.dumps({"version": 1, **policy}, sort_keys=True, default=str).encode()).hexdigest()


def _masked_ocr(ocr, masks, digest):
    """Reconstruct redacted text with boxes still mapped to the original page."""
    text = ""
    words = []
    end = 0
    for word in ocr.words:
        text += ocr.text[end:word.start]
        x0, y0, x1, y1 = word.box
        hidden = any(x0 < b[2] and x1 > b[0] and y0 < b[3] and y1 > b[1] for b in masks)
        value = "[REDACTED]" if hidden else word.text
        start = len(text)
        text += value
        words.append(replace(word, text=value, start=start, end=len(text)))
        end = word.end
    text += ocr.text[end:]
    return visual.OcrResult(digest, text, tuple(words), ocr.warnings)


def _rewrite_boxes(ocr, changed):
    if ocr.text == changed:
        return []
    # Word-level comparison avoids quadratic work over megabytes of characters.
    # Any changed part of a word hides the whole word, including glyph edges.
    before = [w.text for w in ocr.words]
    after = changed.split()
    boxes = []
    for action, start, end, _a, _b in SequenceMatcher(None, before, after).get_opcodes():
        if action == "equal":
            continue
        if start == end:
            start, end = max(0, start - 1), min(len(before), end + 1)
        boxes.extend(w.box for w in ocr.words[start:end])
    if not boxes and before != after:
        raise AttachmentSelectionError("A privacy finding could not be mapped to this page. Add a manual redaction and check it again.")
    return boxes


def check_texts(conn, session_id, name, texts, settings):
    """Run normal export rules and both mandatory scanners; bypass never passes."""
    from .index import apply_share_redactions
    from .share_gate import run_share_gate
    blob = {"session_id": session_id, "display_title": name,
            "messages": [{"role": "user", "content": text} for text in texts]}
    clean, _, _ = apply_share_redactions(
        conn, blob, custom_strings=settings["custom_strings"],
        user_allowlist=settings["allowlist_entries"], extra_usernames=settings["extra_usernames"],
        blocked_domains=settings["blocked_domains"],
    )
    with tempfile.TemporaryDirectory(prefix="clawjournal-visual-scan-") as directory:
        path = Path(directory) / "sessions.jsonl"
        path.write_text(json.dumps(clean, ensure_ascii=True) + "\n", encoding="utf-8")
        report = run_share_gate(path, {"sessions": [{"session_id": session_id}]}, conn=conn)
        if report.bypassed or report.blocking:
            reason = "scanner-bypassed" if report.bypassed else report.block_reason
            raise AttachmentSelectionError(f"Visual attachment privacy check did not pass ({reason}). No pages were approved.")
        if "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest() != report.scanned_sha256:
            raise AttachmentSelectionError("The scanned attachment text changed unexpectedly. Run the privacy check again.")
        checked = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(checked, dict) or not isinstance(checked.get("display_title"), str)
            or not isinstance(checked.get("messages"), list) or len(checked["messages"]) != len(texts)
            or any(not isinstance(m, dict) or not isinstance(m.get("content"), str) for m in checked["messages"])):
        raise AttachmentSelectionError("The privacy scanners changed the page structure. No pages were approved.")
    return checked["display_title"], [m["content"] for m in checked["messages"]], report.engine


def prepare_pages(conn, session_id, name, pages, scans, settings):
    """Return stable checked pixels/text, or fail without issuing an approval."""
    results = [scans[p.index]["result"] for p in pages]
    for _attempt in range(3):
        visible = [visual.recognize_text(visual._page(p.index, r.png)) for p, r in zip(pages, results)]
        mapped = [_masked_ocr(scans[p.index]["ocr"], r.masks, hashlib.sha256(r.png).hexdigest())
                  for p, r in zip(pages, results)]
        texts = [text for original, pixels in zip(mapped, visible) for text in (original.text, pixels.text)]
        checked_name, checked, engine = check_texts(conn, session_id, name, texts, settings)
        rewritten = []
        changed_pixels = False
        for i, (page, result, original, pixels) in enumerate(zip(pages, results, mapped, visible)):
            boxes = _rewrite_boxes(original, checked[2 * i]) + _rewrite_boxes(pixels, checked[2 * i + 1])
            if boxes:
                changed_pixels = True
                current = visual._page(page.index, result.png)
                masked = visual.redact_page(current, pixels, manual_boxes=boxes)
                masks = tuple(sorted(set(result.masks + masked.masks)))
                text = _masked_ocr(scans[page.index]["ocr"], masks, page.digest).text
                warnings = tuple(dict.fromkeys(result.warnings + (
                    "Additional redactions were applied by the full privacy check. Review the updated page.",)))
                rewritten.append(visual.RedactedPage(page.digest, masked.png, text, masks, warnings))
            else:
                rewritten.append(replace(result, text=checked[2 * i]))
        results = rewritten
        name = checked_name
        if not changed_pixels:
            return name, results, engine
    raise AttachmentSelectionError("Visual redactions did not stabilize. Review the page and add any missing masks before trying again.")


def approval_payload(scope, name, pages, scans, engine):
    """Create only after explicit approval of every current page version."""
    return {"schema_version": SCHEMA, "source_revision": scope[1], "attachment_id": scope[2],
            "profile_hash": scope[3], "name": name, "scanner_engine": engine,
            "pages": [{"index": p.index, "width": p.width, "height": p.height,
                       "sha256": hashlib.sha256(scans[p.index]["result"].png).hexdigest(),
                       "png_base64": base64.b64encode(scans[p.index]["result"].png).decode("ascii"),
                       "text": scans[p.index]["result"].text,
                       "redaction_count": len(scans[p.index]["result"].masks)} for p in pages]}


def validate_approval(value, *, attachment_id, source_revision, settings=None):
    """Validate private snapshot data; this does not confer consent on an import."""
    fields = {"schema_version", "source_revision", "attachment_id", "profile_hash", "name", "scanner_engine", "pages"}
    if (not isinstance(value, dict) or set(value) != fields or value.get("schema_version") != SCHEMA
            or value.get("attachment_id") != attachment_id or value.get("source_revision") != source_revision
            or any(not isinstance(value.get(k), str) for k in fields - {"pages"})
            or not value["scanner_engine"] or not isinstance(value["pages"], list)
            or not 0 < len(value["pages"]) <= visual.MAX_PAGES):
        raise AttachmentSelectionError("The approved visual attachment is invalid. Review it again.")
    if settings is not None and value["profile_hash"] != profile_hash(settings):
        raise AttachmentSelectionError("Redaction settings changed after visual approval. Review the attachment again.")
    total = 0
    for i, p in enumerate(value["pages"]):
        if (not isinstance(p, dict) or set(p) != {"index", "width", "height", "sha256", "png_base64", "text", "redaction_count"}
                or type(p["index"]) is not int or p["index"] != i
                or any(type(p[k]) is not int or p[k] < 0 for k in ("width", "height", "redaction_count"))
                or not isinstance(p["text"], str) or not isinstance(p["png_base64"], str)
                or len(p["png_base64"]) > visual.MAX_RENDERED_BYTES * 2):
            raise AttachmentSelectionError("The approved page record is invalid. Review it again.")
        try:
            data = base64.b64decode(p["png_base64"], validate=True)
        except ValueError as exc:
            raise AttachmentSelectionError("Approved page bytes are invalid.") from exc
        total += len(data)
        if total > visual.MAX_RENDERED_BYTES or hashlib.sha256(data).hexdigest() != p["sha256"]:
            raise AttachmentSelectionError("Approved page bytes changed or exceed the limit.")
        page = visual._page(i, data)
        if (page.width, page.height) != (p["width"], p["height"]):
            raise AttachmentSelectionError("Approved page dimensions changed.")
    return value


def assert_visual_unchanged(before, after):
    from ..visual_assets import visual_records
    if list(visual_records([before])) != list(visual_records([after])):
        raise AttachmentSelectionError("The privacy rules changed approved attachment text or references. Review its pages again before sharing.")


def collect_assets(approvals):
    assets = {}
    for approval in approvals.values():
        for page in approval["pages"]:
            assets[f"attachments/{page['sha256']}.png"] = base64.b64decode(page["png_base64"], validate=True)
    return assets


def write_bundle_assets(export_dir, manifest, sessions, assets):
    from ..visual_assets import VERSION, digest, validate_bundle, visual_records
    records = list(visual_records(sessions))
    if not records and not assets:
        manifest.pop("visual_attachments", None)
        return
    manifest["visual_attachments"] = {"schema_version": VERSION,
        "records": [{"session_index": row, "message_index": index, "attachment_id": record["attachment_id"],
                     "record_sha256": digest(record)} for row, index, record in records],
        "assets": [{"path": path, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}
                   for path, data in sorted(assets.items())]}
    validate_bundle(manifest, sessions, assets)
    # Names are fixed content hashes validated above, never imported filenames.
    import os
    for path, data in assets.items():
        destination = export_dir / path
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix=".page-", delete=False) as handle:
            temp = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.replace(temp, destination)
        finally:
            temp.unlink(missing_ok=True)


def verified_bundle_assets(export_dir):
    from ..visual_assets import ASSET_PATH, MAX_ASSET_BYTES, MAX_ASSETS, validate_bundle, VisualAssetError
    manifest = json.loads((export_dir / "manifest.json").read_text(encoding="utf-8"))
    sessions = [json.loads(line) for line in (export_dir / "sessions.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()]
    declared = manifest.get("visual_attachments")
    assets = {}
    if declared is not None:
        if not isinstance(declared, dict) or not isinstance(declared.get("assets"), list) or len(declared["assets"]) > MAX_ASSETS:
            raise VisualAssetError("Invalid visual asset list.")
        for item in declared["assets"]:
            name = item.get("path") if isinstance(item, dict) else None
            if not isinstance(name, str) or not ASSET_PATH.fullmatch(name) or name in assets:
                raise VisualAssetError("Invalid or duplicate visual asset path.")
            path = export_dir / name
            if path.is_symlink() or path.parent.is_symlink():
                raise VisualAssetError("Visual asset paths must not be symlinks.")
            with path.open("rb") as handle:
                assets[name] = handle.read(MAX_ASSET_BYTES + 1)
    validate_bundle(manifest, sessions, assets)
    return assets
