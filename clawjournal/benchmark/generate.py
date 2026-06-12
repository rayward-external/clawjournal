"""Deep, backend-orchestrated benchmark generation.

Replicates the multi-pass workflow (deep-read → cluster → design → critique →
assemble) as a Python pipeline over the scoring backend
(:func:`clawjournal.scoring.backends.run_default_agent_task`) — **not** the
Claude Code Workflow tool, which the daemon cannot invoke. Each stage is one or
more bounded backend calls returning JSON we validate.

The backend is reached through a small :class:`BackendCaller` seam so the whole
orchestration (prompt building, bounded parallelism, parsing, dropping on
critique, assembly, validation) is testable with a fake caller — no real LLM in
CI. The default caller wraps ``run_default_agent_task`` (the resolved/default
agent, per the product decision).

Privacy: both the blob extract AND the free-text substrate fields
(``project``, ``learning_summary``, ``score_reason``, …) are run through the
``Anonymizer`` at the deep-read boundary before reaching the backend — blobs are
stored un-anonymized, and the judge *output* substrate is stored verbatim too,
so neither is pre-scrubbed. The anonymizer strips the local user's home/username
only; de-identification of *other* people (names, card last-4s) is steered by the
prompts and enforced by ``schema.find_pii`` on the agent-facing fields.
"""

from __future__ import annotations

import json
import logging
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from ..redaction.anonymizer import Anonymizer
from ..scoring.backends import default_model_for_backend, resolve_backend, run_default_agent_task
from . import schema as bm
from .select import DEFAULT_DEEPREAD_CAP, DEFAULT_WINDOW_DAYS, FailureCandidate, WeekSlice, select_week_failures

logger = logging.getLogger(__name__)

DEFAULT_MAX_WORKERS = 4
BLOB_EXTRACT_MAX_CHARS = 8000

# Generation makes ~40+ calls; default each backend to ClawJournal's fast model
# choice (still through the CLI/subscription — no raw API). Codex model slugs
# vary, so it stays at the CLI default unless the caller passes `--model`.

# Per-stage subprocess ceilings (seconds). The architect call synthesises ALL
# deep-read seeds in one shot — the heaviest single call — and was timing out
# at the old flat 180s; design builds a full task and also runs long. Reads and
# critique are per-item and lighter. These are upper bounds, not expected
# durations: a healthy call returns well under them.
_STAGE_TIMEOUTS: dict[str, int] = {
    "deepread": 240,
    "architect": 600,
    "design": 360,
    "critique": 240,
}

ProgressFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Backend seam
# ---------------------------------------------------------------------------
class BackendCaller(Protocol):
    """Run one JSON-returning agent call for a pipeline ``stage``."""

    def __call__(self, *, stage: str, system_prompt: str, task_prompt: str) -> dict[str, Any]:
        ...


def _extract_json_object(text: str) -> dict[str, Any]:
    """Pull the first balanced top-level JSON object from possibly-fenced text."""
    if not text or not text.strip():
        raise ValueError("empty backend output")
    s = text.strip()
    start = s.find("{")
    if start < 0:
        raise ValueError("no JSON object in backend output")
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(s)):
        c = s[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return json.loads(s[start : i + 1])
    raise ValueError("unbalanced JSON object in backend output")


# Keys some backends (openclaw/hermes) wrap the agent reply in before the inner
# JSON. Walked innermost-first so the real payload is recovered, not the envelope.
_WRAPPER_KEYS = ("text", "message", "result", "reply", "output", "content", "assistant", "response", "completion")


def _wrapper_text_candidates(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for key in _WRAPPER_KEYS:
            if key in value:
                out.extend(_wrapper_text_candidates(value[key]))
    elif isinstance(value, list):
        for item in value:
            out.extend(_wrapper_text_candidates(item))
    return out


def _read_agent_output(resolved: str, stdout: str, out_file: Path) -> dict[str, Any]:
    """Parse the agent's JSON from the right place per backend (codex file vs stdout),
    unwrapping the openclaw/hermes ``--json`` envelope when present."""
    if resolved == "codex" and out_file.exists():
        return _extract_json_object(out_file.read_text(encoding="utf-8"))
    if resolved in ("openclaw", "hermes"):
        try:
            envelope = json.loads(stdout)
        except (TypeError, json.JSONDecodeError):
            envelope = None
        if envelope is not None:
            for candidate in _wrapper_text_candidates(envelope):
                try:
                    return _extract_json_object(candidate)
                except ValueError:
                    continue
    return _extract_json_object(stdout)


@dataclass
class AgentBackendCaller:
    """Production :class:`BackendCaller` over ``run_default_agent_task``.

    Not exercised in CI (needs a real backend); the golden tests drive the
    orchestrator with a fake caller instead.
    """

    backend: str = "auto"
    model: str | None = None
    timeout_seconds: int = 240  # fallback for stages not in _STAGE_TIMEOUTS

    def __post_init__(self) -> None:
        self.resolved = resolve_backend(self.backend)
        # Fall back to the per-backend fast default unless the caller named a model.
        if self.model is None:
            self.model = default_model_for_backend(self.resolved)

    def __call__(self, *, stage: str, system_prompt: str, task_prompt: str) -> dict[str, Any]:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            sys_file = cwd / "system.md"
            sys_file.write_text(system_prompt, encoding="utf-8")
            # Claude consumes the system prompt via file; other backends get it
            # prepended to the task message.
            task = task_prompt if self.resolved == "claude" else f"{system_prompt}\n\n{task_prompt}"
            result = run_default_agent_task(
                backend=self.resolved,
                cwd=cwd,
                system_prompt_file=sys_file,
                task_prompt=task,
                model=self.model,
                timeout_seconds=_STAGE_TIMEOUTS.get(stage, self.timeout_seconds),
                codex_sandbox="read-only",
                codex_output_file="out.json",
                openclaw_message=task,
            )
            return _read_agent_output(self.resolved, result.stdout, cwd / "out.json")


# ---------------------------------------------------------------------------
# Prompts (taxonomy + de-identification + packet discipline are encoded here)
# ---------------------------------------------------------------------------
_TAXONOMY = (
    "task_framing, method_selection, context_handling, execution_error, "
    "reasoning_fabrication, revision_failure, verification_skipped, "
    "deliverable_defect, communication_error, collaboration_error, "
    "safety_security, efficiency_waste, evaluation_measurement"
)

_SYSTEM = (
    "You build a PERSONALIZED, adversarial, judgment-focused agent benchmark from a user's own "
    "coding-agent failure traces. Output ONLY a single JSON object — no prose, no markdown fences. "
    "De-identify: refer to people by role (e.g. 'the PI'), mask account/card digits as ••XXXX, and "
    "never put a person's name, email, home-dir path, or other PII into agent-facing fields "
    "(scenario, seed_inputs, title). Ground every claim in the trace; do not invent failures from a "
    "bare score. Remember that a session scored mid-flight can be mis-graded — judge the terminal "
    f"state, not the opening narration. Failure taxonomy: {_TAXONOMY}."
)


def _deepread_prompt(candidate: FailureCandidate, blob_extract: str, anon: Anonymizer) -> str:
    # Anonymize the free-text substrate too (not just the blob): the judge OUTPUT
    # fields (learning_summary/score_reason/…) and `project` are stored verbatim,
    # so the local user's home/username can ride through to the backend otherwise.
    def a(value: Any) -> Any:
        return anon.text(str(value)) if value else value

    return (
        "# Benchmark stage: deep-read ONE failure trace into a reusable seed.\n\n"
        f"session_id: {candidate.session_id}\nsource: {candidate.source}\nproject: {a(candidate.project)}\n"
        f"failure_value_score: {candidate.failure_value_score}\n"
        f"failure_modes: {a(candidate.failure_modes)}\nrecovery: {a(candidate.recovery_labels)}\n"
        f"attribution: {a(candidate.failure_attribution)}\n"
        f"learning_summary: {a(candidate.learning_summary)}\nscore_reason: {a(candidate.score_reason)}\n\n"
        f"Anonymized trace extract:\n{blob_extract}\n\n"
        "Return JSON: {domain, user_goal, failure_moment, root_cause_categories[] (from the taxonomy), "
        "seductive_wrong_move, correct_behavior, evidence_snippet (<220 chars), "
        "recovery (self_recovered|user_corrected_recovery|unrecovered|blocked|none), "
        "generalizable_trap, severity (low|medium|high)}."
    )


def _architect_prompt(seeds: list[dict[str, Any]]) -> str:
    return (
        "# Benchmark stage: cluster failure seeds into ONE merged set of themes + task stubs.\n\n"
        f"SEEDS (JSON): {json.dumps(seeds)}\n\n"
        "Dedupe across ALL agents: when the same failure appears for >1 agent on the same session "
        "family, emit ONE stub with all source_agents and session ids. Aim for 8-16 stubs spread "
        "across themes/domains.\n"
        "Return JSON: {themes:[{name, taxonomy[], frequency, evidence_session_ids[], lesson}], "
        "stubs:[{id (short slug like S1), theme, domains[], source_agents[], grounded_session_ids[], "
        "concept, why_personalized}]}."
    )


def _design_prompt(stub: dict[str, Any], seeds: list[dict[str, Any]]) -> str:
    relevant = [s for s in seeds if s.get("session_id") in (stub.get("grounded_session_ids") or [])]
    return (
        "# Benchmark stage: design ONE full benchmark task from a stub.\n\n"
        f"STUB (JSON): {json.dumps(stub)}\nGROUNDING SEEDS (JSON): {json.dumps(relevant or seeds)}\n\n"
        "The scenario + seed_inputs are the AGENT PACKET (what the agent under test sees) — they must "
        "withhold the trap/answer and contain no PII. Everything else is the grader packet.\n"
        "Return JSON: {title, scenario, seed_inputs, the_trap, ideal_trajectory[], pass_criteria[] "
        "(observable), fail_signals[], grading (assertion|judge|manual), difficulty (easy|medium|hard), "
        "points (1-5)}."
    )


def _critique_prompt(stub: dict[str, Any], task_body: dict[str, Any]) -> str:
    return (
        "# Benchmark stage: adversarially critique ONE designed task and set its readiness.\n\n"
        f"TASK (JSON): {json.dumps({**stub, **task_body})}\n\n"
        "Judge: discriminating (a strong agent passes while one prone to the failure fails?), gameable, "
        "leakage (does the scenario telegraph the answer?), measurable. Assign readiness: ready | "
        "needs_staging (repo rollback / mock / withheld state needed) | needs_review | local_only "
        "(depends on private artifacts) | retired. Set leakage_risk/privacy_risk (low|medium|high).\n"
        "Return JSON: {discriminating, gameable, leakage, measurable, verdict (keep|revise|drop), "
        "notes, staging_notes, readiness, leakage_risk, privacy_risk, revised_pass_criteria[]}."
    )


# ---------------------------------------------------------------------------
# Blob extraction (anonymized)
# ---------------------------------------------------------------------------
def _blob_extract(blob_path: str | None, anonymizer: Anonymizer, *, max_chars: int = BLOB_EXTRACT_MAX_CHARS) -> str:
    """A bounded, anonymized text extract of the trace for the deep-read prompt."""
    if not blob_path:
        return "(no trace blob available)"
    try:
        data = json.loads(Path(blob_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "(trace blob unavailable)"
    parts: list[str] = []
    for msg in data.get("messages", []) or []:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "?")
        content = msg.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                str(p.get("text", "")) for p in content if isinstance(p, dict) and p.get("text")
            )
        text = str(content).strip()
        if text:
            # Cap any single message so one giant turn can't dominate the budget.
            parts.append(f"[{role}] {text[:2000]}")
    joined = "\n".join(parts)
    if len(joined) > max_chars:
        # Keep a head + tail slice — the failure usually lands in terminal turns,
        # so head-only truncation would drop the most informative part.
        half = max_chars // 2
        joined = f"{joined[:half]}\n…\n{joined[-half:]}"
    return anonymizer.text(joined) if joined else "(empty trace)"


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
def _repo_git_sha() -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _map(fn: Callable[[Any], Any], items: list[Any], max_workers: int) -> list[Any]:
    if not items:
        return []
    workers = max(1, min(max_workers, len(items)))
    if workers == 1:
        return [fn(x) for x in items]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(fn, items))


def _map_progress(fn, items, max_workers, *, label, note):
    """Like :func:`_map` but emits ``label (k/total)`` from the MAIN thread after
    each parallel chunk — so the UI bar advances smoothly and no DB write ever
    happens off the worker thread (the per-item fns run in the pool; progress does
    not)."""
    total = len(items)
    step = max(1, max_workers)
    out: list[Any] = []
    for i in range(0, total, step):
        out.extend(_map(fn, items[i:i + step], max_workers))
        note(f"{label} ({min(i + step, total)}/{total})")
    return out


def generate_benchmark(
    conn,
    *,
    window_days: int = DEFAULT_WINDOW_DAYS,
    cap: int = DEFAULT_DEEPREAD_CAP,
    backend: str = "auto",
    model: str | None = None,
    caller: BackendCaller | None = None,
    anonymizer: Anonymizer | None = None,
    now: datetime | None = None,
    max_workers: int = DEFAULT_MAX_WORKERS,
    progress: ProgressFn | None = None,
    week_slice: WeekSlice | None = None,
) -> bm.Benchmark:
    """Run the deep pipeline and return a validated :class:`~schema.Benchmark`.

    ``caller`` defaults to :class:`AgentBackendCaller` over the resolved/default
    agent; tests pass a fake. ``week_slice`` lets a caller pass a pre-selected
    slice (else :func:`select_week_failures` is run).
    """
    call = caller or AgentBackendCaller(backend=backend, model=model)
    anon = anonymizer if anonymizer is not None else Anonymizer(enabled=True)
    resolved = getattr(call, "resolved", None) or resolve_backend(backend)

    def note(msg: str) -> None:
        if progress is not None:
            progress(msg)
        logger.info("benchmark: %s", msg)

    sl = week_slice or select_week_failures(conn, window_days=window_days, cap=cap, now=now)
    if not sl.candidates:
        raise ValueError("no failure-signal sessions in the selected window")

    # 1. Deep-read (bounded parallel) -> seeds.
    note(f"Reading your recent failures (0/{len(sl.candidates)})")
    extracts = {c.session_id: _blob_extract(c.blob_path, anon) for c in sl.candidates}

    def _deepread(c: FailureCandidate) -> dict[str, Any] | None:
        try:
            seed = call(stage="deepread", system_prompt=_SYSTEM,
                        task_prompt=_deepread_prompt(c, extracts[c.session_id], anon))
        except Exception as exc:  # one bad call must not kill the run
            logger.warning("benchmark deep-read failed for %s: %s", c.session_id, exc)
            return None
        seed["session_id"] = c.session_id
        seed["source"] = c.source
        return seed

    seeds = [s for s in _map_progress(_deepread, list(sl.candidates), max_workers,
                                      label="Reading your recent failures", note=note) if s]
    if not seeds:
        raise ValueError("deep-read produced no seeds")

    # 2. Architect / cluster (single call) -> themes + stubs.
    note("Grouping failures into themes…")
    try:
        arch = call(stage="architect", system_prompt=_SYSTEM, task_prompt=_architect_prompt(seeds))
    except Exception as exc:  # distinct from the empty-week control-flow ValueErrors
        raise RuntimeError(f"benchmark architect stage failed: {exc}") from exc
    themes: list[bm.BenchmarkTheme] = []
    raw_themes = arch.get("themes") or []
    if isinstance(raw_themes, list):
        for t in raw_themes:
            if not isinstance(t, dict) or not t.get("name"):
                logger.warning("benchmark: dropping malformed theme %r", t)
                continue
            themes.append(bm.BenchmarkTheme(**bm._only(bm.BenchmarkTheme, t)))
    else:
        logger.warning("benchmark: architect 'themes' was not a list (%s); dropping",
                       type(raw_themes).__name__)
    stubs = list(arch.get("stubs", []) or [])
    if not stubs:
        raise ValueError("architect produced no task stubs")

    # 3 + 4. Design then critique each stub (bounded parallel, per-item chain).
    note(f"Writing & reviewing benchmark tasks (0/{len(stubs)})")

    def _build(stub: dict[str, Any]) -> bm.BenchmarkTask | None:
        sid = stub.get("id") or "S?"
        # The whole design→critique→construct chain is guarded: a non-int
        # `points`, a non-iterable list field, or any malformed LLM output must
        # drop only THIS task, never abort the run.
        try:
            body = call(stage="design", system_prompt=_SYSTEM, task_prompt=_design_prompt(stub, seeds))
            crit = call(stage="critique", system_prompt=_SYSTEM, task_prompt=_critique_prompt(stub, body))
            if crit.get("verdict") == "drop":
                logger.info("benchmark: dropped task %s (critique verdict=drop)", sid)
                return None
            pass_criteria = crit.get("revised_pass_criteria") or body.get("pass_criteria") or []
            task = bm.BenchmarkTask(
                id=str(sid),
                title=str(body.get("title") or stub.get("concept") or sid),
                theme=str(stub.get("theme") or ""),
                scenario=str(body.get("scenario") or ""),
                seed_inputs=str(body.get("seed_inputs") or ""),
                the_trap=str(body.get("the_trap") or ""),
                ideal_trajectory=list(body.get("ideal_trajectory") or []),
                pass_criteria=list(pass_criteria),
                fail_signals=list(body.get("fail_signals") or []),
                grading=str(body.get("grading") or "judge"),
                difficulty=str(body.get("difficulty") or "medium"),
                points=int(body.get("points") or 3),
                domains=list(stub.get("domains") or []),
                source_agents=list(stub.get("source_agents") or []),
                grounded_session_ids=list(stub.get("grounded_session_ids") or []),
                readiness=str(crit.get("readiness") or "needs_review"),
                leakage_risk=str(crit.get("leakage_risk") or "low"),
                privacy_risk=str(crit.get("privacy_risk") or "low"),
                critique=bm.TaskCritique(
                    discriminating=bool(crit.get("discriminating", True)),
                    gameable=bool(crit.get("gameable", False)),
                    leakage=bool(crit.get("leakage", False)),
                    measurable=bool(crit.get("measurable", True)),
                    verdict=str(crit.get("verdict") or "keep"),
                    notes=str(crit.get("notes") or ""),
                    staging_notes=str(crit.get("staging_notes") or ""),
                ),
            )
        except Exception as exc:
            logger.warning("benchmark design/critique/build failed for %s: %s", sid, exc)
            return None
        # Drop individually-invalid tasks (e.g. PII leak / bad enum) rather than
        # failing the whole run — robustness over strictness.
        errors = bm.validate_task(task)
        if errors:
            logger.warning("benchmark: dropping invalid task %s: %s", sid, errors)
            return None
        return task

    tasks = [t for t in _map_progress(_build, stubs, max_workers,
                                      label="Writing & reviewing benchmark tasks", note=note) if t]
    if not tasks:
        raise ValueError("no tasks survived design/critique/validation")

    note("Finalizing…")
    benchmark = bm.Benchmark(
        window_start=sl.window_start,
        window_end=sl.window_end,
        generated_at=(now or datetime.now(timezone.utc)).isoformat(),
        backend=resolved,
        rubric_git_sha=_repo_git_sha(),
        source_session_ids=[s["session_id"] for s in seeds],
        dropped_for_cost=sl.dropped_for_cost,
        themes=themes,
        tasks=tasks,
    )
    bm.validate_or_raise(benchmark)
    note(f"Done — {len(tasks)} tasks across {len(themes)} themes")
    return benchmark
