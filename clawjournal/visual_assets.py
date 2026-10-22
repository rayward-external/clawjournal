"""Versioned visual-attachment transport checks, independent of OCR/models.

Assets are opaque, metadata-free RGB PNG pages. The manifest binds their bytes
and the exact reviewed per-message records; text rewrites must not leave older
pixels silently attached. This validates integrity, not scientific sufficiency.
"""
from __future__ import annotations

import hashlib
import base64
import json
import re
import struct
import zlib

VERSION = "clawjournal.visual-assets.v1"
MAX_ASSETS = 200
MAX_ASSET_BYTES = 50 * 1024 * 1024
MAX_TOTAL_BYTES = 100 * 1024 * 1024
MAX_PIXELS = 20_000_000
MAX_STORED_MANIFEST_BYTES = 150 * 1024 * 1024
ASSET_PATH = re.compile(r"attachments/([0-9a-f]{64})\.png\Z")


class VisualAssetError(ValueError):
    pass


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()


def png_dimensions(data):
    """Reject metadata, animation, appended data and oversized decompression."""
    if not isinstance(data, bytes) or len(data) > MAX_ASSET_BYTES or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise VisualAssetError("Visual assets must be bounded PNG files.")
    offset, width, height = 8, 0, 0
    compressed = bytearray()
    ended = False
    while offset < len(data):
        if offset + 12 > len(data):
            raise VisualAssetError("Truncated PNG chunk.")
        size = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        end = offset + 12 + size
        if end > len(data):
            raise VisualAssetError("Truncated PNG chunk.")
        payload = data[offset + 8:offset + 8 + size]
        crc = struct.unpack(">I", data[offset + 8 + size:end])[0]
        if zlib.crc32(kind + payload) & 0xffffffff != crc:
            raise VisualAssetError("Invalid PNG checksum.")
        if kind == b"IHDR" and offset == 8 and size == 13:
            width, height, depth, color, compression, filtering, interlace = struct.unpack(">IIBBBBB", payload)
            if not (width > 0 and height > 0 and width * height <= MAX_PIXELS
                    and (depth, color, compression, filtering, interlace) == (8, 2, 0, 0, 0)):
                raise VisualAssetError("Visual pages must be bounded, non-interlaced, opaque RGB PNGs.")
        elif kind == b"IDAT" and width and not ended:
            compressed.extend(payload)
        elif kind == b"IEND" and width and compressed and size == 0 and end == len(data):
            ended = True
        else:
            raise VisualAssetError("Visual pages must not contain metadata, animation, or appended content.")
        offset = end
    if not ended:
        raise VisualAssetError("PNG is incomplete.")
    expected = height * (width * 3 + 1)
    try:
        decoder = zlib.decompressobj()
        pixels = decoder.decompress(bytes(compressed), expected + 1)
        if len(pixels) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
            raise VisualAssetError("PNG pixel data has an unexpected length.")
        if any(pixels[i] > 4 for i in range(0, expected, width * 3 + 1)):
            raise VisualAssetError("PNG has an invalid row filter.")
    except zlib.error as exc:
        raise VisualAssetError("PNG pixel data is invalid.") from exc
    return width, height


def visual_records(sessions):
    for row, session in enumerate(sessions):
        if not isinstance(session, dict):
            raise VisualAssetError(f"session {row + 1} must be an object")
        messages = session.get("messages", [])
        if not isinstance(messages, list):
            raise VisualAssetError(f"session {row + 1} messages must be an array")
        for message_index, message in enumerate(messages):
            if not isinstance(message, dict):
                raise VisualAssetError(f"session {row + 1} message {message_index + 1} must be an object")
            extra = message.get("extra") if isinstance(message, dict) else None
            review = extra.get("attachment_review") if isinstance(extra, dict) else None
            if not isinstance(review, dict):
                continue
            for item in review.get("items", []) if isinstance(review.get("items"), list) else []:
                if isinstance(item, dict) and "pages" in item:
                    if review.get("schema_version") != "clawjournal.attachments.v2":
                        raise VisualAssetError("Visual pages require attachment schema version 2.")
                    yield row, message_index, item


def validate_bundle(manifest, sessions, assets):
    """Validate the complete asset set; reject missing files and unapproved extras."""
    records = list(visual_records(sessions))
    declared = manifest.get("visual_attachments")
    if declared is None and not records and not assets:
        return
    if (not isinstance(declared, dict) or set(declared) != {"schema_version", "records", "assets"}
            or declared.get("schema_version") != VERSION or not isinstance(declared.get("records"), list)
            or not isinstance(declared.get("assets"), list) or not 0 < len(declared["assets"]) <= MAX_ASSETS):
        raise VisualAssetError("Visual attachment manifest is missing or invalid.")
    expected_records = []
    referenced = {}
    seen_records = set()
    for row, message_index, record in records:
        if (set(record) != {"attachment_id", "message_index", "availability", "status", "name", "pages"}
                or (record.get("message_index") is not None and
                    (record["message_index"] != message_index or type(record["message_index"]) is not int))
                or record.get("status") != "included" or record.get("availability") != "original_file"
                or not isinstance(record.get("attachment_id"), str)
                or not re.fullmatch(r"attachment-[1-9][0-9]*", record["attachment_id"])
                or not isinstance(record.get("name"), str) or not isinstance(record.get("pages"), list)
                or not 0 < len(record["pages"]) <= 20):
            raise VisualAssetError("Invalid visual attachment record.")
        key = (row, message_index, record["attachment_id"])
        if key in seen_records:
            raise VisualAssetError("Duplicate visual attachment record.")
        seen_records.add(key)
        expected_records.append({"session_index": row, "message_index": message_index,
                                 "attachment_id": record["attachment_id"], "record_sha256": digest(record)})
        for index, page in enumerate(record["pages"]):
            if (not isinstance(page, dict) or set(page) != {"page_index", "asset_path", "sha256", "width", "height", "text", "redaction_count"}
                    or type(page.get("page_index")) is not int or page["page_index"] != index
                    or not isinstance(page.get("asset_path"), str) or not isinstance(page.get("text"), str)
                    or any(type(page[k]) is not int or page[k] < 0 for k in ("width", "height", "redaction_count"))):
                raise VisualAssetError("Invalid visual page record.")
            match = ASSET_PATH.fullmatch(page["asset_path"])
            if not match or match[1] != page["sha256"]:
                raise VisualAssetError("Invalid visual asset path or hash.")
            expected = (page["sha256"], page["width"], page["height"])
            if page["asset_path"] in referenced and referenced[page["asset_path"]] != expected:
                raise VisualAssetError("Conflicting visual asset references.")
            referenced[page["asset_path"]] = expected
    if declared["records"] != expected_records:
        raise VisualAssetError("Visual attachment text or references changed after review.")
    if set(assets) != set(referenced) or len(assets) > MAX_ASSETS:
        raise VisualAssetError("Visual asset files do not match the approved references.")
    if sum(len(v) for v in assets.values()) > MAX_TOTAL_BYTES:
        raise VisualAssetError("Visual assets exceed the bundle limit.")
    expected_assets = []
    for name in sorted(assets):
        data = assets[name]
        sha, width, height = referenced[name]
        if hashlib.sha256(data).hexdigest() != sha or png_dimensions(data) != (width, height):
            raise VisualAssetError("Visual asset bytes differ from the approved page.")
        expected_assets.append({"path": name, "sha256": sha, "size_bytes": len(data)})
    if declared["assets"] != expected_assets:
        raise VisualAssetError("Visual asset manifest differs from its files.")


def embed_assets(manifest, sessions, assets):
    """Preserve pages in STEMTraceBench's existing two-file source storage."""
    if "visual_asset_payloads" in manifest:
        raise VisualAssetError("Uploaded bundles must use separate visual asset files.")
    validate_bundle(manifest, sessions, assets)
    if not assets:
        return manifest
    return {**manifest, "visual_asset_payloads": {
        path: base64.b64encode(data).decode("ascii") for path, data in sorted(assets.items())}}


def inline_assets(manifest, sessions):
    """Decode only bounded embedded page bytes; never resolve external paths."""
    payloads = manifest.get("visual_asset_payloads", {})
    if not isinstance(payloads, dict) or len(payloads) > MAX_ASSETS:
        raise VisualAssetError("Invalid stored visual asset data.")
    assets = {}
    total = 0
    for path, encoded in payloads.items():
        if (not isinstance(path, str) or not ASSET_PATH.fullmatch(path) or not isinstance(encoded, str)
                or len(encoded) > MAX_ASSET_BYTES * 2):
            raise VisualAssetError("Invalid stored visual asset encoding.")
        try:
            data = base64.b64decode(encoded, validate=True)
        except ValueError as exc:
            raise VisualAssetError("Invalid stored visual asset encoding.") from exc
        total += len(data)
        if total > MAX_TOTAL_BYTES:
            raise VisualAssetError("Stored visual assets exceed the total size limit.")
        assets[path] = data
    validate_bundle(manifest, sessions, assets)
    return assets
