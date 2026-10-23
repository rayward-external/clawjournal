"""Wire contract for explicitly consented original/redacted conversation pairs.

Mirrored by clawjournal-share; keep protocol constants and validation identical.
"""
import hashlib
import json
import uuid
from urllib.parse import urlsplit

SCHEMA = "clawjournal-redaction-pair-v1"
TERMS_VERSION = "redaction-improvement-originals-v1"
PURPOSE = (
    "Original conversations are used only to evaluate and improve the redaction "
    "pipeline, including redaction-specific model training. They are not for "
    "general research, unrelated benchmarks, or general-purpose model training."
)
SCOPE = "future-captures-in-this-local-evaluation-store"
RETENTION_DAYS = 30
MAX_BYTES = 36 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def is_uuid(value):
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def origin(value):
    if not isinstance(value, str):
        raise ValueError("An exact HTTPS destination origin is required")
    try:
        p = urlsplit(value)
        port = p.port
    except ValueError:
        raise ValueError("Invalid destination") from None
    if (p.scheme != "https" or not p.hostname or p.username or p.password
            or p.path not in ("", "/") or p.query or p.fragment):
        raise ValueError("An exact HTTPS destination origin is required")
    return f"https://{p.hostname.lower()}" + (f":{port}" if port and port != 443 else "")


def validate(bundle):
    keys = {"schema", "bundle_id", "contributor_id", "split", "original", "redacted",
            "annotations", "redaction_stage", "consent", "original_sha256", "redacted_sha256"}
    if not isinstance(bundle, dict) or set(bundle) != keys or bundle["schema"] != SCHEMA:
        raise ValueError("Invalid conversation-pair schema")
    if not is_uuid(bundle["bundle_id"]) or not is_uuid(bundle["contributor_id"]) or bundle["split"] not in ("train", "dev", "test"):
        raise ValueError("Invalid pair identity or split")
    for side in ("original", "redacted"):
        value = bundle[side]
        if not isinstance(value, dict) or not isinstance(value.get("messages"), list):
            raise ValueError("Both normalized conversations are required")
        if bundle[side + "_sha256"] != digest(value):
            raise ValueError("Conversation hash mismatch")
    if bundle["redaction_stage"] != "local-preview-plus-final-secret-gate":
        raise ValueError("Unsupported redaction stage")
    annotations = bundle["annotations"]
    if not isinstance(annotations, list) or len(annotations) > 10000:
        raise ValueError("Invalid annotations")
    for a in annotations:
        if not isinstance(a, dict) or set(a) != {"unit", "start", "end", "decision", "entity_type", "review_status"}:
            raise ValueError("Invalid annotation fields")
        if (not isinstance(a["unit"], str) or not isinstance(a["entity_type"], str)
                or type(a["start"]) is not int or type(a["end"]) is not int
                or not 0 <= a["start"] < a["end"]
                or a["decision"] not in ("redact", "preserve", "uncertain")
                or a["review_status"] not in ("assistant_draft", "human_reviewed")):
            raise ValueError("Invalid annotation")
        try:
            value = bundle["original"]
            for part in a["unit"].split("/")[1:]:
                key = part.replace("~1", "/").replace("~0", "~")
                value = value[int(key)] if isinstance(value, list) else value[key]
            if not a["unit"].startswith("/messages/") or not isinstance(value, str) or a["end"] > len(value):
                raise ValueError()
        except (KeyError, IndexError, ValueError, TypeError):
            raise ValueError("Invalid annotation offsets") from None
    consent = bundle["consent"]
    if not isinstance(consent, dict) or set(consent) != {"version", "purpose", "destination", "rights", "share_original", "share_redacted", "grant_id", "retention_days", "scope"}:
        raise ValueError("Separate permission for originals and redacted conversations is required")
    if (consent["version"] != TERMS_VERSION or consent["purpose"] != PURPOSE
            or consent["scope"] != SCOPE or not is_uuid(consent["grant_id"])
            or type(consent["retention_days"]) is not int or consent["retention_days"] != RETENTION_DAYS
            or any(consent[k] is not True for k in ("rights", "share_original", "share_redacted"))
            or origin(consent["destination"]) != consent["destination"]):
        raise ValueError("Missing or outdated redaction-improvement-only consent")
    if len(canonical(bundle).encode()) > MAX_BYTES:
        raise ValueError("Conversation pair exceeds size limit")
    return bundle
