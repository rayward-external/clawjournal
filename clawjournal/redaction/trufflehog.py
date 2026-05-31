"""TruffleHog post-redaction scanner.

Runs as a mandatory gate on every share export: after our layered
redaction produces the final ``sessions.jsonl``, TruffleHog scans
that output as an independent oracle. Any finding (verified,
unverified, or unknown) blocks the export and leaves the directory
intact for debugging.

TruffleHog is invoked as a subprocess — it is AGPL-3.0 and must not
be linked in-process. Install via ``brew install trufflehog`` or the
upstream Go binary. The escape hatch ``CLAWJOURNAL_SKIP_TRUFFLEHOG=1``
exists for CI / development only and is recorded in the share
manifest so downstream reviewers can tell a scanned share from a
bypassed one.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from ..findings import RawFinding

TRUFFLEHOG_ENGINE_ID = "trufflehog"

SKIP_ENV_VAR = "CLAWJOURNAL_SKIP_TRUFFLEHOG"

_MAX_SCAN_BYTES = 200 * 1024 * 1024  # 200 MB — cap on engine-path payload


def _scrubbed_subprocess_env() -> dict[str, str]:
    """Minimal env for TruffleHog subprocess invocations.

    The parent process may hold API keys and cloud credentials
    (ANTHROPIC_API_KEY, OPENAI_API_KEY, GCS service-account JSON, …).
    A malicious ``trufflehog`` binary on PATH would inherit all of
    them by default. Restrict to what the binary genuinely needs:

    - ``PATH`` to exec helpers
    - ``HOME`` / ``TMPDIR`` for config + workspace
    - locale vars for UTF-8 decoding
    - **Network trust**: ``SSL_CERT_FILE`` / ``SSL_CERT_DIR`` so
      verification against provider APIs works on distros that rely
      on env-specified CA bundles; proxy vars so the same works
      behind a corporate HTTPS proxy. Without these, provider
      verification silently fails and real secrets get downgraded
      from ``verified`` to ``unknown``.
    """
    keep = (
        "PATH", "HOME", "TMPDIR",
        "LANG", "LC_ALL", "LC_CTYPE",
        "SSL_CERT_FILE", "SSL_CERT_DIR",
        "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "no_proxy",
    )
    return {k: os.environ[k] for k in keep if k in os.environ}

# Detectors we ask TruffleHog to skip. The gate's "any finding blocks"
# contract is deliberately strict, so this list exists only for detectors
# that collide with structural content in agent session files.
#
# - ``refiner`` (refiner.io user-feedback platform): its pattern is
#   "the word 'refiner' followed by a UUID", which trips on any project
#   name containing that substring (e.g. ``tracerefinery``) since
#   Claude/Codex session files are full of tool-use / session UUIDs
#   stored nearby. Verification against api.refiner.io correctly
#   returns ``unverified`` for those, so they are never real leaks.
EXCLUDED_DETECTORS: tuple[str, ...] = ("refiner",)

# Detectors whose UNVERIFIED findings are dropped as structural false
# positives, while their verified/unknown findings still block. This is
# narrower than EXCLUDED_DETECTORS (which disables a detector entirely,
# reserved for detectors that never catch real leaks): the detector stays
# active, so a real, live secret it confirms still blocks the share.
#
# - ``Azure`` is TruffleHog's legacy generic Azure detector (type 3). Its
#   broad pattern, fed through the PLAIN and BASE64 decoders, matches ordinary
#   agent-session terminal content — ``127.0.0.1:5432`` connection lines, file
#   paths like ``scripts/foo.sh``, ANSI color codes (``\x1b[0m``) — and
#   Azure-API verification returns unverified for all of them. The specific
#   Azure detectors (AzureStorage, AzureSAS, AzureBatch, …) stay active and
#   still catch real Azure keys by type.
NOISY_UNVERIFIED_DETECTORS: frozenset[str] = frozenset({"Azure"})

INSTALL_HINT = (
    "TruffleHog is required to export shares but was not found on PATH.\n"
    "Install it with:\n"
    "  macOS:  brew install trufflehog\n"
    "  Linux:  https://github.com/trufflesecurity/trufflehog#floppy_disk-installation\n"
    "Or set CLAWJOURNAL_SKIP_TRUFFLEHOG=1 to bypass (unsafe — the share "
    "may leak secrets that survived our redaction layers)."
)

FindingStatus = Literal["verified", "unverified", "unknown"]


@dataclass
class TruffleHogFinding:
    detector: str
    status: FindingStatus
    line: int | None
    masked: str
    raw_sha256: str | None


@dataclass
class TruffleHogReport:
    scanned_path: str
    scanned_sha256: str
    findings: list[TruffleHogFinding] = field(default_factory=list)
    verified: int = 0
    unverified: int = 0
    unknown: int = 0
    top_detectors: list[str] = field(default_factory=list)
    bypassed: bool = False
    binary_missing: bool = False
    scan_error: str | None = None

    @property
    def blocking(self) -> bool:
        if self.bypassed:
            return False
        if self.binary_missing:
            return True
        if self.scan_error:
            return True
        return len(self.findings) > 0

    @property
    def block_reason(self) -> str | None:
        if self.bypassed:
            return None
        if self.binary_missing:
            return "trufflehog-not-installed"
        if self.scan_error:
            return "trufflehog-error"
        if self.findings:
            return "trufflehog-findings"
        return None

    def summary(self) -> dict:
        """Public summary safe for the share manifest — no raw values."""
        return {
            "findings": len(self.findings),
            "verified": self.verified,
            "unverified": self.unverified,
            "unknown": self.unknown,
            "top_detectors": list(self.top_detectors),
            "bypassed": self.bypassed,
            "binary_missing": self.binary_missing,
            "scan_error": self.scan_error,
            "examples": [
                {
                    "detector": f.detector,
                    "status": f.status,
                    "line": f.line,
                    "masked": f.masked,
                }
                for f in self.findings[:5]
            ],
        }


def is_available() -> bool:
    return shutil.which("trufflehog") is not None


_version_cache: dict[tuple, str] = {}

_VERSION_RE = re.compile(r"\btrufflehog\s+(\d+\.\d+\.\d+)", re.IGNORECASE)


def _binary_signature() -> tuple:
    """Stable per-binary key for the fingerprint cache.

    Folds the resolved path and its mtime+size so a ``brew upgrade``
    (new inode, new size, new mtime) invalidates cached entries in
    a long-running daemon without requiring a restart. Returns
    ``("missing",)`` if the binary isn't on PATH.
    """
    resolved = shutil.which("trufflehog")
    if resolved is None:
        return ("missing",)
    try:
        st = os.stat(resolved)
    except OSError:
        return ("unknown", resolved)
    return (resolved, st.st_mtime_ns, st.st_size)


def engine_fingerprint() -> str:
    """Return a short fingerprint folded into ``findings_revision``.

    Captures presence + version so cached session revisions invalidate
    when the user installs or upgrades TruffleHog. Missing binary
    reports ``"missing"``; parse/timeout errors report ``"unknown"``.
    Result is memoized per ``(path, mtime, size)`` tuple so a long-
    running daemon notices upgrades without a restart.
    """
    signature = _binary_signature()
    cached = _version_cache.get(signature)
    if cached is not None:
        return cached
    if signature[0] == "missing":
        _version_cache[signature] = "missing"
        return "missing"
    try:
        result = subprocess.run(
            ["trufflehog", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env=_scrubbed_subprocess_env(),
        )
        blob = (result.stdout or "") + "\n" + (result.stderr or "")
        match = _VERSION_RE.search(blob)
        if match:
            fingerprint = f"trufflehog {match.group(1)}"
        else:
            # Fallback for an unrecognized banner — take the first
            # non-empty line verbatim.
            lines = [ln.strip() for ln in blob.splitlines() if ln.strip()]
            fingerprint = lines[0] if lines else "unknown"
        _version_cache[signature] = fingerprint
        return fingerprint
    except (subprocess.TimeoutExpired, OSError):
        _version_cache[signature] = "unknown"
        return "unknown"


def reset_version_cache() -> None:
    """Clear the cached version fingerprint — for tests only."""
    _version_cache.clear()


def is_bypassed() -> bool:
    return os.environ.get(SKIP_ENV_VAR) == "1"


def mask_secret(raw: str) -> str:
    """Partial-mask a raw secret for human triage.

    Preserves the first 4 chars and last 4 chars so reviewers can
    recognize the credential type without seeing the full value.
    ``npm_``-prefixed tokens keep 8 leading chars since the prefix
    itself is the type marker.
    """
    if len(raw) <= 8:
        return "***"
    prefix_len = min(8, len(raw) - 4) if raw.startswith("npm_") else min(4, len(raw) - 4)
    suffix_len = min(4, len(raw) - prefix_len)
    return f"{raw[:prefix_len]}***{raw[-suffix_len:]}"


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return f"sha256:{h.hexdigest()}"


def _classify_trufflehog_status(parsed: dict) -> FindingStatus:
    """Status rules, matching TruffleHog's JSONL schema:

    - ``Verified: true``  → verified (provider API said yes)
    - ``VerificationError`` non-empty at top level → unknown (provider
      unreachable / auth error / DNS failure; likely real but unprovable)
    - fallback → unverified (pattern matched, no verification run or
      provider said no)

    ``VerificationError`` is a **top-level** field on TruffleHog's
    output struct (see ``pkg/output/json.go``), not nested under
    ``ExtraData``. Earlier iterations of this parser probed only the
    nested path and mis-classified every verification failure as
    ``unverified``.
    """
    if parsed.get("Verified") is True:
        return "verified"
    top_err = parsed.get("VerificationError")
    if isinstance(top_err, str) and top_err.strip():
        return "unknown"
    # ExtraData probe kept as a defensive fallback for older/future
    # TruffleHog versions that might move the field.
    extra = parsed.get("ExtraData") if isinstance(parsed.get("ExtraData"), dict) else None
    if extra:
        for key in ("verification_error", "verificationError", "error"):
            value = extra.get(key)
            if isinstance(value, str) and value.strip():
                return "unknown"
    return "unverified"


def _parse_finding(parsed: dict) -> TruffleHogFinding | None:
    detector = parsed.get("DetectorName")
    if not isinstance(detector, str):
        return None

    status: FindingStatus = _classify_trufflehog_status(parsed)

    line_no: int | None = None
    source_metadata = parsed.get("SourceMetadata")
    if isinstance(source_metadata, dict):
        data = source_metadata.get("Data")
        if isinstance(data, dict):
            filesystem = data.get("Filesystem")
            if isinstance(filesystem, dict) and isinstance(filesystem.get("line"), int):
                line_no = filesystem["line"]

    raw = parsed.get("Raw")
    raw_str = raw if isinstance(raw, str) and raw else None
    raw_sha = (
        f"sha256:{hashlib.sha256(raw_str.encode()).hexdigest()}" if raw_str else None
    )
    masked = mask_secret(raw_str) if raw_str else "[REDACTED]"

    return TruffleHogFinding(
        detector=detector,
        status=status,
        line=line_no,
        masked=masked,
        raw_sha256=raw_sha,
    )


def scan_file(path: Path) -> TruffleHogReport:
    """Scan ``path`` with TruffleHog. Returns a report.

    Never raises on missing-binary, findings, subprocess timeouts,
    spawn failures, unexpected exit statuses, or I/O errors reading
    the scanned path — every failure is surfaced as a blocking
    report so the mandatory share gate fails closed rather than
    propagating a raw exception to the caller.
    """
    try:
        scanned_sha256 = _sha256_file(path)
    except OSError as exc:
        # Permission denied / missing file / mid-read disk error:
        # turn into a blocking report with scan_error instead of
        # bubbling up to export_share_to_disk and aborting the share
        # export with no block_reason persisted.
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256="",
            scan_error=f"could not hash scan target: {exc.__class__.__name__}",
        )

    if is_bypassed():
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            bypassed=True,
        )

    if not is_available():
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            binary_missing=True,
        )

    args = [
        "trufflehog",
        "filesystem",
        str(path),
        "-j",
        "--results=verified,unknown,unverified",
        "--no-color",
        "--no-update",
    ]
    if EXCLUDED_DETECTORS:
        args.append(f"--exclude-detectors={','.join(EXCLUDED_DETECTORS)}")

    # Hard-limit the subprocess so a hung scan can't wedge the share
    # export. DEVNULL on stdin prevents any interactive prompt from
    # deadlocking a non-interactive runner. Scrubbed env keeps the
    # parent process's API keys out of the child.
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
            stdin=subprocess.DEVNULL,
            env=_scrubbed_subprocess_env(),
        )
    except subprocess.TimeoutExpired:
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            scan_error="timed out after 60 seconds",
        )
    except OSError:
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            scan_error="could not execute the trufflehog binary",
        )
    # TruffleHog exits 0 on clean and 183 on findings in some versions;
    # accept either as a successful scan.
    if result.returncode not in (0, 183):
        return TruffleHogReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            scan_error=f"unexpected exit status {result.returncode}",
        )

    findings: list[TruffleHogFinding] = []
    detector_counts: dict[str, int] = {}
    verified = unverified = unknown = 0
    seen_keys: set[tuple] = set()

    for raw_line in result.stdout.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        finding = _parse_finding(parsed)
        if finding is None:
            continue
        # Drop structural false positives from broad detectors before they can
        # block the gate (see NOISY_UNVERIFIED_DETECTORS). Only unverified
        # findings are dropped; verified/unknown ones still block.
        if (
            finding.status == "unverified"
            and finding.detector in NOISY_UNVERIFIED_DETECTORS
        ):
            continue
        key = (finding.detector, finding.status, finding.line, finding.raw_sha256)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        findings.append(finding)
        detector_counts[finding.detector] = detector_counts.get(finding.detector, 0) + 1
        if finding.status == "verified":
            verified += 1
        elif finding.status == "unverified":
            unverified += 1
        else:
            unknown += 1

    findings.sort(key=lambda f: (f.line if f.line is not None else 10**9, f.detector))
    top = sorted(detector_counts.items(), key=lambda item: (-item[1], item[0]))[:8]

    return TruffleHogReport(
        scanned_path=str(path),
        scanned_sha256=scanned_sha256,
        findings=findings,
        verified=verified,
        unverified=unverified,
        unknown=unknown,
        top_detectors=[detector for detector, _ in top],
    )


def scan_text(text: str) -> TruffleHogReport:
    """Scan an in-memory string by dropping it to a temp file.

    Used by the Redact step, which wants a per-session preview scan
    without managing its own temp dir. The final authoritative gate
    still runs on the merged sessions.jsonl at Package time.
    """
    import tempfile

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(text)
        tmp_path = Path(tf.name)
    try:
        return scan_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_report(path: Path, report: TruffleHogReport) -> None:
    payload = {
        # Record only the basename so tempfile paths (e.g., /var/folders/...
        # on macOS) don't leak the user's tmpdir structure into the
        # manifest bundle.
        "scanned_path": Path(report.scanned_path).name,
        "scanned_sha256": report.scanned_sha256,
        "bypassed": report.bypassed,
        "binary_missing": report.binary_missing,
        "scan_error": report.scan_error,
        "findings": [
            {
                "detector": f.detector,
                "status": f.status,
                "line": f.line,
                "masked": f.masked,
                "raw_sha256": f.raw_sha256,
            }
            for f in report.findings
        ],
        "summary": report.summary(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _scan_text_for_raw_matches(text: str) -> list[dict]:
    """Internal: run TruffleHog on ``text`` and return raw-bearing dicts.

    Used only by the findings-engine entry points — the apply path
    needs ``raw`` to build the replace map and to compute salted
    hashes, but raw values must never be persisted or returned from
    public ``scan_*`` helpers.

    Returns ``[{"raw": str, "detector": str, "status": str}]``.
    Silently returns ``[]`` when the binary is missing or bypassed;
    the findings pipeline should not fail a scan just because the
    optional engine is unavailable.
    """
    if is_bypassed() or not is_available() or not text.strip():
        return []

    try:
        encoded = text.encode("utf-8", errors="replace")
    except (UnicodeError, AttributeError):
        return []
    if len(encoded) > _MAX_SCAN_BYTES:
        return []

    args = [
        "trufflehog",
        "stdin",
        "-j",
        "--results=verified,unknown,unverified",
        "--no-color",
        "--no-update",
    ]
    if EXCLUDED_DETECTORS:
        args.append(f"--exclude-detectors={','.join(EXCLUDED_DETECTORS)}")

    # Stream the payload via stdin — no tempfile on disk means a
    # SIGKILL between scan and cleanup cannot leave plaintext secrets
    # behind. Scrubbed env prevents a malicious binary on PATH from
    # seeing the parent process's API keys / credentials. Hard
    # timeout so a hung scan can't block the findings pipeline.
    try:
        result = subprocess.run(
            args,
            input=encoded,
            capture_output=True,
            check=False,
            timeout=30,
            env=_scrubbed_subprocess_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return []
    if result.returncode not in (0, 183):
        # Engine path intentionally fails soft — a broken scan
        # shouldn't block the whole findings rebuild.
        return []

    out: list[dict] = []
    seen: set[tuple] = set()
    stdout = result.stdout.decode("utf-8", errors="replace")
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        detector = parsed.get("DetectorName")
        raw = parsed.get("Raw")
        if not isinstance(detector, str) or not isinstance(raw, str) or not raw:
            continue
        status = _classify_trufflehog_status(parsed)
        key = (detector, raw)
        if key in seen:
            continue
        seen.add(key)
        out.append({"raw": raw, "detector": detector, "status": status})
    return out


def placeholder_for_detector(detector: str) -> str:
    """``[REDACTED_<DETECTOR>]`` — matches the style of SECRET_PLACEHOLDER."""
    normalized = re.sub(r"\W+", "_", detector).upper().strip("_")
    return f"[REDACTED_{normalized}]" if normalized else "[REDACTED_TRUFFLEHOG]"


def _iter_session_text_fields(session: dict):
    """Yield ``(text, field, msg_idx, tool_field)`` for every scannable
    string in ``session``. Mirrors ``secrets._iter_text_locations`` but
    returns only what the findings-engine entry point needs."""
    for field_name in ("display_title", "project", "git_branch"):
        val = session.get(field_name)
        if isinstance(val, str) and val:
            yield val, field_name, None, None

    for msg_idx, msg in enumerate(session.get("messages", []) or []):
        if not isinstance(msg, dict):
            continue
        for field_name in ("content", "thinking"):
            val = msg.get(field_name)
            if isinstance(val, str) and val:
                yield val, field_name, msg_idx, None
        for tool_idx, tool_use in enumerate(msg.get("tool_uses", []) or []):
            if not isinstance(tool_use, dict):
                continue
            for branch in ("input", "output"):
                val = tool_use.get(branch)
                if isinstance(val, str) and val:
                    yield val, f"tool_uses[{tool_idx}].{branch}", msg_idx, branch
                elif isinstance(val, dict):
                    for key, nested in val.items():
                        if isinstance(nested, str) and nested:
                            yield (
                                nested,
                                f"tool_uses[{tool_idx}].{branch}.{key}",
                                msg_idx,
                                branch,
                            )


def _serialize_session_for_scan(session: dict) -> str:
    field_payload = "\n\n".join(text for text, *_ in _iter_session_text_fields(session))
    try:
        json_payload = json.dumps(session, default=str, sort_keys=True)
    except (TypeError, ValueError):
        json_payload = ""
    if field_payload and json_payload:
        return f"{field_payload}\n\n{json_payload}"
    return field_payload or json_payload


def scan_session_for_trufflehog_findings(
    session: dict,
    *,
    user_allowlist: list[dict] | None = None,  # noqa: ARG001 — reserved for parity with other engines
) -> list["RawFinding"]:
    """Emit one ``RawFinding`` per occurrence of each TruffleHog match.

    One subprocess call per session: all text fields are concatenated
    into a single payload scanned once. Each raw match is then
    re-located in every text field so the resulting findings have
    field-local offsets (the same shape the share-time apply path
    expects from ``_iter_text_locations``).
    """
    from ..findings import RawFinding  # noqa: PLC0415 — lazy to avoid cycle

    payload = _serialize_session_for_scan(session)
    matches = _scan_text_for_raw_matches(payload)
    if not matches:
        return []

    findings: list[RawFinding] = []
    for match in matches:
        raw = match["raw"]
        if len(raw) < 3:
            continue
        detector = match["detector"]
        confidence = 1.0 if match["status"] == "verified" else 0.9
        for text, field_name, msg_idx, tool_field in _iter_session_text_fields(session):
            start = 0
            while True:
                idx = text.find(raw, start)
                if idx < 0:
                    break
                findings.append(RawFinding(
                    engine=TRUFFLEHOG_ENGINE_ID,
                    rule=detector,
                    entity_type=detector,
                    entity_text=raw,
                    field=field_name,
                    offset=idx,
                    length=len(raw),
                    confidence=confidence,
                    message_index=msg_idx,
                    tool_field=tool_field,
                ))
                start = idx + len(raw)
    return findings


def apply_trufflehog_pass(
    session: dict,
) -> tuple[int, list[dict]]:
    """Legacy-path equivalent of the findings-engine entry: run
    TruffleHog on the session, replace each raw match with
    ``[REDACTED_<DETECTOR>]`` in every field where it occurs, and
    return a redaction log compatible with
    ``apply_share_redactions``' shape (one entry per occurrence
    with ``type=trufflehog_<detector>`` so the UI buckets them
    under secrets).
    """
    payload = _serialize_session_for_scan(session)
    matches = _scan_text_for_raw_matches(payload)
    if not matches:
        return 0, []

    detector_by_raw: dict[str, tuple[str, str]] = {}
    # Sort (detector, raw) so overlapping detectors pick a placeholder
    # deterministically across runs — e.g. ("AWS", raw) wins over
    # ("Generic", raw) rather than whichever arrived first.
    for match in sorted(matches, key=lambda m: (m["detector"], m["raw"])):
        raw = match["raw"]
        if len(raw) < 3:
            continue
        detector_by_raw.setdefault(raw, (match["detector"], match["status"]))

    if not detector_by_raw:
        return 0, []

    # Longest raw first so overlapping patterns replace cleanly.
    ordered = sorted(detector_by_raw.items(), key=lambda kv: -len(kv[0]))

    total = 0
    log: list[dict] = []

    def _replace_in_text(
        text: str,
        *,
        field_name: str,
        message_index: int | None,
    ) -> tuple[str, int]:
        out = text
        local = 0
        for raw, (detector, status) in ordered:
            if raw not in out:
                continue
            placeholder = placeholder_for_detector(detector)
            n = out.count(raw)
            out = out.replace(raw, placeholder)
            local += n
            confidence = 1.0 if status == "verified" else 0.9
            entry: dict = {
                "type": f"trufflehog_{detector.lower()}",
                "confidence": confidence,
                "original_length": len(raw),
                "field": field_name,
            }
            if message_index is not None:
                entry["message_index"] = message_index
            for _ in range(n):
                log.append(dict(entry))
        return out, local

    for field_name in ("display_title", "project", "git_branch"):
        val = session.get(field_name)
        if isinstance(val, str) and val:
            new_val, n = _replace_in_text(val, field_name=field_name, message_index=None)
            if n:
                session[field_name] = new_val
                total += n

    for msg_idx, msg in enumerate(session.get("messages", []) or []):
        if not isinstance(msg, dict):
            continue
        for field_name in ("content", "thinking"):
            val = msg.get(field_name)
            if isinstance(val, str) and val:
                new_val, n = _replace_in_text(val, field_name=field_name, message_index=msg_idx)
                if n:
                    msg[field_name] = new_val
                    total += n
        for tool_idx, tool_use in enumerate(msg.get("tool_uses", []) or []):
            if not isinstance(tool_use, dict):
                continue
            for branch in ("input", "output"):
                val = tool_use.get(branch)
                if isinstance(val, str) and val:
                    new_val, n = _replace_in_text(
                        val,
                        field_name=f"tool_uses[{tool_idx}].{branch}",
                        message_index=msg_idx,
                    )
                    if n:
                        tool_use[branch] = new_val
                        total += n
                elif isinstance(val, dict):
                    for key, nested in list(val.items()):
                        if isinstance(nested, str) and nested:
                            new_val, n = _replace_in_text(
                                nested,
                                field_name=f"tool_uses[{tool_idx}].{branch}.{key}",
                                message_index=msg_idx,
                            )
                            if n:
                                val[key] = new_val
                                total += n
    return total, log


def trufflehog_secret_map_from_blob(
    blob: dict,
    decisions: dict[str, str],
    user_allowlist: list[dict] | None = None,  # noqa: ARG001 — reserved
) -> dict[str, str]:
    """Apply-path contribution: map ``raw → placeholder`` for each
    surviving TruffleHog hit that is not ``ignored``.

    The caller (``apply_findings_to_blob`` in ``secrets.py``) hoists
    this call *outside* its per-pass loop, so this function runs
    exactly once per apply — the raws TruffleHog finds don't change
    after their first replacement, and paying the subprocess cost
    on every pass would be pure waste. When the binary is
    unavailable the engine produces no replacements and the other
    engines still run.

    When two detectors flag the same raw, placeholder selection is
    stabilized by sorting matches ``(detector, raw)`` ascending so
    the tiebreaker is deterministic across runs.
    """
    from ..findings import hash_entity  # noqa: PLC0415 — lazy

    payload = _serialize_session_for_scan(blob)
    matches = _scan_text_for_raw_matches(payload)
    if not matches:
        return {}

    out: dict[str, str] = {}
    for match in sorted(matches, key=lambda m: (m["detector"], m["raw"])):
        raw = match["raw"]
        if len(raw) < 3:
            continue
        if decisions.get(hash_entity(raw)) == "ignored":
            continue
        out.setdefault(raw, placeholder_for_detector(match["detector"]))
    return out


def format_block_message(report: TruffleHogReport) -> str:
    if report.bypassed:
        return "TruffleHog was bypassed via CLAWJOURNAL_SKIP_TRUFFLEHOG."
    if report.binary_missing:
        return INSTALL_HINT
    if report.scan_error:
        return (
            "TruffleHog scan failed before producing a result. "
            f"Share blocked: {report.scan_error}."
        )
    examples = ", ".join(
        f"L{f.line if f.line is not None else '?'} {f.status} {f.detector} {f.masked}"
        for f in report.findings[:5]
    )
    suffix = "" if len(report.findings) <= 5 else f" (+{len(report.findings) - 5} more)"
    return (
        f"TruffleHog blocked the share: {len(report.findings)} finding(s) "
        f"(verified={report.verified}, unverified={report.unverified}, "
        f"unknown={report.unknown}). Examples: {examples}{suffix}"
    )
