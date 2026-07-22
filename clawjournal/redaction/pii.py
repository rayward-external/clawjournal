"""LLM-assisted and rule-based PII review for exported ClawJournal bundles.

The file-based `PIIFinding` substrate (type, placeholders, load/write
helpers, apply functions) lives in `clawjournal.findings` and is
re-exported below for back-compat. Only the LLM-review + rule-scan
machinery still lives in this module — it predates the DB-backed
findings flow and remains the path for `pii-review` / `pii-apply`
until LLM-PII gets a no-plaintext deterministic apply design.
"""

from __future__ import annotations

import json
import math
import re
import tempfile
from collections.abc import Iterable
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path
from time import monotonic
from typing import Any, Callable

from ..findings import (
    ALLOWED_ENTITY_TYPES,
    PIIFinding,
    PLACEHOLDER_BY_TYPE,
    RawFinding,
    apply_findings_to_session,
    apply_findings_to_text,
    hash_entity,
    load_findings,
    merge_findings,
    normalize_finding,
    replacement_for_type,
    write_findings,
)
from ..scoring.backends import (
    BACKEND_CHOICES,
    PROMPTS_DIR,
    resolve_backend,
    run_default_agent_task,
)

__all__ = [
    "ALLOWED_ENTITY_TYPES",
    "PII_ENGINE_ID",
    "PIIFinding",
    "PLACEHOLDER_BY_TYPE",
    "apply_findings_to_session",
    "apply_findings_to_text",
    "load_findings",
    "load_jsonl_sessions",
    "merge_findings",
    "normalize_finding",
    "replacement_for_type",
    "review_session_pii",
    "review_session_pii_hybrid",
    "review_session_pii_with_agent",
    "scan_session_for_pii_findings",
    "scan_text_for_pii",
    "write_findings",
    "write_jsonl_sessions",
]

MAX_LLM_TEXT_CHARS = 12000


def load_jsonl_sessions(path: Path) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            sessions.append(json.loads(line))
    return sessions


def write_jsonl_sessions(path: Path, sessions: Iterable[dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for session in sessions:
            f.write(json.dumps(session, ensure_ascii=False) + "\n")


def _truncate_for_llm(text: str, max_chars: int = MAX_LLM_TEXT_CHARS) -> str:
    if len(text) <= max_chars:
        return text
    head = text[: max_chars // 2]
    tail = text[-(max_chars // 2):]
    return head + "\n\n[...TRUNCATED FOR PII REVIEW...]\n\n" + tail


_PII_RUBRIC_FILE = PROMPTS_DIR / "pii_review" / "rubric.md"

# Inline fallback — used only if the rubric file is missing.
_FALLBACK_PII_RUBRIC = """\
You are a PII reviewer for coding-agent conversation traces that will be published as open datasets.
Your job: find text that could identify a real person, organization, or private system.

Return ONLY valid JSON: an array of finding objects. No prose, no markdown fences.

## What to flag (MUST flag if present)

### High confidence (0.85–1.0)
- **person_name**: Real human names. First+last, or distinctive first names in context (e.g., "Kai said", "from Alice"). Not generic words that happen to be names.
- **email**: Full email addresses (user@domain.tld). Not noreply@ or generic service addresses.
- **phone**: Phone numbers in any format (+1-555-123-4567, (555) 123 4567, etc.).
- **username**: GitHub handles, Telegram usernames, SSH user names, bot names — anywhere a handle identifies a person. Includes handles in URLs (github.com/handle), CLI commands (gh repo view handle/repo), git configs, commit metadata.
- **user_id**: Numeric user/chat/account IDs. Telegram chat IDs, Slack user IDs, etc. Not UUIDs, session IDs, or commit SHAs.
- **custom_sensitive**: API tokens, bot tokens (especially Telegram format: digits:alphanumeric), service credentials that survived earlier redaction.

### Medium confidence (0.60–0.84)
- **org_name**: Company, client, or internal organization names when they appear identifying. "Acme Corp", "Initech", client project codenames. Not public products (GitHub, OpenAI, AWS).
- **project_name**: Internal/private project codenames, private repo names, internal tool names. Not public open-source projects.
- **private_url**: URLs pointing to internal systems, private repos, intranet sites, or containing usernames/org names. Not public docs, npm, PyPI, Stack Overflow.
- **domain**: Private or corporate domains (acme-internal.com, dev.mycompany.io). Not public domains (github.com, google.com).
- **device_id**: Device names (kais-macbook-pro, my-workstation-01), hostnames with personal identifiers, hardware serial numbers.

### Lower confidence (0.40–0.59)
- **address**: Physical addresses, office locations ("123 Main St", "Building 4, Floor 2").
- **location**: City + context that narrows to a person ("our SF office", "the Tokyo team"). Not just generic city mentions.
- **bot_name**: Bot/service account names that could trace back to a person or team.

## What NOT to flag (skip these)
- Already-redacted placeholders: [REDACTED_*], [REDACTED], ***
- Public product/service names: GitHub, OpenAI, Anthropic, Telegram, Docker, AWS, GCP, Hugging Face, npm, PyPI
- Localhost, 127.0.0.1, 0.0.0.0, example.com, test.com
- Generic technical terms, function/class/variable names
- Open-source project names (tensorflow, pytorch, react, clawjournal)
- Public documentation URLs
- Version numbers, build IDs, commit SHAs, UUIDs
- Standard paths (/usr/bin, /tmp, /etc)

## Confidence calibration
- 0.95+: Unambiguous PII (full name + context, email, phone, explicit username)
- 0.85–0.94: Very likely PII (handle in URL, numeric user ID in metadata)
- 0.70–0.84: Likely PII but could be a project/product name
- 0.50–0.69: Possible PII, needs human review
- Below 0.50: Don't flag — too speculative

## Output schema
Each finding must be:
{
  "entity_text": "exact text to redact",
  "entity_type": "person_name"|"email"|"phone"|"username"|"user_id"|"org_name"|"project_name"|"private_url"|"domain"|"address"|"location"|"bot_name"|"device_id"|"custom_sensitive",
  "confidence": <number 0.0–1.0>,
  "reason": "brief explanation"
}
"""


def _load_pii_rubric() -> str:
    """Load the PII review rubric from file, with inline fallback."""
    if _PII_RUBRIC_FILE.exists():
        return _PII_RUBRIC_FILE.read_text(encoding="utf-8")
    return _FALLBACK_PII_RUBRIC


# Keep module-level constant for backward compat (tests, cli imports).
PII_REVIEW_RUBRIC = _load_pii_rubric()


def _build_pii_review_prompt(payload: dict[str, Any], rubric: str | None = None) -> str:
    return (
        (rubric or PII_REVIEW_RUBRIC)
        + "\n## Text to review\n"
        + json.dumps(payload, ensure_ascii=False)
        + "\n"
    )


def _extract_json_array(text: str) -> list[dict[str, Any]]:
    text = text.strip()
    try:
        parsed = json.loads(text)
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        pass
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        return []
    try:
        parsed = json.loads(match.group(0))
        return parsed if isinstance(parsed, list) else []
    except json.JSONDecodeError:
        return []


def _normalize_llm_findings(session_id: str, message_index: int, field: str, findings: list[dict[str, Any]], source: str) -> list[PIIFinding]:
    out: list[PIIFinding] = []
    for finding in findings:
        entity_type = str(finding.get("entity_type") or "custom_sensitive")
        if entity_type not in ALLOWED_ENTITY_TYPES:
            entity_type = "custom_sensitive"
        normalized = normalize_finding({
            "session_id": session_id,
            "message_index": message_index,
            "field": field,
            "entity_text": finding.get("entity_text") or "",
            "entity_type": entity_type,
            "confidence": finding.get("confidence", 0.0),
            "reason": finding.get("reason") or "",
            "replacement": replacement_for_type(entity_type),
            "source": source,
        })
        if normalized.get("entity_text"):
            out.append(normalized)
    return out


_PII_PROMPT_FILE = PROMPTS_DIR / "pii_review" / "system.md"

# Safety valve for session-level batching.  Modern agent CLIs have large
# context windows (Claude Opus: 1M tokens, Codex/OpenClaw: 200K+).  At ~4
# chars/token, 2M chars ≈ 500K tokens — half the largest context window,
# leaving ample room for rubric + reasoning.  In practice this never splits;
# the largest sessions we've seen are ~120K chars (~30K tokens).
_BATCH_CHAR_LIMIT = 2_000_000


def _write_batch_inputs(tmp_path: Path, session_id: str, work_items: list[tuple[str, int, str, str]], rubric: str | None) -> None:
    """Write batched PII review inputs: one JSONL file with all text chunks."""
    lines: list[str] = []
    for _, message_index, field, text in work_items:
        lines.append(json.dumps({
            "message_index": message_index,
            "field": field,
            "text": _truncate_for_llm(text),
        }, ensure_ascii=False))
    (tmp_path / "texts_to_review.jsonl").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "context.json").write_text(json.dumps({"session_id": session_id}), encoding="utf-8")
    (tmp_path / "PII_RUBRIC.md").write_text(rubric or PII_REVIEW_RUBRIC, encoding="utf-8")


def _read_batch_findings(tmp_path: Path, session_id: str, source: str, stdout: str = "") -> list[PIIFinding]:
    """Read findings.json containing batched findings with message_index and field per entry."""
    raw: list[dict] = []
    findings_path = tmp_path / "findings.json"
    if findings_path.exists():
        try:
            parsed = json.loads(findings_path.read_text(encoding="utf-8"))
            if isinstance(parsed, list):
                raw = parsed
        except json.JSONDecodeError:
            pass
    if not raw:
        raw = _extract_json_array(stdout)

    out: list[PIIFinding] = []
    for finding in raw:
        if not isinstance(finding, dict):
            continue
        msg_idx = finding.get("message_index", 0)
        field = finding.get("field", "content")
        entity_type = finding.get("entity_type") or "custom_sensitive"
        if entity_type not in ALLOWED_ENTITY_TYPES:
            entity_type = "custom_sensitive"
        normalized = normalize_finding({
            "session_id": session_id,
            "message_index": msg_idx,
            "field": field,
            "entity_text": finding.get("entity_text") or "",
            "entity_type": entity_type,
            "confidence": finding.get("confidence", 0.0),
            "reason": finding.get("reason") or "",
            "replacement": replacement_for_type(entity_type),
            "source": source,
        })
        if normalized.get("entity_text"):
            out.append(normalized)
    return out


_BATCH_TASK_PROMPT = (
    "Review texts_to_review.jsonl for PII. Each line is a JSON object with "
    "message_index, field, and text. Read PII_RUBRIC.md and context.json. "
    "Write findings.json with a JSON array. Each finding must include: "
    "message_index, field, entity_text, entity_type, confidence, reason. "
    "Write [] if no PII found."
)


def _split_into_batches(work_items: list[tuple[str, int, str, str]], char_limit: int = _BATCH_CHAR_LIMIT) -> list[list[tuple[str, int, str, str]]]:
    """Split work items into batches that fit within char_limit."""
    batches: list[list[tuple[str, int, str, str]]] = []
    current: list[tuple[str, int, str, str]] = []
    current_chars = 0
    for item in work_items:
        item_chars = min(len(item[3]), MAX_LLM_TEXT_CHARS)
        if current and current_chars + item_chars > char_limit:
            batches.append(current)
            current = []
            current_chars = 0
        current.append(item)
        current_chars += item_chars
    if current:
        batches.append(current)
    return batches


class _AgentCallGateError(Exception):
    """Preserve a caller's control-gate failure across hybrid fallback logic."""

    def __init__(self, cause: Exception):
        super().__init__(str(cause))
        self.cause = cause


def _review_batch(
    session_id: str,
    work_items: list[tuple[str, int, str, str]],
    *,
    rubric: str | None,
    backend: str = "auto",
    timeout_seconds: int = 180,
    before_agent_call: Callable[[], None] | None = None,
) -> list[PIIFinding]:
    """Review a batch of text chunks via the shared agent runner."""
    resolved = resolve_backend(backend)
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        _write_batch_inputs(tmp_path, session_id, work_items, rubric)

        # Build OpenClaw-specific message with absolute paths
        openclaw_msg = None
        if resolved == "openclaw":
            openclaw_msg = (
                "Review texts_to_review.jsonl for PII. Each line has message_index, field, text.\n"
                f"Read: {tmp_path / 'PII_RUBRIC.md'} and {tmp_path / 'context.json'}.\n"
                "Return a JSON array of findings, each with: message_index, field, entity_text, "
                "entity_type, confidence, reason. Return [] if no PII. No markdown fences."
            )

        try:
            # This is the external-egress boundary.  Automatic sharing uses
            # it to recheck generation, hold, revision, scope, and profile for
            # every provider call, including each batch of a long session.
            if before_agent_call is not None:
                try:
                    before_agent_call()
                except Exception as exc:
                    raise _AgentCallGateError(exc) from exc
            result = run_default_agent_task(
                backend=resolved,
                cwd=tmp_path,
                system_prompt_file=_PII_PROMPT_FILE,
                task_prompt=_BATCH_TASK_PROMPT,
                timeout_seconds=timeout_seconds,
                codex_sandbox="read-only",
                codex_output_file="findings.json",
                openclaw_message=openclaw_msg,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"PII review failed for session {session_id}: {exc}") from exc

        return _read_batch_findings(tmp_path, session_id, resolved, result.stdout)


def _review_text_with_agent(session_id: str, message_index: int, field: str, text: str, *, rubric: str | None = None, backend: str = "auto") -> list[PIIFinding]:
    """Review a single text chunk — used by backward-compat wrappers."""
    if not text.strip():
        return []
    items = [(session_id, message_index, field, text)]
    return _review_batch(session_id, items, rubric=rubric, backend=backend)


def _collect_text_work_items(session: dict[str, Any]) -> list[tuple[str, int, str, str]]:
    """Extract all (session_id, message_index, field, text) tuples from a session."""
    from ..parsing.widened import iter_widened_text_locations

    session_id = str(session.get("session_id") or "")
    messages = session.get("messages", [])
    work_items: list[tuple[str, int, str, str]] = []
    if isinstance(messages, list):
        for i, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            for field in ("content", "thinking"):
                value = msg.get(field)
                if isinstance(value, str) and value.strip():
                    work_items.append((session_id, i, field, value))
            for tool_index, tool_use in enumerate(msg.get("tool_uses", [])):
                if not isinstance(tool_use, dict):
                    continue
                for branch in ("input", "output"):
                    value = tool_use.get(branch)
                    if isinstance(value, dict):
                        for key, nested in value.items():
                            if isinstance(nested, str) and nested.strip():
                                work_items.append((session_id, i, f"tool_uses[{tool_index}].{branch}.{key}", nested))
                    elif isinstance(value, str) and value.strip():
                        work_items.append((session_id, i, f"tool_uses[{tool_index}].{branch}", value))
            # Widened message model (phase-2 C1): same redaction passes
            # apply to invocations / snippets / extra / author.
            for text, field_label in iter_widened_text_locations(msg):
                if text.strip():
                    work_items.append((session_id, i, field_label, text))
    for field, text in _iter_session_level_text_locations(session):
        if text.strip():
            work_items.append((session_id, -1, field, text))
    return work_items


def _iter_session_level_text_locations(session: dict[str, Any]) -> Iterable[tuple[str, str]]:
    """Yield metadata and judge-generated text that can be exported."""
    for field in (
        "project",
        "git_branch",
        "display_title",
        "ai_learning_summary",
        "ai_score_reason",
        "ai_display_title",
        "ai_summary",
    ):
        value = session.get(field)
        if isinstance(value, str) and value:
            yield field, value

    raw = session.get("ai_scoring_detail")
    if not isinstance(raw, str) or not raw:
        return
    try:
        detail = json.loads(raw)
    except json.JSONDecodeError:
        yield "ai_scoring_detail", raw
        return
    yield from _iter_json_text_locations("ai_scoring_detail", detail)


def _iter_json_text_locations(prefix: str, value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, str):
        yield prefix, value
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            yield from _iter_json_text_locations(f"{prefix}[{idx}]", item)
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _iter_json_text_locations(f"{prefix}.{key}", item)


def review_session_pii_with_agent(
    session: dict[str, Any],
    *,
    backend: str = "auto",
    ignore_errors: bool = False,
    rubric: str | None = None,
    max_workers: int = 4,
    timeout_seconds: int = 180,
    before_agent_call: Callable[[], None] | None = None,
) -> list[PIIFinding]:
    """Review a session for PII using session-level batching (one agent call per batch)."""
    work_items = _collect_text_work_items(session)
    if not work_items:
        return []

    session_id = str(session.get("session_id") or "")
    batches = _split_into_batches(work_items)

    findings: list[PIIFinding] = []
    errors: list[RuntimeError] = []

    # A mutable control gate must be observed in a strict sequence.  Starting
    # all batches concurrently would let later provider calls cross egress
    # before a pause/hold applied during the first call can be seen.  An
    # ungated max_workers<=1 call stays on the pooled path below: one worker
    # is already strictly sequential and must share the one request deadline.
    if len(batches) == 1 or before_agent_call is not None:
        for batch in batches:
            try:
                gate_kwargs = (
                    {"before_agent_call": before_agent_call}
                    if before_agent_call is not None
                    else {}
                )
                findings.extend(
                    _review_batch(
                        session_id,
                        batch,
                        rubric=rubric,
                        backend=backend,
                        timeout_seconds=timeout_seconds,
                        **gate_kwargs,
                    )
                )
            except RuntimeError:
                if not ignore_errors:
                    raise
    else:
        # Multiple batches share ONE request budget. Submitting every batch up
        # front made timeout_seconds apply once per worker wave, so a large trace
        # could keep the HTTP handler and agent CLIs alive long after the browser
        # deadline. Refill only while time remains and pass each new worker the
        # remaining budget. Backend wrappers retain their existing 10-second
        # process-cleanup grace, so total wall time is bounded by budget + grace.
        deadline = monotonic() + max(0.001, float(timeout_seconds))
        worker_count = min(max(1, max_workers), len(batches))
        pool = ThreadPoolExecutor(max_workers=worker_count)
        futures: dict[Any, list[tuple[str, int, str, str]]] = {}
        next_batch = 0
        timed_out = False

        def fill_workers() -> None:
            nonlocal next_batch
            while len(futures) < worker_count and next_batch < len(batches):
                remaining = deadline - monotonic()
                if remaining <= 0:
                    return
                batch = batches[next_batch]
                next_batch += 1
                future = pool.submit(
                    _review_batch,
                    session_id,
                    batch,
                    rubric=rubric,
                    backend=backend,
                    timeout_seconds=max(1, math.ceil(remaining)),
                )
                futures[future] = batch

        try:
            fill_workers()
            while futures:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                # OpenClaw/Hermes wrappers allow ten seconds after their own
                # deadline to terminate and collect output.
                done, _ = wait(
                    futures,
                    timeout=remaining + 10,
                    return_when=FIRST_COMPLETED,
                )
                if not done:
                    timed_out = True
                    break
                for future in done:
                    futures.pop(future, None)
                    try:
                        findings.extend(future.result())
                    except RuntimeError as exc:
                        if not ignore_errors:
                            errors.append(exc)
                if errors:
                    break
                if next_batch < len(batches) and monotonic() >= deadline:
                    timed_out = True
                    break
                fill_workers()
            if next_batch < len(batches):
                timed_out = True
        finally:
            for future in futures:
                future.cancel()
            pool.shutdown(wait=True, cancel_futures=True)

        if timed_out and not ignore_errors:
            errors.append(
                RuntimeError(
                    f"PII review timed out after {timeout_seconds}s for session {session_id}"
                )
            )
        if errors:
            raise errors[0]

    return merge_findings(findings)


def review_session_pii_hybrid(
    session: dict[str, Any],
    *,
    ignore_llm_errors: bool = True,
    rubric: str | None = None,
    backend: str = "auto",
    return_coverage: bool = False,
    timeout_seconds: int = 180,
    before_agent_call: Callable[[], None] | None = None,
) -> list[PIIFinding] | tuple[list[PIIFinding], str]:
    """Run hybrid PII detection (rule-based + AI agent).

    When *return_coverage* is True, returns a tuple of (findings, coverage)
    where coverage is ``"full"`` if AI detection succeeded or ``"rules_only"``
    if the AI backend was unavailable or errored.
    """
    rule_findings = review_session_pii(session)
    coverage = "full"
    try:
        agent_findings = review_session_pii_with_agent(
            session,
            backend=backend,
            ignore_errors=False,
            rubric=rubric,
            timeout_seconds=timeout_seconds,
            before_agent_call=before_agent_call,
        )
    except _AgentCallGateError as exc:
        raise exc.cause from exc
    except Exception:
        if not ignore_llm_errors:
            raise
        agent_findings = []
        coverage = "rules_only"
    merged = merge_findings(rule_findings + agent_findings)
    if return_coverage:
        return merged, coverage
    return merged


_GITHUB_URL_PUBLIC_ORGS = frozenset({
    "anthropics", "anthropic", "openai", "google", "microsoft", "meta",
    "facebook", "aws", "hashicorp", "vercel", "supabase", "huggingface",
    "pytorch", "tensorflow", "golang", "rust-lang", "python", "nodejs",
    "actions", "github", "cli", "docker", "kubernetes", "helm", "npm",
    "homebrew", "apache", "mozilla", "jetbrains", "gradle", "maven",
    "rayward-external",
})


def _content_findings_for_text(session_id: str, message_index: int, field: str, text: str) -> list[PIIFinding]:
    """Scan free-form text for PII patterns beyond JSON metadata."""
    findings: list[PIIFinding] = []
    patterns: list[tuple[str, str, str, float, int]] = [
        # GitHub user/org in URLs — group 1 is the username/org
        (r"github\.com/([A-Za-z0-9_.-]{2,})", "username", "GitHub username/org in URL", 0.85, 1),
        (r"raw\.githubusercontent\.com/([A-Za-z0-9_.-]{2,})", "username", "GitHub username/org in raw URL", 0.85, 1),
        # Email-like identifiers (require user@domain.tld format)
        (r"([A-Za-z0-9_.+-]{3,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", "email", "Email address", 0.90, 1),
        # Partial email / identifier with @ (e.g., "jane.doe@" in tabular output)
        (r"([A-Za-z0-9_.+-]{3,})@(?=\s|$)", "email", "Email-like identifier (truncated)", 0.75, 1),
        # Telegram bot tokens: numeric_id:alphanumeric_token
        (r"(\d{8,}:[A-Za-z0-9_-]{30,})", "custom_sensitive", "Likely Telegram bot token", 0.95, 1),
        # Hostnames with personal identifiers (e.g., kais-macbook-pro, alice-desktop)
        (r"\b([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)\b", "device_id", "Likely personal hostname", 0.80, 1),
        # Absolute home-directory paths (leaks username and directory structure)
        (r"(/(?:Users|home)/[A-Za-z0-9._-]{2,}/[^\s\"'`,;)}\]]{3,})", "path", "Home-directory file path", 0.85, 1),
        # Private/internal IP addresses (not localhost)
        (r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b", "custom_sensitive", "Private IP address (10.x)", 0.70, 1),
        (r"\b(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b", "custom_sensitive", "Private IP address (172.16-31.x)", 0.70, 1),
        (r"\b(192\.168\.\d{1,3}\.\d{1,3})\b", "custom_sensitive", "Private IP address (192.168.x)", 0.70, 1),
    ]
    for pattern, entity_type, reason, confidence, group in patterns:
        for match in re.finditer(pattern, text):
            entity_text = match.group(group).strip()
            if not entity_text or len(entity_text) < 3:
                continue
            # skip already-redacted placeholders
            if entity_text.startswith("[") and entity_text.endswith("]"):
                continue
            # skip well-known public GitHub orgs
            if "GitHub" in reason and entity_text.lower() in _GITHUB_URL_PUBLIC_ORGS:
                continue
            # skip noreply / no-reply email addresses
            if entity_type == "email" and entity_text.lower().startswith(("noreply@", "no-reply@")):
                continue
            findings.append(normalize_finding({
                "session_id": session_id,
                "message_index": message_index,
                "field": field,
                "entity_text": entity_text,
                "entity_type": entity_type,
                "confidence": confidence,
                "reason": reason,
                "replacement": replacement_for_type(entity_type),
                "source": "rule",
            }))
    return findings


def _metadata_findings_for_text(session_id: str, message_index: int, field: str, text: str) -> list[PIIFinding]:
    findings: list[PIIFinding] = []
    _Q = r'\\?"'  # match both `"` and `\"`
    patterns: list[tuple[str, str, str, str, float]] = [
        (rf'{_Q}username{_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "username", "Likely username in metadata block", "rule", 0.98),
        (rf'{_Q}sender_id{_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "user_id", "Likely sender/user ID in metadata block", "rule", 0.98),
        (rf'{_Q}(?:user_id|chat_id|account_id|sender_id|from_id){_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "user_id", "Likely user/chat/account ID in metadata block", "rule", 0.95),
        (rf'{_Q}id{_Q}\s*:\s*{_Q}(\d{{5,}}){_Q}', "user_id", "Likely numeric user ID in metadata block", "rule", 0.75),
        (rf'{_Q}name{_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "person_name", "Likely person name in metadata block", "rule", 0.82),
        (rf'{_Q}sender{_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "person_name", "Likely sender name in metadata block", "rule", 0.82),
        (rf'{_Q}label{_Q}\s*:\s*{_Q}([^"\\]{{3,}}){_Q}', "person_name", "Likely identifying label in metadata block", "rule", 0.75),
    ]
    for pattern, entity_type, reason, source, confidence in patterns:
        for match in re.finditer(pattern, text):
            entity_text = match.group(1).strip()
            if entity_text.startswith("[") and entity_text.endswith("]"):
                continue
            findings.append(normalize_finding({
                "session_id": session_id,
                "message_index": message_index,
                "field": field,
                "entity_text": entity_text,
                "entity_type": entity_type,
                "confidence": confidence,
                "reason": reason,
                "replacement": replacement_for_type(entity_type),
                "source": source,
            }))
    return findings


def _scan_text_for_pii(session_id: str, message_index: int, field: str, text: str) -> list[PIIFinding]:
    """Run both metadata and content PII scans on a text value."""
    findings = _metadata_findings_for_text(session_id, message_index, field, text)
    findings.extend(_content_findings_for_text(session_id, message_index, field, text))
    return findings


def review_session_pii(session: dict[str, Any]) -> list[PIIFinding]:
    from ..parsing.widened import iter_widened_text_locations

    findings: list[PIIFinding] = []
    session_id = str(session.get("session_id") or "")

    # Scan top-level metadata and judge-generated export fields for PII.
    for field, value in _iter_session_level_text_locations(session):
        if value.strip():
            findings.extend(_scan_text_for_pii(session_id, -1, field, value))

    messages = session.get("messages", [])
    if not isinstance(messages, list):
        return findings
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            continue
        for field in ("content", "thinking"):
            value = msg.get(field)
            if isinstance(value, str):
                findings.extend(_scan_text_for_pii(session_id, i, field, value))
        for tool_index, tool_use in enumerate(msg.get("tool_uses", [])):
            if not isinstance(tool_use, dict):
                continue
            for branch in ("input", "output"):
                value = tool_use.get(branch)
                if isinstance(value, dict):
                    for key, nested in value.items():
                        if isinstance(nested, str):
                            field = f"tool_uses[{tool_index}].{branch}.{key}"
                            findings.extend(_scan_text_for_pii(session_id, i, field, nested))
                elif isinstance(value, str):
                    field = f"tool_uses[{tool_index}].{branch}"
                    findings.extend(_scan_text_for_pii(session_id, i, field, value))
        # Widened message model (phase-2 C1).
        for text, field_label in iter_widened_text_locations(msg):
            findings.extend(_scan_text_for_pii(session_id, i, field_label, text))
    return merge_findings(findings)


# ---------------------------------------------------------------------------
# DB-backed findings adapter (regex_pii engine)
#
# Mirrors `clawjournal.redaction.secrets.scan_session_for_findings` so the
# findings pipeline can persist hashed PII matches and the share-time apply
# path can rebuild redactions deterministically from per-entity decisions.
# ---------------------------------------------------------------------------

PII_ENGINE_ID = "regex_pii"


# (rule_name, compiled_pattern, entity_type, confidence, capture_group, kind)
# `kind` drives the GitHub-orgs skiplist; it is opaque to the substrate.
_PII_CONTENT_PATTERNS_COMPILED: list[tuple[str, "re.Pattern[str]", str, float, int, str]] = [
    ("github_url_username", re.compile(r"github\.com/([A-Za-z0-9_.-]{2,})"), "username", 0.85, 1, "github"),
    ("github_raw_url_username", re.compile(r"raw\.githubusercontent\.com/([A-Za-z0-9_.-]{2,})"), "username", 0.85, 1, "github"),
    ("email", re.compile(r"([A-Za-z0-9_.+-]{3,}@[A-Za-z0-9.-]+\.[A-Za-z]{2,})"), "email", 0.90, 1, "plain"),
    ("email_truncated", re.compile(r"([A-Za-z0-9_.+-]{3,})@(?=\s|$)"), "email", 0.75, 1, "plain"),
    ("telegram_bot_token", re.compile(r"(\d{8,}:[A-Za-z0-9_-]{30,})"), "custom_sensitive", 0.95, 1, "plain"),
    ("personal_hostname", re.compile(r"\b([a-z][a-z0-9]*s?-(?:macbook|imac|laptop|desktop|pc|workstation|server)-?[a-z0-9]*)\b"), "device_id", 0.80, 1, "plain"),
    ("home_dir_path", re.compile(r"(/(?:Users|home)/[A-Za-z0-9._-]{2,}/[^\s\"'`,;)}\]]{3,})"), "path", 0.85, 1, "plain"),
    ("private_ip_10", re.compile(r"\b(10\.\d{1,3}\.\d{1,3}\.\d{1,3})\b"), "custom_sensitive", 0.70, 1, "plain"),
    ("private_ip_172", re.compile(r"\b(172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"), "custom_sensitive", 0.70, 1, "plain"),
    ("private_ip_192", re.compile(r"\b(192\.168\.\d{1,3}\.\d{1,3})\b"), "custom_sensitive", 0.70, 1, "plain"),

    # -----------------------------------------------------------------
    # Added rules — promoted from the AI layer because they show up
    # repeatedly across the user's traces (project names, private URLs,
    # cloud resource ids, personal identifiers that resist name-entity
    # recognition but have distinctive shape).
    # -----------------------------------------------------------------

    # MAC address, colon or hyphen separated.
    ("mac_address", re.compile(r"\b((?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2})\b"), "device_id", 0.95, 1, "plain"),

    # US SSN. Excludes obvious placeholders (000-xx-xxxx, 666-xx-xxxx,
    # 9xx-xx-xxxx, xx-00-xxxx, xx-xx-0000).
    ("ssn_us", re.compile(r"\b((?!000|666|9\d{2})\d{3}-(?!00)\d{2}-(?!0000)\d{4})\b"), "custom_sensitive", 0.90, 1, "plain"),

    # E.164-style phone number. Low confidence — false-positive prone
    # against long version numbers, so keep it below the review
    # threshold; the aggregate summary still surfaces the count.
    ("phone_e164", re.compile(r"(?<!\d)(\+\d{1,3}[-.\s]?\(?\d{1,4}\)?[-.\s]?\d{2,4}[-.\s]?\d{3,9})(?!\d)"), "custom_sensitive", 0.55, 1, "plain"),

    # Hostnames ending in common private TLDs: `.local`, `.internal`,
    # `.corp`, `.lan`, `.intranet`, `.localnet`. Covers intranet hosts
    # that escape the private-IP rules.
    ("internal_tld_host", re.compile(r"\b([a-z0-9][a-z0-9-]*(?:\.[a-z0-9][a-z0-9-]*)*\.(?:local|internal|corp|lan|intranet|localnet))\b", re.IGNORECASE), "private_url", 0.80, 1, "plain"),

    # URLs pointing at RFC1918 addresses (full URL, preserves path /
    # query so the user sees the whole private endpoint redacted).
    ("private_ip_url", re.compile(r"(https?://(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})(?::\d{2,5})?(?:/[^\s\"'`<>]*)?)"), "private_url", 0.90, 1, "plain"),

    # `localhost` URLs — often carry auth tokens or internal paths that
    # shouldn't leak. Kept separate from the RFC1918 rule for clarity.
    ("localhost_url", re.compile(r"(https?://(?:localhost|127\.0\.0\.1)(?::\d{2,5})?(?:/[^\s\"'`<>]*)?)"), "private_url", 0.85, 1, "plain"),

    # npm-style scoped package: `@scope/pkg`.
    ("npm_scoped_package", re.compile(r"(?<![A-Za-z0-9])(@[a-z0-9][a-z0-9-]{1,38}/[a-z0-9][a-z0-9._-]{1,63})\b"), "project_name", 0.70, 1, "plain"),

    # `name@semver` — catches the `project@1.2.3` shape. Requires a
    # proper 3-part semver to minimise false positives against email
    # addresses and timestamps.
    ("package_at_version", re.compile(r"(?<![A-Za-z0-9@])([a-z][a-z0-9-]{2,}(?:/[a-z][a-z0-9-]+)?@\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?)\b"), "project_name", 0.65, 1, "plain"),

    # GitHub `owner/repo` slug. The existing github_url_username rule
    # catches the owner; this rule captures owner-and-repo so the
    # project name is redacted as a unit.
    ("github_repo_slug", re.compile(r"github\.com/([A-Za-z0-9._-]{2,39}/[A-Za-z0-9._-]{1,100})(?![A-Za-z0-9])"), "project_name", 0.80, 1, "github"),

    # Same for GitLab and Bitbucket.
    ("gitlab_repo_slug", re.compile(r"gitlab\.com/([A-Za-z0-9._-]{2,39}(?:/[A-Za-z0-9._-]+){1,4})(?![A-Za-z0-9])"), "project_name", 0.80, 1, "plain"),
    ("bitbucket_repo_slug", re.compile(r"bitbucket\.org/([A-Za-z0-9._-]{2,39}/[A-Za-z0-9._-]+)(?![A-Za-z0-9])"), "project_name", 0.80, 1, "plain"),

    # AWS resource ARN — always contains a 12-digit account id plus a
    # resource path; both are identifying.
    ("aws_arn", re.compile(r"(arn:aws:[a-z0-9-]+:[a-z0-9-]*:\d{12}:[^\s\"'`<>]+)"), "custom_sensitive", 0.95, 1, "plain"),

    # AWS account id embedded in an ECR registry hostname.
    ("aws_account_ecr", re.compile(r"\b(\d{12}\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com)\b"), "custom_sensitive", 0.95, 1, "plain"),

    # GCP project id in the `projects/xxx` form (CLI output, IAM refs,
    # bucket/topic URLs).
    ("gcp_project_id", re.compile(r"projects/([a-z][a-z0-9-]{4,28}[a-z0-9])(?![A-Za-z0-9])"), "project_name", 0.80, 1, "plain"),

    # Credit-card-shaped PANs (13–19 digits, optional space or hyphen
    # separators). No Luhn check — set low confidence to keep it below
    # the review threshold, but redact aggressively since the cost of
    # leaking a real number outweighs a false positive.
    ("credit_card", re.compile(r"(?<!\d)((?:\d[ -]?){12,18}\d)(?!\d)"), "custom_sensitive", 0.60, 1, "plain"),
]

_PII_METADATA_Q = r'\\?"'
_PII_METADATA_PATTERNS_COMPILED: list[tuple[str, "re.Pattern[str]", str, float]] = [
    ("meta_username", re.compile(rf'{_PII_METADATA_Q}username{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "username", 0.98),
    ("meta_sender_id", re.compile(rf'{_PII_METADATA_Q}sender_id{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "user_id", 0.98),
    ("meta_user_chat_id", re.compile(rf'{_PII_METADATA_Q}(?:user_id|chat_id|account_id|sender_id|from_id){_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "user_id", 0.95),
    ("meta_numeric_id", re.compile(rf'{_PII_METADATA_Q}id{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}(\d{{5,}}){_PII_METADATA_Q}'), "user_id", 0.75),
    ("meta_name", re.compile(rf'{_PII_METADATA_Q}name{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "person_name", 0.82),
    ("meta_sender", re.compile(rf'{_PII_METADATA_Q}sender{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "person_name", 0.82),
    ("meta_label", re.compile(rf'{_PII_METADATA_Q}label{_PII_METADATA_Q}\s*:\s*{_PII_METADATA_Q}([^"\\]{{3,}}){_PII_METADATA_Q}'), "person_name", 0.75),
]


def _pii_should_skip(entity_text: str, entity_type: str, kind: str) -> bool:
    if not entity_text or len(entity_text) < 3:
        return True
    if entity_text.startswith("[") and entity_text.endswith("]"):
        return True
    if kind == "github" and entity_text.lower() in _GITHUB_URL_PUBLIC_ORGS:
        return True
    if entity_type == "email" and entity_text.lower().startswith(("noreply@", "no-reply@")):
        return True
    return False


def _pii_user_allowlist_skip(matched_text: str, entity_type: str,
                             user_allowlist: list[dict] | None) -> bool:
    """Same shape as secrets._check_user_allowlist; kept local to avoid
    cross-imports."""
    if not user_allowlist:
        return False
    for entry in user_allowlist:
        etype = entry.get("type", "exact")
        if etype == "exact" and entry.get("text") == matched_text:
            return True
        if etype == "category" and entry.get("match_type") == entity_type:
            return True
        if etype == "pattern":
            regex = entry.get("regex", "")
            if regex:
                try:
                    if re.search(regex, matched_text):
                        return True
                except re.error:
                    pass
    return False


def scan_text_for_pii(text: str, user_allowlist: list[dict] | None = None) -> list[dict]:
    """Find PII matches in `text`. Same return shape as `secrets.scan_text`:
    `[{type, rule, match, start, end, confidence}, ...]`. Offsets are
    Python codepoint indices into `text` (matches `derive_preview`)."""
    if not text:
        return []

    matches: list[dict] = []
    for rule_name, pattern, entity_type, confidence, group, kind in _PII_CONTENT_PATTERNS_COMPILED:
        for m in pattern.finditer(text):
            try:
                entity_text = m.group(group)
            except IndexError:
                continue
            if entity_text is None:
                continue
            if _pii_should_skip(entity_text, entity_type, kind):
                continue
            if _pii_user_allowlist_skip(entity_text, entity_type, user_allowlist):
                continue
            matches.append({
                "type": entity_type,
                "rule": rule_name,
                "match": entity_text,
                "start": m.start(group),
                "end": m.end(group),
                "confidence": confidence,
            })

    for rule_name, pattern, entity_type, confidence in _PII_METADATA_PATTERNS_COMPILED:
        for m in pattern.finditer(text):
            try:
                entity_text = m.group(1)
            except IndexError:
                continue
            if entity_text is None:
                continue
            if _pii_should_skip(entity_text, entity_type, "plain"):
                continue
            if _pii_user_allowlist_skip(entity_text, entity_type, user_allowlist):
                continue
            matches.append({
                "type": entity_type,
                "rule": rule_name,
                "match": entity_text,
                "start": m.start(1),
                "end": m.end(1),
                "confidence": confidence,
            })

    return matches


def _dedupe_overlapping_pii(matches: list[dict]) -> list[dict]:
    """Same span-overlap dedupe as secrets._dedupe_overlapping_matches.
    Multiple PII patterns can match the same span (e.g. `email` and
    `email_truncated`); the longest/highest-confidence match wins."""
    if not matches:
        return []
    ordered = sorted(
        matches,
        key=lambda m: (-(m["end"] - m["start"]), -m["confidence"], m["start"]),
    )
    kept: list[dict] = []
    for cand in ordered:
        if any(cand["start"] < ex["end"] and cand["end"] > ex["start"] for ex in kept):
            continue
        kept.append(cand)
    kept.sort(key=lambda m: m["start"])
    return kept


def scan_session_for_pii_findings(
    session: dict,
    *,
    user_allowlist: list[dict] | None = None,
) -> list[RawFinding]:
    """Mirror of `secrets.scan_session_for_findings` for the regex_pii
    engine. Iterates the same `_iter_text_locations` (imported lazily to
    avoid a circular import) so field/offset semantics line up across
    engines."""
    from .secrets import _iter_text_locations  # local — secrets already imports findings

    findings: list[RawFinding] = []
    for text, field, msg_idx, tool_field, _wk, _wkey in _iter_text_locations(session):
        raw = scan_text_for_pii(text, user_allowlist=user_allowlist)
        for match in _dedupe_overlapping_pii(raw):
            findings.append(RawFinding(
                engine=PII_ENGINE_ID,
                rule=match["rule"],
                entity_type=match["type"],
                entity_text=match["match"],
                field=field,
                offset=match["start"],
                length=match["end"] - match["start"],
                confidence=match["confidence"],
                message_index=msg_idx,
                tool_field=tool_field,
            ))
    return findings


def pii_secret_map_from_text_decisions(
    text: str,
    decisions: dict[str, str],
    user_allowlist: list[dict] | None,
) -> dict[str, str]:
    """Return a `plaintext -> placeholder` map for one text value, dropping
    matches whose hashed entity is `ignored` in `decisions`. Used by
    `apply_findings_to_blob` to merge engines into a single replace pass."""
    out: dict[str, str] = {}
    for match in _dedupe_overlapping_pii(scan_text_for_pii(text, user_allowlist=user_allowlist)):
        matched = match["match"]
        if decisions.get(hash_entity(matched)) == "ignored":
            continue
        out.setdefault(matched, replacement_for_type(match["type"]))
    return out
