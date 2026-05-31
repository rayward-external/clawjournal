"""Typed shapes, validation, and packet-splitting for personalized benchmarks.

This module is the single source of truth for the benchmark shape: the
dataclasses, their (de)serialization to the JSON stored in
``benchmarks.payload_json``, validation (enum domains, grounding,
de-identification), and the **agent-packet vs grader-packet** split that keeps
the answer key out of anything handed to an agent under test.

Packet separation is load-bearing: the agent packet is the runnable prompt
(scenario + seed inputs only); everything that reveals the trap or the answer —
``the_trap``, ``ideal_trajectory``, ``pass_criteria``, ``fail_signals``,
``critique``, and ``grounded_session_ids`` — is grader-only and must never leak
into the agent packet.
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass, field
from typing import Any

# Bump when the stored payload shape changes incompatibly.
SCHEMA_VERSION = 1

READINESS_STATES = ("ready", "needs_staging", "needs_review", "local_only", "retired")
RISK_LEVELS = ("low", "medium", "high")
GRADING_METHODS = ("assertion", "judge", "manual")
DIFFICULTIES = ("easy", "medium", "hard")
CRITIQUE_VERDICTS = ("keep", "revise", "drop")

# Fields the agent under test may see.
AGENT_PACKET_FIELDS = ("id", "title", "scenario", "seed_inputs")
# Fields that must NEVER appear in an agent packet.
GRADER_ONLY_FIELDS = (
    "the_trap",
    "ideal_trajectory",
    "pass_criteria",
    "fail_signals",
    "critique",
    "grounded_session_ids",
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------
@dataclass
class BenchmarkTheme:
    name: str
    taxonomy: list[str] = field(default_factory=list)
    frequency: int = 0
    evidence_session_ids: list[str] = field(default_factory=list)
    lesson: str = ""


@dataclass
class TaskCritique:
    discriminating: bool = True
    gameable: bool = False
    leakage: bool = False
    measurable: bool = True
    verdict: str = "keep"  # keep|revise|drop
    notes: str = ""
    staging_notes: str = ""


@dataclass
class BenchmarkTask:
    id: str
    title: str
    theme: str
    scenario: str
    seed_inputs: str = ""
    the_trap: str = ""
    ideal_trajectory: list[str] = field(default_factory=list)
    pass_criteria: list[str] = field(default_factory=list)
    fail_signals: list[str] = field(default_factory=list)
    grading: str = "judge"
    difficulty: str = "medium"
    points: int = 3
    domains: list[str] = field(default_factory=list)
    source_agents: list[str] = field(default_factory=list)
    grounded_session_ids: list[str] = field(default_factory=list)
    readiness: str = "needs_review"
    leakage_risk: str = "low"
    privacy_risk: str = "low"
    critique: TaskCritique = field(default_factory=TaskCritique)


@dataclass
class Benchmark:
    window_start: str
    window_end: str
    generated_at: str
    backend: str = ""
    rubric_git_sha: str = ""
    source_session_ids: list[str] = field(default_factory=list)
    dropped_for_cost: int = 0
    themes: list[BenchmarkTheme] = field(default_factory=list)
    tasks: list[BenchmarkTask] = field(default_factory=list)
    schema_version: int = SCHEMA_VERSION
    benchmark_id: str = ""

    # Derived counts — surfaced in the stored payload and used to populate the
    # denormalized run-row columns.
    @property
    def n_tasks(self) -> int:
        return len(self.tasks)

    @property
    def total_points(self) -> int:
        return sum(int(t.points or 0) for t in self.tasks)

    @property
    def ready_count(self) -> int:
        return sum(1 for t in self.tasks if t.readiness == "ready")

    @property
    def needs_staging_count(self) -> int:
        return sum(1 for t in self.tasks if t.readiness == "needs_staging")

    @property
    def source_count(self) -> int:
        return len(self.source_session_ids)


# ---------------------------------------------------------------------------
# (De)serialization — tolerant of extra/missing keys so the stored payload can
# evolve without breaking older rows.
# ---------------------------------------------------------------------------
def _field_names(cls: type) -> set[str]:
    return {f.name for f in dataclasses.fields(cls)}


def _only(cls: type, data: dict[str, Any]) -> dict[str, Any]:
    names = _field_names(cls)
    return {k: v for k, v in (data or {}).items() if k in names}


def benchmark_to_dict(benchmark: Benchmark) -> dict[str, Any]:
    """Serialize, including the derived counts (handy for stored/served payloads)."""
    out = dataclasses.asdict(benchmark)
    out["n_tasks"] = benchmark.n_tasks
    out["total_points"] = benchmark.total_points
    out["ready_count"] = benchmark.ready_count
    out["needs_staging_count"] = benchmark.needs_staging_count
    out["source_count"] = benchmark.source_count
    return out


def task_from_dict(data: dict[str, Any]) -> BenchmarkTask:
    crit_raw = data.get("critique")
    critique = TaskCritique(**_only(TaskCritique, crit_raw)) if isinstance(crit_raw, dict) else TaskCritique()
    base = _only(BenchmarkTask, data)
    base["critique"] = critique
    return BenchmarkTask(**base)


def benchmark_from_dict(data: dict[str, Any]) -> Benchmark:
    themes = [BenchmarkTheme(**_only(BenchmarkTheme, t)) for t in data.get("themes", []) or []]
    tasks = [task_from_dict(t) for t in data.get("tasks", []) or []]
    base = _only(Benchmark, data)
    base.pop("themes", None)
    base.pop("tasks", None)
    return Benchmark(themes=themes, tasks=tasks, **base)


# ---------------------------------------------------------------------------
# Packet split
# ---------------------------------------------------------------------------
def to_agent_packet(task: BenchmarkTask) -> dict[str, Any]:
    """The runnable prompt the agent under test sees — answer withheld.

    By construction contains none of :data:`GRADER_ONLY_FIELDS` (including the
    grounded session ids).
    """
    return {
        "id": task.id,
        "title": task.title,
        "scenario": task.scenario,
        "seed_inputs": task.seed_inputs,
    }


def to_grader_packet(task: BenchmarkTask) -> dict[str, Any]:
    """The answer key — trap, ideal trajectory, pass criteria, grounding, critique."""
    return dataclasses.asdict(task)


# ---------------------------------------------------------------------------
# De-identification heuristic (best-effort guardrail, not a redaction engine)
# ---------------------------------------------------------------------------
# Bounded quantifiers (RFC-ish limits) so an unbounded greedy run can't trigger
# O(n^2) backtracking on a long no-match field (e.g. a large embedded scenario).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,255}\.[A-Za-z]{2,24}")
# 7+ consecutive digits (account / card-PAN-ish runs). Bare 4-digit "last-4"
# values are too noisy to flag automatically — generators are prompted to mask
# them as ``••XXXX`` instead.
_LONG_DIGITS_RE = re.compile(r"\b\d{7,}\b")
# Home-dir paths reveal a username. Generic (any user), unlike
# ``redaction/anonymizer.py`` which only strips the *local* user's home.
_HOME_PATH_RE = re.compile(r"(?:/Users/|/home/|[A-Za-z]:\\Users\\)[A-Za-z0-9._-]+")


def find_pii(text: str) -> list[str]:
    """Return obvious PII tokens (emails, home-dir paths, long digit runs) in ``text``.

    Best-effort: used to reject the most blatant leaks from the agent-facing
    fields, not as a substitute for the deterministic/AI redaction applied at
    export time.
    """
    if not text:
        return []
    hits: list[str] = []
    hits.extend(m.group(0) for m in _EMAIL_RE.finditer(text))
    hits.extend(m.group(0) for m in _HOME_PATH_RE.finditer(text))
    hits.extend(m.group(0) for m in _LONG_DIGITS_RE.finditer(text))
    return hits


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_task(task: BenchmarkTask) -> list[str]:
    errors: list[str] = []
    tid = task.id or "<no-id>"
    if not task.id:
        errors.append("task missing id")
    if not task.title:
        errors.append(f"{tid}: missing title")
    if not task.scenario:
        errors.append(f"{tid}: missing scenario")
    if task.readiness not in READINESS_STATES:
        errors.append(f"{tid}: invalid readiness {task.readiness!r}")
    if task.leakage_risk not in RISK_LEVELS:
        errors.append(f"{tid}: invalid leakage_risk {task.leakage_risk!r}")
    if task.privacy_risk not in RISK_LEVELS:
        errors.append(f"{tid}: invalid privacy_risk {task.privacy_risk!r}")
    if task.grading not in GRADING_METHODS:
        errors.append(f"{tid}: invalid grading {task.grading!r}")
    if task.difficulty not in DIFFICULTIES:
        errors.append(f"{tid}: invalid difficulty {task.difficulty!r}")
    if isinstance(task.points, bool) or not isinstance(task.points, int) or task.points < 0:
        errors.append(f"{tid}: points must be a non-negative int, got {task.points!r}")
    if task.critique.verdict not in CRITIQUE_VERDICTS:
        errors.append(f"{tid}: invalid critique.verdict {task.critique.verdict!r}")
    # Every runnable task must pin >=1 real session. `needs_review` tasks are
    # explicitly un-grounded placeholders, so they're exempt.
    if task.readiness != "needs_review" and not task.grounded_session_ids:
        errors.append(f"{tid}: no grounded_session_ids (required unless needs_review)")
    # Agent-facing fields must be de-identified.
    for fieldname in ("title", "scenario", "seed_inputs"):
        hits = find_pii(getattr(task, fieldname) or "")
        if hits:
            errors.append(f"{tid}: PII in agent-facing {fieldname}: {hits[:3]}")
    return errors


def validate_benchmark(benchmark: Benchmark) -> list[str]:
    """Return a list of human-readable validation errors (empty == valid)."""
    errors: list[str] = []
    if not benchmark.window_start or not benchmark.window_end:
        errors.append("benchmark missing window_start/window_end")
    if not benchmark.generated_at:
        errors.append("benchmark missing generated_at")
    seen_ids: set[str] = set()
    for task in benchmark.tasks:
        if task.id in seen_ids:
            errors.append(f"duplicate task id {task.id!r}")
        seen_ids.add(task.id)
        errors.extend(validate_task(task))
    return errors


def validate_or_raise(benchmark: Benchmark) -> None:
    errors = validate_benchmark(benchmark)
    if errors:
        raise ValueError("invalid benchmark: " + "; ".join(errors))
