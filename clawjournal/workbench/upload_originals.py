"""Read originals from the reviewed local snapshots for one explicit upload.

No persistent opt-in, raw file in the research bundle, or automatic-upload grant.
"""
import json
import tempfile
import uuid
from pathlib import Path

from ..redaction.pair_contract import canonical, digest, MAX_BYTES, TERMS_VERSION

SCHEMA = "clawjournal-upload-originals-v1"


def prepare(conn, share_id, settings):
    from . import index
    from .review_snapshots import load_share_snapshot
    if settings.get("projects_confirmed") is not True:
        raise ValueError("Confirm project exclusions before sharing originals.")
    share = index.get_share(conn, share_id)
    if not share:
        raise ValueError("Share not found")
    selected = [row for row in share.get("sessions", [])
                if not index.session_matches_excluded_projects(row, settings.get("excluded_projects"))]
    ids = [row["session_id"] for row in selected]
    if (not ids or index.release_gate_blockers(conn, ids)
            or index.source_scope_blockers(conn, ids, settings.get("source_filter"))
            or index.share_revision_blockers(conn, share_id)):
        raise ValueError("The original conversations are unavailable, changed, or blocked from sharing. Review this share again.")
    originals = []
    for row in selected:
        detail = load_share_snapshot(conn, share_id, row["session_id"])
        if detail is None:
            detail = index.get_session_detail(conn, row["session_id"])
        if not detail or index.compute_content_revision(detail) != row.get("share_content_revision"):
            raise ValueError("The original conversation no longer matches the reviewed revision.")
        originals.append({"session_id": row["session_id"], "messages": detail["messages"]})
    # Materialize a stable JSON copy, without paths, metadata or binary attachments.
    document = json.loads(json.dumps({"schema": SCHEMA, "sessions": originals}, default=str))
    if len(canonical(document).encode()) > MAX_BYTES:
        raise ValueError("Original conversations exceed the 36 MiB limit. Select fewer conversations.")
    return document


def checked_bytes(conn, share_id, settings, *, approved_digest, redacted):
    """Recheck exact preview and run existing local-only original credential checks."""
    from ..redaction.conversation_pairs import _scan
    document = prepare(conn, share_id, settings)
    if not approved_digest or digest(document) != approved_digest:
        raise ValueError("Originals changed or were not reviewed. Inspect and approve them again.")
    by_id = {row["session_id"]: row for row in redacted}
    if set(by_id) != {row["session_id"] for row in document["sessions"]}:
        raise ValueError("The finalized conversations differ from the original preview. Review this share again.")
    with tempfile.TemporaryDirectory(prefix="clawjournal-original-check-") as directory:
        for original in document["sessions"]:
            final = by_id[original["session_id"]]
            if final.get("revision_hash") != "sha256:" + digest({"messages": original["messages"]}):
                raise ValueError("Original does not match the finalized redacted conversation.")
            _scan(original, final, str(uuid.uuid4()), Path(directory))
    if digest(prepare(conn, share_id, settings)) != approved_digest:
        raise ValueError("Originals changed while scanning. Review this share again.")
    return canonical(document).encode()
