"""Bounded, ephemeral visual review drafts. None of these IDs authorize sharing."""
from __future__ import annotations

from dataclasses import dataclass, field
import copy
import hashlib
import json
import secrets
import threading
import time

from .. import attachment_visuals as visual
from ..attachments import AttachmentSelectionError

MAX_CACHE_BYTES = 128 * 1024 * 1024
MAX_DRAFTS = 4
DRAFT_TTL = 15 * 60
MAX_MASKS = 500


class VisualReviewError(AttachmentSelectionError):
    def __init__(self, message, status=409):
        super().__init__(message)
        self.status = status


@dataclass
class Draft:
    scope: tuple[str, str, str, str]
    pages: tuple[visual.Page, ...]
    touched: float
    scans: dict = field(default_factory=dict)
    privacy_checked: bool = False
    redacted_name: str = ""
    scanner_engine: str = ""
    approved: bool = False

    @property
    def size(self):
        return sum(len(p.png) for p in self.pages) + sum(
            len(s["result"].png) + len(s["ocr"].text.encode()) * 4 + len(s["ocr"].words) * 512
            for s in self.scans.values())


class VisualReviews:
    def __init__(self):
        self._drafts = {}
        self._lock = threading.RLock()
        self._processing = threading.Lock()
        self._generation = 0

    def clear(self):
        with self._lock:
            self._drafts.clear()
            self._generation += 1

    def _prune(self):
        now = time.monotonic()
        for key in list(self._drafts):
            if now - self._drafts[key].touched > DRAFT_TTL:
                del self._drafts[key]

    def _get(self, review_id, scope):
        self._prune()
        draft = self._drafts.get(review_id)
        if draft is None:
            raise VisualReviewError("The local visual review expired. Reopen the attachment to start again.", 410)
        if draft.scope != scope:
            raise VisualReviewError("The conversation or redaction settings changed. Start a new visual review.")
        draft.touched = time.monotonic()
        return draft

    def _reserve(self, size, keep=None):
        if size > MAX_CACHE_BYTES:
            raise VisualReviewError("The visual review exceeds the local memory limit.", 422)
        self._prune()
        while self._drafts and (sum(v.size for k, v in self._drafts.items() if k != keep) + size > MAX_CACHE_BYTES
                                or (keep is None and len(self._drafts) >= MAX_DRAFTS)):
            choices = [(v.touched, k) for k, v in self._drafts.items() if k != keep]
            if not choices:
                break
            del self._drafts[min(choices)[1]]

    def start(self, detail, scope):
        if not self._processing.acquire(blocking=False):
            raise VisualReviewError("Another attachment is being processed. Try again when it finishes.")
        try:
            with self._lock:
                generation = self._generation
            pages = visual.render_attachment(detail, scope[2])
            draft = Draft(scope, pages, time.monotonic())
            with self._lock:
                if generation != self._generation:
                    raise VisualReviewError("Saved reviews were cleared while this file was rendering. Reopen it to start again.")
                self._reserve(draft.size)
                review_id = secrets.token_urlsafe(24)
                self._drafts[review_id] = draft
                return self._describe(review_id, draft)
        finally:
            self._processing.release()

    def describe(self, review_id, scope):
        with self._lock:
            return self._describe(review_id, self._get(review_id, scope))

    @staticmethod
    def _describe(review_id, draft):
        pages = []
        for p in draft.pages:
            scan = draft.scans.get(p.index)
            pages.append({"index": p.index, "width": p.width, "height": p.height,
                          "status": "scanned" if scan else "unscanned",
                          "version": scan["version"] if scan else p.digest,
                          "candidates": scan["candidates"] if scan else [],
                          "selected_candidate_ids": scan["selected"] if scan else [],
                          "manual_boxes": scan["manual"] if scan else [],
                          "warnings": list(scan["result"].warnings) if scan else [],
                          "redacted_text": scan["result"].text if scan else ""})
        return {"review_id": review_id, "attachment_id": draft.scope[2], "reviewed_revision": draft.scope[1],
                "pages": pages, "sharing_authorized": False, "privacy_checked": draft.privacy_checked,
                "approved": draft.approved, "redacted_name": draft.redacted_name,
                "scanner_engine": draft.scanner_engine}

    @staticmethod
    def _check_versions(draft, versions):
        if len(draft.scans) != len(draft.pages):
            raise VisualReviewError("Scan every page before checking or approving this attachment.")
        expected = [draft.scans[p.index]["version"] for p in draft.pages]
        if versions != expected:
            raise VisualReviewError("A page changed. Review the current previews before continuing.")

    def prepare(self, review_id, scope, versions, *, conn, name, settings):
        from .visual_share import prepare_pages
        if not self._processing.acquire(blocking=False):
            raise VisualReviewError("Another attachment is being processed. Try again when it finishes.")
        try:
            with self._lock:
                draft = self._get(review_id, scope)
                self._check_versions(draft, versions)
                draft.approved = False
                draft.privacy_checked = False
            checked_name, results, engine = prepare_pages(conn, scope[0], name, draft.pages, draft.scans, settings)
            scans = {}
            for page, result in zip(draft.pages, results):
                scan = {**draft.scans[page.index], "result": result}
                scan["version"] = hashlib.sha256(result.png + json.dumps(
                    [result.text, scan["selected"], scan["manual"]], sort_keys=True).encode()).hexdigest()
                scans[page.index] = scan
            with self._lock:
                current = self._get(review_id, scope)
                new = Draft(scope, current.pages, current.touched, scans,
                            privacy_checked=True, redacted_name=checked_name, scanner_engine=engine)
                self._reserve(new.size, keep=review_id)
                self._drafts[review_id] = new
                return self._describe(review_id, new)
        finally:
            self._processing.release()

    def approve(self, review_id, scope, versions, reviewed_pages):
        with self._lock:
            if self._processing.locked():
                raise VisualReviewError("Wait for the privacy check to finish before approving pages.")
            draft = self._get(review_id, scope)
            self._check_versions(draft, versions)
            if (not draft.privacy_checked or not isinstance(reviewed_pages, list)
                    or any(type(i) is not int for i in reviewed_pages)
                    or sorted(reviewed_pages) != list(range(len(draft.pages)))):
                raise VisualReviewError("Run the full privacy check and explicitly review every page before approving.")
            draft.approved = True
            return self._describe(review_id, draft)

    def export_approved(self, review_id, scope):
        from .visual_share import approval_payload
        with self._lock:
            if self._processing.locked():
                raise VisualReviewError("An attachment is changing. Wait for processing to finish.")
            draft = self._get(review_id, scope)
            if not draft.approved or not draft.privacy_checked:
                raise VisualReviewError("Approve the current redacted pages before including this attachment.")
            return copy.deepcopy(approval_payload(scope, draft.redacted_name, draft.pages, draft.scans, draft.scanner_engine))

    def page(self, review_id, scope, index, variant, version):
        with self._lock:
            draft = self._get(review_id, scope)
            if type(index) is not int or not 0 <= index < len(draft.pages) or variant not in {"original", "redacted"}:
                raise VisualReviewError("Invalid page preview request.", 400)
            page = draft.pages[index]
            scan = draft.scans.get(index)
            current_version = scan["version"] if scan else page.digest
            if version != current_version:
                raise VisualReviewError("The page preview changed. Refresh the visual review.")
            if variant == "original":
                return page.png
            if scan is None:
                raise VisualReviewError("Scan the page before requesting its redacted preview.")
            return scan["result"].png

    def update(self, review_id, scope, index, *, version, custom_strings=(), selected=None, manual=None):
        if not self._processing.acquire(blocking=False):
            raise VisualReviewError("Another attachment is being processed. Try again when it finishes.")
        try:
            with self._lock:
                draft = self._get(review_id, scope)
                if type(index) is not int or not 0 <= index < len(draft.pages):
                    raise VisualReviewError("Invalid page index.", 400)
                page = draft.pages[index]
                old = draft.scans.get(index)
                if version != (old["version"] if old else page.digest):
                    raise VisualReviewError("The page changed. Refresh it before changing redactions.")
            if old is None:
                if selected is not None or manual is not None:
                    raise VisualReviewError("Scan this page before changing its redactions.", 400)
                ocr = visual.recognize_text(page)
                spans = visual.candidate_spans(ocr, custom_strings)
                candidates = [{"id": i, "text": ocr.text[start:end], "start": start, "end": end}
                              for i, (start, end) in enumerate(spans)]
                selected = list(range(len(candidates)))
                manual = []
            else:
                ocr, candidates = old["ocr"], old["candidates"]
                selected = old["selected"] if selected is None else selected
                manual = old["manual"] if manual is None else manual
            if (not isinstance(selected, list) or any(type(i) is not int for i in selected)
                    or len(selected) != len(set(selected)) or not set(selected) <= set(range(len(candidates)))):
                raise VisualReviewError("Invalid suggested-redaction choices.", 400)
            if not isinstance(manual, list) or len(manual) > MAX_MASKS:
                raise VisualReviewError("Too many manual redaction rectangles.", 400)
            spans = [(c["start"], c["end"]) for c in candidates if c["id"] in selected]
            result = visual.redact_page(page, ocr, spans, manual)
            version = hashlib.sha256(result.png + json.dumps([result.text, selected, manual], sort_keys=True).encode()).hexdigest()
            scan = {"ocr": ocr, "candidates": candidates, "selected": list(selected), "manual": manual,
                    "result": result, "version": version}
            with self._lock:
                current = self._get(review_id, scope)
                new = Draft(current.scope, current.pages, current.touched, {**current.scans, index: scan})
                self._reserve(new.size, keep=review_id)
                self._drafts[review_id] = new
                return self._describe(review_id, new)
        finally:
            self._processing.release()


reviews = VisualReviews()


def handle(handler, session_id, *, body=None, params=None):
    """Called only after daemon bearer authentication. No files or approvals saved."""
    from . import daemon
    from ..redaction.boundaries import RedactionBoundaryError

    def context(attachment_id, revision):
        if not isinstance(attachment_id, str) or not attachment_id or not isinstance(revision, str) or not revision:
            raise VisualReviewError("Attachment ID and source revision are required.", 400)
        with daemon.open_index() as conn:
            detail = daemon.get_session_detail(conn, session_id)
            if detail is None:
                raise VisualReviewError("Session not found.", 404)
            settings = daemon.get_effective_share_settings(conn, daemon.load_config())
        if revision != detail["content_revision"]:
            raise VisualReviewError("The conversation changed. Refresh the attachment list.")
        from .visual_share import profile_hash
        fingerprint = profile_hash(settings)
        return detail, settings, (session_id, revision, attachment_id, fingerprint)

    try:
        if params is not None:
            values = {k: v[0] for k, v in params.items() if v}
            detail, settings, scope = context(values.get("attachment_id"), values.get("expected_revision"))
            try:
                page_index = int(values.get("page", ""))
            except ValueError as exc:
                raise VisualReviewError("Invalid page index.", 400) from exc
            data = reviews.page(values.get("review_id"), scope, page_index, values.get("variant"), values.get("version"))
            handler.send_response(200)
            handler.send_header("Content-Type", "image/png")
            handler.send_header("Content-Length", str(len(data)))
            handler.send_header("Cache-Control", "no-store")
            handler.send_header("X-Content-Type-Options", "nosniff")
            handler.send_header("Cross-Origin-Resource-Policy", "same-origin")
            handler.end_headers()
            handler.wfile.write(data)
            return
        if not isinstance(body, dict):
            raise VisualReviewError("A visual review request is required.", 400)
        detail, settings, scope = context(body.get("attachment_id"), body.get("expected_revision"))
        action = body.get("action")
        if action == "start":
            result = reviews.start(detail, scope)
        elif action == "update":
            if not isinstance(body.get("review_id"), str):
                raise VisualReviewError("Visual review ID is required.", 400)
            # These initial suggestions are local. Full export scans and
            # anonymization must still run before any visual sharing is enabled.
            strings = [*settings["custom_strings"], *settings["extra_usernames"], *settings["blocked_domains"]]
            result = reviews.update(body["review_id"], scope, body.get("page"), version=body.get("version"),
                                    custom_strings=strings, selected=body.get("selected_candidate_ids"),
                                    manual=body.get("manual_boxes"))
        elif action in {"prepare", "approve"}:
            if not isinstance(body.get("review_id"), str):
                raise VisualReviewError("Visual review ID is required.", 400)
            if action == "prepare":
                from ..attachments import inventory
                name = next((e["name"] for e in inventory(detail) if e["id"] == scope[2]), None)
                if name is None:
                    raise VisualReviewError("The attachment changed. Refresh the list.")
                with daemon.open_index() as conn:
                    result = reviews.prepare(body["review_id"], scope, body.get("versions"), conn=conn,
                                             name=name, settings=settings)
            else:
                result = reviews.approve(body["review_id"], scope, body.get("versions"), body.get("reviewed_pages"))
        else:
            raise VisualReviewError("Unknown visual review action.", 400)
        # Rendering/OCR runs without holding the DB. Refuse a source/settings
        # change that happened while the expensive local work was in progress.
        if context(scope[2], scope[1])[2] != scope:
            raise VisualReviewError("Redaction settings changed. Start a new visual review.")
        daemon._json_response(handler, result, cache_control="no-store")
    except VisualReviewError as exc:
        daemon._json_response(handler, {"error": str(exc)}, exc.status, cache_control="no-store")
    except (AttachmentSelectionError, RedactionBoundaryError) as exc:
        daemon._json_response(handler, {"error": str(exc)}, 422, cache_control="no-store")
    except OSError:
        daemon._json_response(handler, {"error": "Local visual processing could not access its temporary files or scanner."}, 422, cache_control="no-store")
