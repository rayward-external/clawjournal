"""Opt-in, local-only redaction evaluation. Never consumed by share/export.

Originals are deliberately retained here, unlike the hashed findings store.
Labels describe selected spans, NOT exhaustive NER training annotations.
"""
from __future__ import annotations

import copy
import hashlib
import importlib.metadata
import json
import math
import os
from pathlib import Path
import sqlite3
import time
import uuid

POLICY = "actual-identifiers-v1"
POLICY_TEXT = (
    "Redact actual personal/contact/account identifiers, including public emails. "
    "Preserve scientific measurements, hyperparameters and ordinary numbers. "
    "Mark ambiguous cases uncertain. These are selected-span annotations, not exhaustive labels."
)
LABELS = ["person", "email", "phone_number", "user_name", "ip_address", "address",
          "credit_card_number", "bank_account_number", "social_security_number",
          "medical_record_number", "passport_number", "tax_id", "date_of_birth"]
MAX_INPUT = 16 * 1024 * 1024


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def private_write(path: Path, text: str):
    from ..paths import atomic_write_text
    if path.is_symlink():
        raise ValueError("Refusing a symlink output")
    atomic_write_text(path, text)
    path.chmod(0o600)


def text_units(session):
    """JSON-pointer keys identify every string in the normalized message payload."""
    result = {}

    def walk(value, path):
        if isinstance(value, str):
            result[path] = value
        elif isinstance(value, dict):
            for key, child in value.items():
                escaped = str(key).replace("~", "~0").replace("/", "~1")
                walk(child, path + "/" + escaped)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, path + "/" + str(i))

    walk(session.get("messages", []), "/messages")
    return result


def capture_preview(session):
    """Run the existing local preview, on a copy. This is not the final share gate."""
    from ..config import load_config
    from ..workbench.index import apply_share_redactions
    from . import betterleaks
    config = load_config()
    if any(os.environ.get(key) for key in
           ("CLAWJOURNAL_SKIP_BETTERLEAKS", "CLAWJOURNAL_SKIP_TRUFFLEHOG")):
        raise ValueError("Capture refuses scanner bypass flags")
    # Preview's scanner adapters fail soft. Record a checked local scan so a
    # missing/crashed scanner cannot silently masquerade as a complete capture.
    report = betterleaks.scan_text(canonical(session))
    if report.binary_missing or report.scan_error or report.bypassed:
        raise ValueError("Betterleaks scan failed; install/check the local scanner before capture")
    # Imported input has no saved finding decisions. Do not write to the live
    # index or accidentally inherit ignored findings from an unrelated session.
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE findings(session_id TEXT, entity_hash TEXT, status TEXT)")
    try:
        redacted, count, _log = apply_share_redactions(
            conn, copy.deepcopy(session),
            custom_strings=config.get("redact_strings", []),
            user_allowlist=config.get("allowlist_entries", []),
            extra_usernames=config.get("redact_usernames", []),
        )
    finally:
        conn.close()
    settings = {key: config.get(key, []) for key in
                ("redact_strings", "allowlist_entries", "redact_usernames")}
    return redacted, {"stage": "local-share-preview-before-final-gate", "changes": count,
                      "settings_sha256": digest(settings), "betterleaks": betterleaks.engine_fingerprint(),
                      "settings_scope": "config-only imported input; no indexed policies or saved finding decisions"}


class Store:
    """Separate opt-in SQLite store; no connection to upload manifests or index."""

    def __init__(self, root: Path, *, initialize=False, retention_days=30, max_mib=100):
        root = Path(root).expanduser().absolute()
        if any(p.is_symlink() for p in (root, *root.parents)):
            raise ValueError("Evaluation storage must not use symlinks")
        self.root = root
        db = root / "evaluation.db"
        if not initialize and not db.is_file():
            raise ValueError("Capture is not enabled: run redaction-eval init first")
        if not 1 <= retention_days <= 365 or not 1 <= max_mib <= 10240:
            raise ValueError("Retention must be 1–365 days; quota 1–10240 MiB")
        from ..filesystem import classify_state_storage
        if classify_state_storage(db).storage_risk == "network":
            raise ValueError("Use a local filesystem for original conversation storage")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        root.chmod(0o700)
        if db.is_symlink() or (root / "reviews").is_symlink():
            raise ValueError("Refusing a symlink in evaluation storage")
        if not db.exists():
            fd = os.open(db, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        db.chmod(0o600)
        self.conn = sqlite3.connect(db)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA secure_delete=ON")
        self.conn.execute("PRAGMA journal_mode=DELETE")
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK(id=1),
                    enabled INTEGER, retention_days INTEGER, quota INTEGER);
                CREATE TABLE IF NOT EXISTS groups (name TEXT PRIMARY KEY, split TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS captures (id TEXT PRIMARY KEY, source_hash TEXT NOT NULL,
                    pair_hash TEXT UNIQUE NOT NULL, group_name TEXT NOT NULL REFERENCES groups(name),
                    created REAL NOT NULL, expires REAL NOT NULL, payload TEXT NOT NULL,
                    size INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS corrections (capture_id TEXT PRIMARY KEY
                    REFERENCES captures(id) ON DELETE CASCADE, payload TEXT NOT NULL);
            """)
            self.conn.execute("INSERT OR IGNORE INTO settings VALUES(1,1,?,?)",
                              (retention_days, max_mib * 1024 * 1024))
        self.prune()

    def close(self):
        self.conn.close()

    def settings(self):
        return dict(self.conn.execute("SELECT * FROM settings").fetchone())

    def disable(self):
        with self.conn:
            self.conn.execute("UPDATE settings SET enabled=0")

    def delete(self, capture_id):
        self.get(capture_id)
        from .contributions import purge_capture
        purge_capture(self, capture_id)
        review = self.root / "reviews" / (capture_id + ".html")
        review.unlink(missing_ok=True)
        with self.conn:
            self.conn.execute("DELETE FROM captures WHERE id=?", (capture_id,))
            self.conn.execute("DELETE FROM groups WHERE name NOT IN (SELECT group_name FROM captures)")

    def prune(self):
        ids = [r[0] for r in self.conn.execute("SELECT id FROM captures WHERE expires<=?", (time.time(),))]
        for capture_id in ids:
            self.delete(capture_id)
        return len(ids)

    def list(self):
        return [dict(r) for r in self.conn.execute("""
            SELECT c.id,c.group_name,g.split,c.created,c.expires,c.revision,c.size
            FROM captures c JOIN groups g ON g.name=c.group_name ORDER BY c.created,c.id
        """)]

    def get(self, capture_id):
        if not isinstance(capture_id, str):
            raise ValueError("Expected a capture ID")
        row = self.conn.execute("SELECT * FROM captures WHERE id=?", (capture_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown or expired capture")
        result = json.loads(row["payload"])
        correction = self.conn.execute("SELECT payload FROM corrections WHERE capture_id=?", (capture_id,)).fetchone()
        result.update(id=capture_id, pair_hash=row["pair_hash"], revision=row["revision"],
                      annotations=json.loads(correction[0]) if correction else [])
        return result

    def capture(self, original, *, group, split, source_kind, provenance, preview=capture_preview):
        if not self.settings()["enabled"]:
            raise ValueError("Capture is disabled; existing examples remain reviewable")
        if split not in ("train", "dev", "test"):
            raise ValueError("Invalid split")
        if source_kind not in ("owned", "contributor-approved", "licensed", "synthetic"):
            raise ValueError("Record an authorized source kind")
        if not group.strip() or len(group) > 100 or not provenance.strip() or len(provenance) > 1000:
            raise ValueError("Provide a short pseudonymous group and provenance note")
        if not isinstance(original, dict) or not isinstance(original.get("messages"), list):
            raise ValueError("Expected a normalized session object with messages")
        if len(canonical(original).encode()) > MAX_INPUT:
            raise ValueError("Capture input exceeds 16 MiB")
        original = copy.deepcopy(original)
        # ID is transport metadata, not a way to bypass source/group checks.
        source_hash = digest({k: v for k, v in original.items() if k != "session_id"})
        original.setdefault("session_id", "evaluation-" + source_hash[:16])
        redacted, metadata = preview(copy.deepcopy(original))
        payload = {"original": original, "redacted": redacted, "metadata": metadata,
                   "policy": POLICY, "source_kind": source_kind, "provenance": provenance,
                   "group": group, "split": split}
        pair_hash = digest(payload)
        serialized = canonical(payload)
        size = len(serialized.encode())
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            existing_group = self.conn.execute("SELECT split FROM groups WHERE name=?", (group,)).fetchone()
            if existing_group and existing_group[0] != split:
                raise ValueError("Related examples must remain in the same split")
            duplicates = self.conn.execute("SELECT group_name FROM captures WHERE source_hash=?", (source_hash,)).fetchall()
            if any(r[0] != group for r in duplicates):
                raise ValueError("This source already belongs to a different group")
            existing = self.conn.execute("SELECT id FROM captures WHERE pair_hash=?", (pair_hash,)).fetchone()
            if existing:
                self.conn.rollback()
                return existing[0]
            used = self.conn.execute("SELECT COALESCE(SUM(size),0) FROM captures").fetchone()[0]
            if used + size > self.settings()["quota"]:
                raise ValueError("Original/redacted payload quota exceeded; delete captures first")
            capture_id = uuid.uuid4().hex
            now = time.time()
            self.conn.execute("INSERT OR IGNORE INTO groups VALUES(?,?)", (group, split))
            self.conn.execute("INSERT INTO captures VALUES(?,?,?,?,?,?,?,?,0)",
                              (capture_id, source_hash, pair_hash, group, now,
                               now + self.settings()["retention_days"] * 86400, serialized, size))
            self.conn.commit()
            return capture_id
        except BaseException:
            self.conn.rollback()
            raise

    def import_corrections(self, document):
        if not isinstance(document, dict):
            raise ValueError("Expected a correction object")
        capture = self.get(document.get("capture_id"))
        if document.get("policy") != POLICY or document.get("pair_hash") != capture["pair_hash"]:
            raise ValueError("Correction file does not match capture/policy")
        revision = document.get("revision")
        if type(revision) is not int or revision != capture["revision"]:
            raise ValueError("Stale corrections: reopen the review page")
        annotations = document.get("annotations")
        if not isinstance(annotations, list) or len(annotations) > 10000:
            raise ValueError("Expected at most 10000 annotations")
        units = text_units(capture["original"])
        clean, spans = [], {}
        for a in annotations:
            if not isinstance(a, dict):
                raise ValueError("Expected annotation objects")
            unit, start, end = a.get("unit"), a.get("start"), a.get("end")
            if not isinstance(unit, str) or unit not in units or type(start) is not int or type(end) is not int or not 0 <= start < end <= len(units[unit]):
                raise ValueError("Invalid annotation offsets (use Unicode code points)")
            if a.get("decision") not in ("redact", "preserve", "uncertain"):
                raise ValueError("Invalid annotation decision")
            if a.get("review_status") not in ("assistant_draft", "human_reviewed"):
                raise ValueError("Specify assistant_draft or human_reviewed")
            entity = a.get("entity_type", "")
            note = a.get("note", "")
            if not isinstance(entity, str) or len(entity) > 80 or not isinstance(note, str) or len(note) > 1000:
                raise ValueError("Invalid entity type or note")
            if a["decision"] == "redact" and not entity.strip():
                raise ValueError("Redact labels require an entity type")
            if any(s < end and e > start for s, e in spans.setdefault(unit, [])):
                raise ValueError("Annotations must not overlap")
            spans[unit].append((start, end))
            clean.append({"unit": unit, "start": start, "end": end, "decision": a["decision"],
                          "entity_type": entity, "note": note, "review_status": a["review_status"]})
        clean.sort(key=lambda a: (a["unit"], a["start"], a["end"]))
        with self.conn:
            changed = self.conn.execute("UPDATE captures SET revision=revision+1 WHERE id=? AND revision=?",
                                        (capture["id"], revision)).rowcount
            if changed != 1:
                raise ValueError("Stale corrections: reopen the review page")
            self.conn.execute("INSERT OR REPLACE INTO corrections VALUES(?,?)", (capture["id"], canonical(clean)))
        # Remove old locally generated pages; downloaded copies have revision protection.
        (self.root / "reviews" / (capture["id"] + ".html")).unlink(missing_ok=True)
        from .contributions import purge_capture
        purge_capture(self, capture["id"])
        return len(clean)

    def review(self, capture_id):
        capture = self.get(capture_id)
        data = {"capture_id": capture_id, "pair_hash": capture["pair_hash"], "revision": capture["revision"],
                "policy": POLICY, "policy_text": POLICY_TEXT, "annotations": capture["annotations"],
                "original": text_units(capture["original"]), "redacted": text_units(capture["redacted"])}
        template = Path(__file__).with_name("evaluation_review.html").read_text()
        # No user content is interpreted as HTML, including </script> in a conversation.
        encoded = canonical(data).replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
        directory = self.root / "reviews"
        directory.mkdir(mode=0o700, exist_ok=True)
        directory.chmod(0o700)
        path = directory / (capture_id + ".html")
        private_write(path, template.replace("__CAPTURE_DATA__", encoded))
        return path


def metrics(tp, fp, fn, tn):
    def ratio(a, b):
        return a / b if b else None
    return {"TP": tp, "FP": fp, "FN": fn, "TN": tn,
            "accuracy": ratio(tp + tn, tp + fp + fn + tn), "precision": ratio(tp, tp + fp),
            "TPR_recall": ratio(tp, tp + fn), "FNR": ratio(fn, tp + fn),
            "TNR_specificity": ratio(tn, tn + fp), "FPR": ratio(fp, tn + fp),
            "F1": ratio(2 * tp, 2 * tp + fp + fn)}


def context_window(text, start, end, radius=120):
    if end - start > 256:
        raise ValueError("Select targets of at most 256 characters for candidate evaluation")
    left, right = max(0, start - radius), min(len(text), end + radius)
    return text[left:right], start - left, end - left


def rules_predictor():
    from .pii import scan_text_for_pii
    return scan_text_for_pii, {"engine": "rules", "scope": "PII regex baseline, not complete share pipeline",
                              "code_sha256": hashlib.sha256(Path(__file__).with_name("pii.py").read_bytes()).hexdigest()}


def gliner_predictor(model_dir, threshold):
    if not model_dir or not Path(model_dir).is_dir():
        raise ValueError("GLiNER requires a downloaded local --model-dir")
    if not math.isfinite(threshold) or not 0 < threshold < 1:
        raise ValueError("Threshold must be between 0 and 1")
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                      HF_HUB_DISABLE_TELEMETRY="1", HF_HUB_DISABLE_IMPLICIT_TOKEN="1",
                      TOKENIZERS_PARALLELISM="false")
    try:
        import onnxruntime
        onnxruntime.disable_telemetry_events()
        import torch
        from gliner import GLiNER
    except ImportError as exc:
        raise ValueError("Use a separate environment with GLiNER and onnxruntime installed") from exc
    torch.set_num_threads(4)
    torch.manual_seed(0)
    model = GLiNER.from_pretrained(str(Path(model_dir).resolve()), local_files_only=True, map_location="cpu")
    model.eval()
    tokenizer = model.data_processor.transformer_tokenizer
    # Small, explicit windows prevent silent truncation. Reserve room for entity prompts.
    def predict(text):
        if len(tokenizer.encode(text, add_special_tokens=True)) > 256:
            raise ValueError("A context exceeds 256 model tokens; shorten the example before evaluating")
        with torch.inference_mode():
            return model.predict_entities(text, LABELS, threshold=threshold, flat_ner=False)
    files = {}
    for path in sorted(Path(model_dir).rglob("*")):
        if path.is_file() and not any(part.startswith(".") for part in path.relative_to(model_dir).parts):
            h = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                    h.update(chunk)
            files[str(path.relative_to(model_dir))] = h.hexdigest()
    return predict, {"engine": "gliner", "threshold": threshold, "labels": LABELS,
                     "model_files_sha256": files,
                     "versions": {name: importlib.metadata.version(name) for name in ("gliner", "torch", "transformers")}}


def evaluate(store, predict, engine_info, *, split="dev", include_drafts=False):
    rows, excluded, groups = [], {"draft": 0, "uncertain": 0, "duplicate": 0}, set()
    seen = {}
    for summary in store.list():
        if summary["split"] != split:
            continue
        capture = store.get(summary["id"])
        units = text_units(capture["original"])
        for annotation in capture["annotations"]:
            if annotation["decision"] == "uncertain":
                excluded["uncertain"] += 1
                continue
            if annotation["review_status"] != "human_reviewed" and not include_drafts:
                excluded["draft"] += 1
                continue
            # Recapturing the same source after a redactor/config change must
            # not inflate the number of independent labelled examples.
            key = digest({"text": units[annotation["unit"]], "group": capture["group"],
                          "unit": annotation["unit"], "start": annotation["start"], "end": annotation["end"]})
            answer = (annotation["decision"], annotation["entity_type"])
            if key in seen:
                if seen[key] != answer:
                    raise ValueError("Duplicate targets have conflicting labels; reconcile the corrections")
                excluded["duplicate"] += 1
                continue
            seen[key] = answer
            text, start, end = context_window(units[annotation["unit"]], annotation["start"], annotation["end"])
            rows.append({"capture_id": capture["id"], "pair_hash": capture["pair_hash"],
                         "annotation": annotation, "text": text, "start": start, "end": end,
                         "group": capture["group"], "source_kind": capture["source_kind"]})
            groups.add(capture["group"])
    rows.sort(key=lambda r: (r["pair_hash"], r["annotation"]["unit"], r["annotation"]["start"]))
    if not rows:
        raise ValueError("No eligible labels. Review/import corrections, or explicitly use --include-drafts")
    tp = fp = fn = tn = partial = covered = 0
    cases, sources = [], {}
    for row in rows:
        predictions = predict(row["text"])
        # A broken model/scanner must never be counted as a clean negative.
        for p in predictions:
            if type(p.get("start")) is not int or type(p.get("end")) is not int or not 0 <= p["start"] < p["end"] <= len(row["text"]):
                raise ValueError("Predictor returned invalid offsets")
        hits = [p for p in predictions if p["start"] < row["end"] and p["end"] > row["start"]]
        sensitive = row["annotation"]["decision"] == "redact"
        # Every character must be covered; partial matches are NOT full protection.
        full = all(any(p["start"] <= i < p["end"] for p in hits) for i in range(row["start"], row["end"]))
        tp += int(sensitive and bool(hits)); fn += int(sensitive and not hits)
        fp += int(not sensitive and bool(hits)); tn += int(not sensitive and not hits)
        covered += int(sensitive and full)
        partial += int(sensitive and bool(hits) and not full)
        sources[row["source_kind"]] = sources.get(row["source_kind"], 0) + 1
        cases.append({"capture_id": row["capture_id"], "unit": row["annotation"]["unit"],
                      "start": row["annotation"]["start"], "end": row["annotation"]["end"],
                      "decision": row["annotation"]["decision"], "detected": bool(hits),
                      "fully_covered": full if sensitive else None,
                      "predicted_types": sorted({p.get("label", p.get("type", p.get("rule", "unknown"))) for p in hits})})
    return {"schema_version": 1, "policy": POLICY, "policy_text": POLICY_TEXT,
            "scope": "Selected targets in bounded context; not whole-conversation recall or export approval",
            "engine": engine_info, "split": split, "include_drafts": include_drafts,
            "dataset_sha256": digest({"policy": POLICY, "split": split, "rows": rows}),
            "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            "context_radius_characters": 120, "targets": len(rows), "groups": len(groups),
            "source_counts": sources, "excluded": excluded,
            "detection_metrics": metrics(tp, fp, fn, tn),
            "sensitive_span_coverage": {"total": tp + fn, "fully_covered": covered,
                                        "partial": partial, "missed": fn,
                                        "full_coverage_rate": covered / (tp + fn) if tp + fn else None},
            "cases": cases}
