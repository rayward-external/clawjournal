"""Betterleaks secrets scanner — the share gate's primary detection layer.

Betterleaks (MIT, by the original Gitleaks author) is invoked as a
subprocess with network validation **always off**: candidate secrets
must never leave the machine from the detection layer. Live-credential
verification is TruffleHog's job (see ``trufflehog.py``), which the
gate runs verified-only as a secondary check.

The binary is resolved managed-install first (``~/.clawjournal/bin/``,
populated by ``clawjournal betterleaks install`` — see
``betterleaks_install.py``), then ``PATH`` (e.g. ``brew install
betterleaks``). The escape hatch ``CLAWJOURNAL_SKIP_BETTERLEAKS=1``
exists for CI / development only and is recorded in scan reports so
downstream reviewers can tell a scanned share from a bypassed one.

Verified against betterleaks 1.6.1:

- ``betterleaks dir <path>`` / ``betterleaks stdin`` write a JSON
  **array** of findings to ``--report-path`` (a bare ``null`` on a
  clean scan); findings carry ``RuleID``, ``StartLine`` (1-based file
  line, which for a one-session-per-line ``sessions.jsonl`` is the
  session index), ``Secret``, ``Match``, ``Entropy``, ``Tags``,
  ``Fingerprint``.
- ``--exit-code 183`` makes the findings exit status match the
  TruffleHog wrapper convention, so ``{0, 183}`` means "scan ran".
- ``--redact`` would scrub ``Secret`` from the report too, so it is
  never passed: the raw is needed transiently to compute the mask and
  the salted hash, exactly like the TruffleHog wrapper parses ``Raw``
  from stdout. Raw values never leave this module via public helpers.
- Secrets are reported in *file-level* form: a private key inside a
  JSONL string arrives with literal ``\\n`` escape sequences, matching
  the bytes on disk rather than the decoded JSON value.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .trufflehog import (
    _iter_session_text_fields,
    _scrubbed_subprocess_env,
    _serialize_session_for_scan,
    _sha256_file,
    mask_secret,
)

if TYPE_CHECKING:
    from ..findings import RawFinding

BETTERLEAKS_ENGINE_ID = "betterleaks"

SKIP_ENV_VAR = "CLAWJOURNAL_SKIP_BETTERLEAKS"

_MAX_SCAN_BYTES = 200 * 1024 * 1024  # 200 MB — cap on engine-path payload

# Findings exit status we ask for via --exit-code so both scanner
# wrappers share the "{0, 183} means the scan ran" contract.
_FINDINGS_EXIT_CODE = 183

INSTALL_HINT = (
    "Betterleaks is required to export shares but was not found.\n"
    "Install a pinned, checksum-verified copy with:\n"
    "  clawjournal betterleaks install\n"
    "Or install it yourself:\n"
    "  macOS:  brew install betterleaks\n"
    "  Linux:  https://github.com/betterleaks/betterleaks#installation\n"
    "Or set CLAWJOURNAL_SKIP_BETTERLEAKS=1 to bypass (unsafe — the share "
    "may leak secrets that survived our redaction layers)."
)


@dataclass
class BetterleaksFinding:
    rule_id: str
    description: str
    line: int | None
    masked: str
    raw_sha256: str | None
    entropy: float | None
    tags: list[str] = field(default_factory=list)


@dataclass
class BetterleaksReport:
    scanned_path: str
    scanned_sha256: str
    findings: list[BetterleaksFinding] = field(default_factory=list)
    top_rules: list[str] = field(default_factory=list)
    bypassed: bool = False
    binary_missing: bool = False
    scan_error: str | None = None
    # Which engine actually scanned (engine_fingerprint() form, e.g.
    # "betterleaks 1.6.1"). Stamped by the export chokepoints before the
    # report is persisted. "" means not stamped (preview scans).
    engine: str = ""

    # Deliberately no ``.blocking`` / ``.block_reason`` here: unlike the
    # legacy TruffleHog gate, whether a Betterleaks finding blocks a
    # share is a per-finding tier decision owned by the policy layer
    # (``scan_policy``), not by the scanner wrapper.

    def summary(self) -> dict:
        """Public summary safe for the share manifest — no raw values."""
        return {
            "findings": len(self.findings),
            "top_rules": list(self.top_rules),
            "bypassed": self.bypassed,
            "binary_missing": self.binary_missing,
            "scan_error": self.scan_error,
            "engine": self.engine,
            "examples": [
                {
                    "rule": f.rule_id,
                    "line": f.line,
                    "masked": f.masked,
                    "entropy": f.entropy,
                }
                for f in self.findings[:5]
            ],
        }


def bundled_config_path() -> Path:
    """The share-gate TOML shipped in the wheel: extends the default
    ruleset and allowlists clawjournal's own ``[REDACTED_*]`` placeholders
    so the gate's redact-and-rescan loop converges instead of re-flagging
    its own output."""
    return Path(__file__).parent / "betterleaks.toml"


def managed_binary_path() -> Path:
    """Path of the managed binary that ``clawjournal betterleaks install``
    maintains. Reads ``config.CONFIG_DIR`` at call time (not import time)
    so tests and future env overrides that monkeypatch the attribute see
    the change.
    """
    from .. import config  # noqa: PLC0415 — call-time so CONFIG_DIR patches apply

    name = "betterleaks.exe" if os.name == "nt" else "betterleaks"
    return config.CONFIG_DIR / "bin" / name


def resolve_binary() -> str | None:
    """Resolve the Betterleaks binary: managed install first, then PATH.

    The managed copy wins because its version is pinned and its archive
    checksum was verified at install time; an unmanaged PATH binary is
    whatever brew/go last put there.
    """
    managed = managed_binary_path()
    try:
        if managed.is_file() and os.access(managed, os.X_OK):
            return str(managed)
    except OSError:
        pass
    return shutil.which("betterleaks")


def is_available() -> bool:
    return resolve_binary() is not None


def managed_off_pin() -> tuple[str, str] | None:
    """``(actual_version, pinned_version)`` when the gate resolves the
    managed copy and that copy is off the source pin; ``None`` otherwise.

    Only the managed copy is judged against the pin — a PATH binary's
    freshness is the user's package manager's business. Warn, never
    block: an off-pin scanner is still a working backstop.
    """
    from .betterleaks_install import PINNED_VERSION  # noqa: PLC0415 — avoid import cycle

    resolved = resolve_binary()
    if resolved is None or resolved != str(managed_binary_path()):
        return None
    match = _BARE_VERSION_RE.search(engine_fingerprint())
    if match is None:
        return None
    actual = match.group(1)
    if actual == PINNED_VERSION:
        return None
    return (actual, PINNED_VERSION)


_version_cache: dict[tuple, str] = {}

# `betterleaks --version` prints "betterleaks version 1.6.1"; the
# `version` subcommand prints a bare "1.6.1". Match either.
_VERSION_RE = re.compile(r"\bbetterleaks\s+version\s+(\d+\.\d+\.\d+)", re.IGNORECASE)
_BARE_VERSION_RE = re.compile(r"(\d+\.\d+\.\d+)")


def _binary_signature() -> tuple:
    """Stable per-binary key for the fingerprint cache.

    Folds the resolved path and its mtime+size so a ``brew upgrade``
    or ``clawjournal betterleaks install`` (new inode, new size, new
    mtime) invalidates cached entries in a long-running daemon without
    requiring a restart. Returns ``("missing",)`` if no binary resolves.
    """
    resolved = resolve_binary()
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
    when the user installs or upgrades Betterleaks. Missing binary
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
    binary = signature[1] if signature[0] == "unknown" else signature[0]
    try:
        result = subprocess.run(
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
            stdin=subprocess.DEVNULL,
            env=_scrubbed_subprocess_env(),
        )
        blob = (result.stdout or "") + "\n" + (result.stderr or "")
        match = _VERSION_RE.search(blob) or _BARE_VERSION_RE.search(blob)
        if match:
            fingerprint = f"betterleaks {match.group(1)}"
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


def _base_args(binary: str, mode_args: list[str]) -> list[str]:
    """Common argv for every scan invocation.

    ``--validation`` must NEVER appear here: it would send candidate
    secrets to provider APIs, and the detection layer is local-only by
    design (PRIVACY.md). Verification is TruffleHog's role.
    """
    args = [binary, *mode_args]
    config_path = bundled_config_path()
    # The bundled config only ADDS an allowlist on top of the default
    # rules, so a missing file (unusual dev tree) degrades toward MORE
    # findings, never fewer — safe to omit rather than fail.
    if config_path.is_file():
        args += ["--config", str(config_path)]
    args += [
        "--report-format", "json",
        "--exit-code", str(_FINDINGS_EXIT_CODE),
        "--no-banner",
        "--no-color",
        "--log-level", "error",
    ]
    return args


def _read_report_payload(report_path: Path) -> list[dict] | None:
    """Parse the JSON report file. Returns the findings list, or None
    when the file is missing/unreadable/malformed (a scan error, since
    exit-status checking already passed). A clean scan writes ``null``,
    which normalizes to ``[]``."""
    try:
        content = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    if not content.strip():
        return None
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if parsed is None:
        return []
    if not isinstance(parsed, list):
        return None
    return [item for item in parsed if isinstance(item, dict)]


def _parse_finding(parsed: dict) -> BetterleaksFinding | None:
    rule_id = parsed.get("RuleID")
    if not isinstance(rule_id, str) or not rule_id:
        return None

    description = parsed.get("Description")
    description = description if isinstance(description, str) else ""

    line_no: int | None = None
    start_line = parsed.get("StartLine")
    if isinstance(start_line, int) and start_line >= 1:
        line_no = start_line

    secret = parsed.get("Secret")
    raw_str = secret if isinstance(secret, str) and secret else None
    raw_sha = (
        f"sha256:{hashlib.sha256(raw_str.encode()).hexdigest()}" if raw_str else None
    )
    masked = mask_secret(raw_str) if raw_str else "[REDACTED]"

    entropy: float | None = None
    raw_entropy = parsed.get("Entropy")
    if isinstance(raw_entropy, (int, float)):
        entropy = float(raw_entropy)

    tags = parsed.get("Tags")
    tags = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []

    return BetterleaksFinding(
        rule_id=rule_id,
        description=description,
        line=line_no,
        masked=masked,
        raw_sha256=raw_sha,
        entropy=entropy,
        tags=tags,
    )


def scan_file(path: Path) -> BetterleaksReport:
    """Scan ``path`` with Betterleaks. Returns a report.

    Never raises on missing-binary, findings, subprocess timeouts,
    spawn failures, unexpected exit statuses, or I/O errors — every
    failure is surfaced on the report (``scan_error`` /
    ``binary_missing``) so the policy layer can fail closed rather
    than the caller handling a raw exception.
    """
    report, _raws = scan_file_with_raws(path)
    return report


def scan_file_with_raws(path: Path) -> tuple[BetterleaksReport, list[dict]]:
    """Gate-internal variant of ``scan_file`` that also returns the raw
    matches from the same single subprocess, so the share gate's
    redact-and-rescan loop can replace the exact on-disk byte
    sequences without a second scan.

    Raw entries are ``[{"raw", "rule_id", "entropy", "line"}]``,
    deduped per ``(rule_id, raw, line)`` — the same value on two lines
    stays two entries because tier decisions are per-session. Raw
    values must never be persisted or returned from public ``scan_*``
    helpers.
    """
    try:
        scanned_sha256 = _sha256_file(path)
    except OSError as exc:
        return BetterleaksReport(
            scanned_path=str(path),
            scanned_sha256="",
            scan_error=f"could not hash scan target: {exc.__class__.__name__}",
        ), []

    if is_bypassed():
        return BetterleaksReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            bypassed=True,
        ), []

    if not is_available():
        return BetterleaksReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            binary_missing=True,
        ), []

    parsed_findings, error = _run_report_scan(
        ["dir", str(path)], input_bytes=None, timeout=120
    )
    if error is not None:
        return BetterleaksReport(
            scanned_path=str(path),
            scanned_sha256=scanned_sha256,
            scan_error=error,
        ), []

    findings: list[BetterleaksFinding] = []
    raw_matches: list[dict] = []
    rule_counts: dict[str, int] = {}
    seen_keys: set[tuple] = set()
    seen_raw_keys: set[tuple] = set()
    for parsed in parsed_findings:
        finding = _parse_finding(parsed)
        if finding is None:
            continue
        key = (finding.rule_id, finding.line, finding.raw_sha256)
        if key not in seen_keys:
            seen_keys.add(key)
            findings.append(finding)
            rule_counts[finding.rule_id] = rule_counts.get(finding.rule_id, 0) + 1
        raw = parsed.get("Secret")
        if isinstance(raw, str) and raw:
            raw_key = (finding.rule_id, raw, finding.line)
            if raw_key not in seen_raw_keys:
                seen_raw_keys.add(raw_key)
                raw_matches.append({
                    "raw": raw,
                    "rule_id": finding.rule_id,
                    "entropy": finding.entropy,
                    "line": finding.line,
                })

    findings.sort(key=lambda f: (f.line if f.line is not None else 10**9, f.rule_id))
    top = sorted(rule_counts.items(), key=lambda item: (-item[1], item[0]))[:8]

    return BetterleaksReport(
        scanned_path=str(path),
        scanned_sha256=scanned_sha256,
        findings=findings,
        top_rules=[rule for rule, _ in top],
    ), raw_matches


def _run_report_scan(
    mode_args: list[str],
    *,
    input_bytes: bytes | None,
    timeout: int,
) -> tuple[list[dict], str | None]:
    """Run one scan subprocess and return ``(parsed_findings, error)``.

    The report goes through a 0600 temp file (mkstemp) because
    Betterleaks cannot stream JSON reports to stdout; it is read and
    unlinked in ``finally`` so raw secrets never persist past the call.
    Callers have already handled bypass/missing-binary.
    """
    fd, report_name = tempfile.mkstemp(prefix=".betterleaks-report.", suffix=".json")
    os.close(fd)
    report_path = Path(report_name)

    # `or "betterleaks"` covers callers that stub is_available() without
    # providing a real binary (tests); production always resolves a path.
    args = _base_args(resolve_binary() or "betterleaks", mode_args)
    args += ["--report-path", str(report_path)]

    # DEVNULL on stdin (when not streaming a payload) prevents any
    # interactive prompt from deadlocking a non-interactive runner.
    # Scrubbed env keeps the parent process's API keys out of the child.
    io_kwargs: dict = {"env": _scrubbed_subprocess_env()}
    if input_bytes is not None:
        io_kwargs["input"] = input_bytes
    else:
        io_kwargs["stdin"] = subprocess.DEVNULL

    try:
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                check=False,
                timeout=timeout,
                **io_kwargs,
            )
        except subprocess.TimeoutExpired:
            return [], f"timed out after {timeout} seconds"
        except OSError:
            return [], "could not execute the betterleaks binary"
        if result.returncode not in (0, _FINDINGS_EXIT_CODE):
            return [], f"unexpected exit status {result.returncode}"

        payload = _read_report_payload(report_path)
        if payload is None:
            return [], "scan produced no readable JSON report"
        return payload, None
    finally:
        try:
            report_path.unlink()
        except OSError:
            pass


def scan_text(text: str) -> BetterleaksReport:
    """Scan an in-memory string by dropping it to a temp file.

    Used by preview scans that want the full report shape without
    managing their own temp dir. The authoritative gate still runs on
    the merged sessions.jsonl at Package time.
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
    ) as tf:
        tf.write(text)
        tmp_path = Path(tf.name)
    try:
        return scan_file(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def write_report(path: Path, report: BetterleaksReport) -> None:
    payload = {
        # Record only the basename so tempfile paths (e.g., /var/folders/...
        # on macOS) don't leak the user's tmpdir structure into the
        # manifest bundle.
        "scanned_path": Path(report.scanned_path).name,
        "scanned_sha256": report.scanned_sha256,
        "bypassed": report.bypassed,
        "binary_missing": report.binary_missing,
        "scan_error": report.scan_error,
        "engine": report.engine,
        "findings": [
            {
                "rule": f.rule_id,
                "description": f.description,
                "line": f.line,
                "masked": f.masked,
                "raw_sha256": f.raw_sha256,
                "entropy": f.entropy,
            }
            for f in report.findings
        ],
        "summary": report.summary(),
    }
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _raw_matches_from_payload(parsed_findings: list[dict]) -> list[dict]:
    """Normalize report entries to raw-bearing dicts for internal use.

    Returns ``[{"raw": str, "rule_id": str, "entropy": float | None,
    "line": int | None}]``, deduped on ``(rule_id, raw)`` keeping the
    first line each pair was seen on.
    """
    out: list[dict] = []
    seen: set[tuple] = set()
    for parsed in parsed_findings:
        rule_id = parsed.get("RuleID")
        raw = parsed.get("Secret")
        if not isinstance(rule_id, str) or not rule_id:
            continue
        if not isinstance(raw, str) or not raw:
            continue
        key = (rule_id, raw)
        if key in seen:
            continue
        seen.add(key)
        entropy = parsed.get("Entropy")
        start_line = parsed.get("StartLine")
        out.append({
            "raw": raw,
            "rule_id": rule_id,
            "entropy": float(entropy) if isinstance(entropy, (int, float)) else None,
            "line": start_line if isinstance(start_line, int) and start_line >= 1 else None,
        })
    return out


def _scan_file_for_raw_matches(path: Path) -> list[dict]:
    """Internal: dir-mode scan of ``path`` returning raw-bearing dicts.

    Used by the share gate's redact-and-rescan loop, which must know
    the exact on-disk byte sequence to replace (``Secret`` arrives in
    file-level escaped form) and the 1-based line for session mapping.
    Raw values must never be persisted or returned from public
    ``scan_*`` helpers.

    Fails soft to ``[]`` when bypassed or the binary is missing; the
    caller distinguishes "no findings" from "cannot scan" by running
    ``scan_file`` alongside (its report carries ``binary_missing`` /
    ``scan_error``).
    """
    if is_bypassed() or not is_available():
        return []
    parsed_findings, error = _run_report_scan(
        ["dir", str(path)], input_bytes=None, timeout=120
    )
    if error is not None:
        return []
    return _raw_matches_from_payload(parsed_findings)


def _scan_text_for_raw_matches(text: str) -> list[dict]:
    """Internal: stdin-mode scan of ``text`` returning raw-bearing dicts.

    Used only by the findings-engine entry points — the apply path
    needs ``raw`` to build the replace map and to compute salted
    hashes. Streaming via stdin means no plaintext tempfile of the
    *session* is ever written. The JSON *report* (which carries the
    matched ``Secret`` values) still goes through a 0600 mkstemp file
    that is unlinked in ``finally`` — a SIGKILL in that window can
    orphan it; betterleaks cannot stream reports to stdout.

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

    parsed_findings, error = _run_report_scan(
        ["stdin"], input_bytes=encoded, timeout=30
    )
    if error is not None:
        # Engine path intentionally fails soft — a broken scan
        # shouldn't block the whole findings rebuild.
        return []
    return _raw_matches_from_payload(parsed_findings)


def placeholder_for_rule(rule_id: str) -> str:
    """``[REDACTED_<RULE_ID>]`` — matches the style of SECRET_PLACEHOLDER."""
    normalized = re.sub(r"\W+", "_", rule_id).upper().strip("_")
    return f"[REDACTED_{normalized}]" if normalized else "[REDACTED_BETTERLEAKS]"


def scan_session_for_betterleaks_findings(
    session: dict,
    *,
    user_allowlist: list[dict] | None = None,  # noqa: ARG001 — reserved for parity with other engines
) -> list["RawFinding"]:
    """Emit one ``RawFinding`` per occurrence of each Betterleaks match.

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
        rule_id = match["rule_id"]
        for text, field_name, msg_idx, tool_field in _iter_session_text_fields(
            session, include_widened=True
        ):
            start = 0
            while True:
                idx = text.find(raw, start)
                if idx < 0:
                    break
                findings.append(RawFinding(
                    engine=BETTERLEAKS_ENGINE_ID,
                    rule=rule_id,
                    entity_type=rule_id,
                    entity_text=raw,
                    field=field_name,
                    offset=idx,
                    length=len(raw),
                    confidence=0.9,
                    message_index=msg_idx,
                    tool_field=tool_field,
                ))
                start = idx + len(raw)
    return findings


def betterleaks_secret_map_from_blob(
    blob: dict,
    decisions: dict[str, str],
    user_allowlist: list[dict] | None = None,  # noqa: ARG001 — reserved
) -> dict[str, str]:
    """Apply-path contribution: map ``raw → placeholder`` for each
    surviving Betterleaks hit that is not ``ignored``.

    The caller (``apply_findings_to_blob`` in ``secrets.py``) hoists
    this call *outside* its per-pass loop, so this function runs
    exactly once per apply — the raws Betterleaks finds don't change
    after their first replacement, and paying the subprocess cost
    on every pass would be pure waste. When the binary is
    unavailable the engine produces no replacements and the other
    engines still run.

    When two rules flag the same raw, placeholder selection is
    stabilized by sorting matches ``(rule_id, raw)`` ascending so
    the tiebreaker is deterministic across runs.
    """
    from ..findings import hash_entity  # noqa: PLC0415 — lazy

    payload = _serialize_session_for_scan(blob)
    matches = _scan_text_for_raw_matches(payload)
    if not matches:
        return {}

    out: dict[str, str] = {}
    for match in sorted(matches, key=lambda m: (m["rule_id"], m["raw"])):
        raw = match["raw"]
        if len(raw) < 3:
            continue
        if decisions.get(hash_entity(raw)) == "ignored":
            continue
        out.setdefault(raw, placeholder_for_rule(match["rule_id"]))
    return out
