"""Local attachment inventory and explicit, per-preview sharing choices.

Never resolve a filename, URL or provider ID into a filesystem/network read.
Only text present in the imported row can enter the existing redaction pipeline.
Opaque bytes remain unavailable for sharing until a supported extractor exists.
"""
from __future__ import annotations

import base64
import copy
import hashlib
import io
import json

VERSION = "clawjournal.attachments.v1"
VISUAL_VERSION = "clawjournal.attachments.v2"
MAX_TEXT_BYTES = 2 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 20_000_000
IMAGE_MIMES = {"image/png", "image/jpeg", "image/webp"}
MEDIA = {"image", "image_url", "input_image", "audio", "input_audio", "video",
         "file", "input_file", "document", "attachment", "image_asset_pointer"}
CARRIERS = {"attachments", "files", "images", "audio", "video"}


class AttachmentSelectionError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True,
                                     default=str).encode()).hexdigest()


def _is_media(value):
    return (isinstance(value, dict)
            and any(isinstance(value.get(k), str) and value[k] in MEDIA for k in ("type", "content_type"))
            and (set(value) <= {"type", "content_type", "role"}
                 or bool(set(value) & {"data", "source", "image_url", "url", "file_id", "file_uuid",
                                       "file_name", "filename", "_filename", "file_data", "asset_pointer",
                                       "extracted_content", "text", "caption", "alt"})))


def _carrier_container(path):
    # A tool's input/output may legitimately contain a scientific `files` or
    # `images` column. Only provider metadata containers own attachment lists.
    return path == () or (len(path) >= 2 and path[0] == "messages"
                          and path[2:] in ((), ("extra",), ("metadata",)))


def _encoded_file(item):
    """Return declared MIME and embedded base64, never resolve a reference."""
    if not isinstance(item, dict):
        return "", None
    source = item.get("source")
    source = source if isinstance(source, dict) else {}
    if source.get("type") == "text":
        return "", None
    encoded = item.get("file_data", item.get("data", source.get("data")))
    mime = item.get("file_type", item.get("mime_type", source.get("media_type", "")))
    for candidate in (encoded, item.get("image_url"), item.get("audio_url")):
        candidate = candidate.get("url") if isinstance(candidate, dict) else candidate
        if isinstance(candidate, str) and candidate.startswith("data:") and ";base64," in candidate:
            header, encoded = candidate.split(";base64,", 1)
            mime = header[5:]
            break
    return mime if isinstance(mime, str) else "", encoded


def _payload(item):
    if not isinstance(item, dict):
        return "reference_only", "", ""
    extracted = item.get("extracted_content")
    text = extracted if isinstance(extracted, str) else ""
    if not text and _is_media(item) and isinstance(item.get("text"), str):
        text = item["text"]
    source = item.get("source")
    if isinstance(source, dict) and source.get("type") == "text" and isinstance(source.get("data"), str):
        text = text or source["data"]
    mime, encoded = _encoded_file(item)
    has_bytes = isinstance(encoded, str) and bool(encoded)
    availability = "original_file" if has_bytes else ("extracted_text" if text else "reference_only")
    if not text and has_bytes:
        if isinstance(mime, str) and (mime.startswith("text/") or mime in {"txt", "application/json"}):
            try:
                if len(encoded) <= MAX_TEXT_BYTES * 2:
                    text = base64.b64decode(encoded, validate=True).decode("utf-8")
            except (ValueError, UnicodeError):
                pass
    if len(text.encode("utf-8", errors="replace")) > MAX_TEXT_BYTES:
        return availability, "", "Attachment text exceeds the local review limit."
    reason = "" if text else ("The export contains no attachment contents." if not has_bytes
                              else "This file needs a supported text extractor before sharing.")
    return availability, text, reason


def _collect(session):
    """Collect carriers and media blocks, with paths retained only locally."""
    entries = []

    def add(item, path, message_index):
        data = item if isinstance(item, dict) else ({"file_name": item} if isinstance(item, str) else {})
        availability, text, reason = _payload(item)
        entries.append({"id": _digest([path, item]), "message_index": message_index,
                        "name": str(data.get("file_name", data.get("filename", data.get("_filename", data.get("name", "Attachment"))))),
                        "availability": availability, "text": text, "reason": reason,
                        "item": item,
                        "paths": [path], "provider_id": data.get("file_uuid", data.get("file_id"))})

    def walk(value, path, index):
        if isinstance(value, list):
            for j, child in enumerate(value):
                walk(child, path + (j,), index)
        elif isinstance(value, dict):
            if _is_media(value):
                add(value, path, index)
                return
            for key in sorted(value):
                child = value[key]
                if key == "attachment_review" and _carrier_container(path):
                    # Re-importing a prior share does not carry consent forward.
                    # Keep its omissions, and offer its already-redacted text as
                    # a new unchecked choice instead of silently dropping it.
                    if isinstance(child, dict) and isinstance(child.get("items"), list):
                        for j, item in enumerate(child["items"]):
                            data = item if isinstance(item, dict) else {}
                            pages = data.get("pages")
                            visual_text = ""
                            if isinstance(pages, list):
                                visual_text = "\n\n".join(
                                    f"[Page {page_index + 1} recognized text]\n{page['text']}"
                                    for page_index, page in enumerate(pages)
                                    if isinstance(page, dict) and isinstance(page.get("text"), str) and page["text"])
                            # A stored PNG reference alone is not the PNG file.
                            # Offer OCR as a fresh text choice and retain the
                            # missing visual evidence and unknown attribution.
                            origin = index if type(data.get("message_index")) is int and data["message_index"] == index else None
                            add({"file_name": data.get("name", "Attachment"),
                                 "extracted_content": visual_text or data.get("text", ""),
                                 "visual_context_missing": isinstance(pages, list) or data.get("visual_context_missing") is True},
                                path + (key, "items", j), origin)
                    else:
                        add({}, path + (key,), index)
                    continue
                if key == "exported_text" and _carrier_container(path):
                    continue
                if _carrier_container(path) and key in CARRIERS and child not in (None, [], {}, ""):
                    for j, item in enumerate(child if isinstance(child, list) else [child]):
                        add(item, path + (key,) + ((j,) if isinstance(child, list) else ()), index)
                else:
                    walk(child, path + (key,), index)

    for i, message in enumerate(session.get("messages", [])):
        walk(message, ("messages", i), i)
    for key in sorted(CARRIERS):
        if key in session:
            walk({key: session[key]}, (), None)
    # Claude repeats a file as metadata alongside its extracted attachment.
    # Merge a reference only when it has exactly one matching text-bearing entry
    # in that message. Never coalesce conflicting payloads by filename.
    result = []
    for entry in entries:
        if entry["availability"] == "reference_only":
            matches = [other for other in entries if other["text"]
                       and other["message_index"] == entry["message_index"]
                       and ((entry["provider_id"] and entry["provider_id"] == other["provider_id"])
                            or (entry["name"] != "Attachment" and entry["name"] == other["name"]))]
            if len(matches) == 1:
                matches[0]["paths"].extend(entry["paths"])
                continue
        result.append(entry)
    return result


def _can_preview_image(item):
    mime, encoded = _encoded_file(item)
    return mime in IMAGE_MIMES and isinstance(encoded, str) and bool(encoded)


def _can_review_visual(item):
    mime, encoded = _encoded_file(item)
    return mime in IMAGE_MIMES | {"application/pdf"} and isinstance(encoded, str) and bool(encoded)


def _has_visual_context(item):
    if not isinstance(item, dict):
        return False
    mime, _encoded = _encoded_file(item)
    source = item.get("source")
    if not mime and isinstance(source, dict) and isinstance(source.get("media_type"), str):
        mime = source["media_type"]
    name = next((item[key] for key in ("file_name", "filename", "_filename", "name")
                 if isinstance(item.get(key), str)), "")
    return (mime.startswith("image/") or mime == "application/pdf"
            or item.get("type") in ("image", "image_url", "input_image", "image_asset_pointer")
            or name.lower().endswith((".pdf", ".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".tif", ".tiff"))
            or item.get("visual_context_missing") is True)


def inventory(session):
    return [{"id": e["id"], "message_index": e["message_index"], "name": e["name"],
             "availability": e["availability"], "can_include": bool(e["text"]),
             "reason": ("Review and approve the redacted pages before including this attachment."
                        if _can_review_visual(e["item"]) and not e["text"] else e["reason"]),
             "original_text": e["text"],
             "can_preview_image": _can_preview_image(e["item"]),
             "can_review_visual": _can_review_visual(e["item"]),
             "visual_context_missing": bool(e["text"]) and _has_visual_context(e["item"])}
            for e in _collect(session)]


def image_preview(session, attachment_id, *, full_resolution=False):
    """Decode a bounded still image locally and return a metadata-free PNG.

    A preview is not redacted content and cannot authorize sharing. Only embedded
    bytes are read. Unsupported formats, animations, and corrupt files fail closed.
    """
    try:
        import PIL
        from PIL import Image, ImageOps, UnidentifiedImageError
    except ImportError as exc:
        raise AttachmentSelectionError("Image previews require Python 3.11 or newer and Pillow 13. Update ClawJournal and its dependencies.") from exc
    if int(PIL.__version__.split(".", 1)[0]) < 13:
        raise AttachmentSelectionError("Image previews require Pillow 13 or newer on Python 3.11 or newer.")

    entry = next((e for e in _collect(session) if e["id"] == attachment_id), None)
    if entry is None:
        raise AttachmentSelectionError("The attachment changed. Refresh the attachment list.")
    mime, encoded = _encoded_file(entry["item"])
    if mime not in IMAGE_MIMES or not isinstance(encoded, str) or not encoded:
        raise AttachmentSelectionError("No supported embedded image is available for preview.")
    if len(encoded) > 4 * ((MAX_IMAGE_BYTES + 2) // 3):
        raise AttachmentSelectionError("Image preview exceeds the 10 MiB file limit.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE_BYTES:
            raise AttachmentSelectionError("Image preview exceeds the 10 MiB file limit.")
        with Image.open(io.BytesIO(raw), formats=["PNG", "JPEG", "WEBP"]) as original:
            if original.width * original.height > MAX_IMAGE_PIXELS:
                raise AttachmentSelectionError("Image preview exceeds the 20 megapixel limit.")
            if getattr(original, "n_frames", 1) != 1:
                raise AttachmentSelectionError("Animated images need a separate preview workflow.")
            original.load()
            pixels = ImageOps.exif_transpose(original).convert("RGBA")
            if not full_resolution:
                pixels.thumbnail((2048, 2048))
            # A fresh image copies pixels only, not EXIF/XMP/text/ICC metadata.
            clean = Image.new("RGBA", pixels.size)
            clean.paste(pixels)
            output = io.BytesIO()
            clean.save(output, format="PNG")
            preview = output.getvalue()
            if len(preview) > MAX_IMAGE_BYTES:
                raise AttachmentSelectionError("Rendered preview exceeds the local size limit.")
            return preview
    except AttachmentSelectionError:
        raise
    except (ValueError, OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as exc:
        raise AttachmentSelectionError("The embedded image could not be decoded safely.") from exc


def prepare(session, selected_ids=None, *, visual_attachments=None):
    """Remove attachment carriers, then expose only explicitly selected text.

    Input is copied. Selection is a caller argument, never authority imported
    from the source row. Filenames and provider IDs for omissions stay local.
    The per-message records use the already scanned/exported `extra` field.
    """
    entries = _collect(session)
    selected_ids = [] if selected_ids is None else selected_ids
    if (not isinstance(selected_ids, list) or any(not isinstance(i, str) for i in selected_ids)
            or len(selected_ids) != len(set(selected_ids))):
        raise AttachmentSelectionError("Attachment choices must be a list of unique IDs.")
    selected = set(selected_ids)
    visual_attachments = {} if visual_attachments is None else visual_attachments
    if not isinstance(visual_attachments, dict) or not set(visual_attachments) <= selected:
        raise AttachmentSelectionError("Visual approvals must match the selected attachments.")
    from .workbench.visual_share import validate_approval
    for identity, approval in visual_attachments.items():
        entry = next((e for e in entries if e["id"] == identity and _can_review_visual(e["item"])), None)
        if entry is None:
            raise AttachmentSelectionError("The approved attachment changed. Review it again.")
        validate_approval(approval, attachment_id=identity, source_revision=session.get("content_revision"))
    allowed = {e["id"] for e in entries if e["text"]} | set(visual_attachments)
    if not selected <= allowed:
        raise AttachmentSelectionError("An attachment changed or has no supported contents. Refresh the preview.")
    result = copy.deepcopy(session)
    # Remove all known aliases, including Claude's retained raw message copies.
    # Quoted attachment text in ordinary chat content is still chat content.
    def strip(value, path=()):
        if isinstance(value, list):
            return [strip(v, path + (j,)) for j, v in enumerate(value) if not _is_media(v)]
        if isinstance(value, dict):
            if _is_media(value):
                return {}
            return {k: strip(v, path + (k,)) for k, v in value.items()
                    if not (_carrier_container(path) and (k in CARRIERS or k == "attachment_review"
                            or (entries and k in {"exported_content", "exported_text"})))}
        return value
    result = strip(result)
    # Preserve message positions even if an importer places a media block at
    # message level. Content-block lists may shrink; message indexes must not.
    result["messages"] = [({"role": m.get("role", "user"), "content": ""} if _is_media(m)
                           else strip(m, ("messages", i))) for i, m in enumerate(session.get("messages", []))]
    result.pop("_review_attachment_ids", None)
    result.pop("_review_visual_attachments", None)
    for n, entry in enumerate(entries):
        index = entry["message_index"]
        # Session-level attachments have no exact message attribution. Bind to
        # the first message with an explicit unknown-origin marker, never guess.
        target = index if index is not None else 0
        if not result.get("messages"):
            result["messages"] = [{"role": "system", "content": ""}]
        message = result["messages"][target]
        if not isinstance(message.get("extra"), dict):
            message["extra"] = {}
        review = message["extra"].setdefault("attachment_review", {"schema_version": VERSION, "items": []})
        included = entry["id"] in selected
        record = {"attachment_id": f"attachment-{n + 1}", "message_index": index,
                  "availability": entry["availability"],
                  "status": "included" if included else ("excluded" if entry["text"] else "unavailable")}
        if included:
            if entry["id"] in visual_attachments:
                approval = visual_attachments[entry["id"]]
                review["schema_version"] = VISUAL_VERSION
                record.update(name=approval["name"], pages=[{
                    "page_index": p["index"], "asset_path": f"attachments/{p['sha256']}.png",
                    "sha256": p["sha256"], "width": p["width"], "height": p["height"],
                    "text": p["text"], "redaction_count": p["redaction_count"],
                } for p in approval["pages"]])
            else:
                record.update(name=entry["name"], text=entry["text"])
                if _has_visual_context(entry["item"]):
                    record["visual_context_missing"] = True
        review["items"].append(record)
    return result
