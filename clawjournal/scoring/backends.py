"""Shared backend detection, resolution, and agent invocation for coding-agent CLIs.

Used by the scoring pipeline and PII review to
auto-detect whether clawjournal is running under Claude Code, Codex, or
OpenClaw and dispatch to the corresponding automation CLI.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


SUPPORTED_BACKENDS = ("claude", "codex", "hermes", "openclaw")
BACKEND_CHOICES = ("auto", *SUPPORTED_BACKENDS)
AUTO_BACKEND_FALLBACK_ORDER = ("codex", "claude", "hermes", "openclaw")
BACKEND_COMMANDS: dict[str, str] = {
    "claude": "claude",
    "codex": "codex",
    "hermes": "hermes",
    "openclaw": "openclaw",
}
BACKEND_ENV_MARKERS: dict[str, tuple[str, ...]] = {
    "claude": ("CLAUDECODE", "CLAUDE_CODE", "CLAUDECODE_SESSION_ID", "CLAUDE_PROJECT_DIR"),
    "codex": ("CODEX_THREAD_ID", "CODEX_SANDBOX", "CODEX_CI"),
    "hermes": ("HERMES_HOME", "HERMES_CONFIG_PATH", "HERMES_SESSION_ID"),
    "openclaw": ("OPENCLAW_HOME", "OPENCLAW_STATE_DIR", "OPENCLAW_CONFIG_PATH"),
}
BACKEND_COMMAND_ALIASES: dict[str, tuple[str, ...]] = {
    "claude": ("claude",),
    "codex": ("codex",),
    "hermes": ("hermes",),
    "openclaw": ("openclaw",),
}


def _detect_current_agent_from_env(env: dict[str, str] | None = None) -> str | None:
    """Infer the current agent from the process environment."""
    env = os.environ if env is None else env
    for backend, keys in BACKEND_ENV_MARKERS.items():
        for key in keys:
            if env.get(key):
                return backend
    return None


def _get_process_field(pid: int, field: str) -> str:
    """Read a single process field from ps, returning an empty string on failure."""
    try:
        proc = subprocess.run(
            ["ps", f"-o{field}=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def _classify_process_command(comm: str, command: str) -> str | None:
    """Map a process command to a supported backend."""
    fields = " ".join(part for part in (comm, command) if part).lower()
    if not fields:
        return None
    base = Path(comm).name.lower() if comm else ""
    for backend, aliases in BACKEND_COMMAND_ALIASES.items():
        for alias in aliases:
            if base == alias or f" {alias}" in f" {fields}" or f"/{alias}" in fields:
                return backend
    return None


def _detect_current_agent_from_process_tree(pid: int | None = None, *, max_depth: int = 6) -> str | None:
    """Walk parent processes to find a known coding-agent CLI."""
    current_pid = pid if pid is not None else os.getppid()
    depth = 0
    seen: set[int] = set()

    while current_pid > 1 and depth < max_depth and current_pid not in seen:
        seen.add(current_pid)
        comm = _get_process_field(current_pid, "comm")
        command = _get_process_field(current_pid, "command")
        detected = _classify_process_command(comm, command)
        if detected:
            return detected
        parent_text = _get_process_field(current_pid, "ppid")
        try:
            current_pid = int(parent_text)
        except ValueError:
            break
        depth += 1
    return None


def detect_current_agent(env: dict[str, str] | None = None) -> str | None:
    """Detect the current coding agent from env vars or process tree."""
    return _detect_current_agent_from_env(env) or _detect_current_agent_from_process_tree()


def detect_available_backend(env: dict[str, str] | None = None) -> str | None:
    """Detect a usable backend, falling back to installed CLIs."""
    detected = detect_current_agent(env)
    if detected and shutil.which(BACKEND_COMMANDS[detected]) is not None:
        return detected
    for backend in AUTO_BACKEND_FALLBACK_ORDER:
        command = BACKEND_COMMANDS[backend]
        if shutil.which(command) is not None:
            return backend
    return None


def resolve_backend(backend: str = "auto", env: dict[str, str] | None = None) -> str:
    """Resolve 'auto' backend selection to a concrete backend name.

    Priority: explicit value > CLAWJOURNAL_SCORER_BACKEND env >
    current-agent detection > installed CLI fallback.
    """
    env = os.environ if env is None else env
    requested = (backend or "auto").strip().lower()
    if requested != "auto":
        if requested not in SUPPORTED_BACKENDS:
            raise RuntimeError(f"Unsupported backend: {backend}")
        return requested

    override = (env.get("CLAWJOURNAL_SCORER_BACKEND") or "").strip().lower()
    if override:
        if override not in SUPPORTED_BACKENDS:
            raise RuntimeError(
                f"Unsupported CLAWJOURNAL_SCORER_BACKEND value: {override}. "
                f"Use one of: {', '.join(SUPPORTED_BACKENDS)}."
            )
        return override

    detected = detect_available_backend(env)
    if detected:
        return detected

    raise RuntimeError(
        "Could not detect a supported scoring backend. "
        "Install a supported agent CLI, set CLAWJOURNAL_SCORER_BACKEND, "
        "or pass --backend explicitly."
    )


def require_backend_command(backend: str) -> str:
    """Return the CLI command for a backend, ensuring it is installed."""
    command = BACKEND_COMMANDS[backend]
    if shutil.which(command) is None:
        raise RuntimeError(f"{backend} CLI not found. Install it or choose a different --backend.")
    return command


def check_backend_runtime(backend: str, env: dict[str, str] | None = None) -> None:
    """Backend-specific runtime preflight hook (extensible, currently a no-op)."""
    _ = backend, env


def summarize_process_error(stderr: str, stdout: str = "") -> str:
    """Return the most actionable error line from subprocess output."""
    lines: list[str] = []
    for raw in f"{stderr}\n{stdout}".splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WARNING: proceeding, even though we could not update PATH"):
            continue
        if line.startswith("note: run with `RUST_BACKTRACE=1`"):
            continue
        if line.startswith("thread '"):
            continue
        lines.append(line)

    if not lines:
        return ""

    for line in reversed(lines):
        lower = line.lower()
        if (
            lower.startswith("error:")
            or " error " in lower
            or "failed" in lower
            or "unauthorized" in lower
        ):
            return line

    return lines[-1]


def format_codex_runtime_error(returncode: int, stderr: str, stdout: str = "") -> str:
    """Normalize common Codex exec failures into actionable guidance."""
    combined = "\n".join(part.strip() for part in (stderr, stdout) if part and part.strip())
    lower = combined.lower()

    if (
        "failed to lookup address information" in lower
        or "temporary failure in name resolution" in lower
        or "name or service not known" in lower
        or "network is unreachable" in lower
        or "could not resolve host" in lower
    ):
        return (
            "Codex runs through `codex exec` in non-interactive mode. "
            "This process could not reach the Codex backend from the current environment. "
            "If you launched clawjournal inside a network-disabled Codex sandbox, "
            "rerun it from your host shell or with network access."
        )

    if (
        "401" in lower
        or "unauthorized" in lower
        or "not signed in" in lower
        or "authentication required" in lower
    ):
        return (
            "Codex runs through `codex exec` in non-interactive mode. "
            "`codex exec` reuses saved CLI authentication by default; for automation, "
            "run `codex login` or set `CODEX_API_KEY` before running clawjournal."
        )

    if "invalid_json_schema" in lower or "response_format" in lower:
        return (
            "Codex rejected the structured-output schema passed by clawjournal. "
            "Update the local clawjournal install to a build with a valid Codex JSON schema."
        )

    summary = summarize_process_error(stderr, stdout)
    if summary:
        return f"codex exited {returncode}: {summary}"
    return f"codex exited {returncode}"


# ---------------------------------------------------------------------------
# Shared agent invocation
# ---------------------------------------------------------------------------

# Canonical prompt root — all agent system prompts and rubrics live here.
PROMPTS_DIR = Path(__file__).resolve().parent.parent / "prompts" / "agents"


@dataclass
class AgentResult:
    """Result of a single agent subprocess invocation."""

    stdout: str
    stderr: str
    returncode: int
    cwd: Path


def _build_claude_cmd(
    command: str,
    *,
    system_prompt_file: Path | None,
    model: str | None,
    bare: bool = False,
) -> list[str]:
    cmd = [
        command, "-p",
        "--permission-mode", "bypassPermissions",
        "--no-session-persistence",
    ]
    if bare:
        cmd += ["--bare"]
    if model:
        cmd += ["--model", model]
    if system_prompt_file is not None:
        if not system_prompt_file.exists():
            raise FileNotFoundError(
                f"System prompt file not found: {system_prompt_file}"
            )
        cmd += ["--system-prompt-file", str(system_prompt_file)]
    return cmd


def _build_codex_cmd(
    command: str,
    *,
    cwd: Path,
    model: str | None,
    sandbox: str | None,
    output_schema_path: Path | None,
    output_file_path: Path | None,
) -> list[str]:
    cmd = [
        command, "exec",
        "-c", "analytics.enabled=false",
        "--skip-git-repo-check",
        "--ephemeral",
        "--color", "never",
        "-C", str(cwd),
    ]
    if sandbox:
        cmd += ["--sandbox", sandbox]
    if model:
        cmd += ["--model", model]
    if output_schema_path:
        cmd += ["--output-schema", str(output_schema_path)]
    if output_file_path:
        cmd += ["--output-last-message", str(output_file_path)]
    return cmd


def _build_openclaw_cmd(
    command: str,
    *,
    message: str,
    timeout_seconds: int,
) -> list[str]:
    return [
        command, "agent",
        "--message", message,
        "--local",
        "--json",
        "--timeout", str(timeout_seconds),
    ]


def _build_hermes_cmd(
    command: str,
    *,
    message: str,
    model: str | None,
) -> list[str]:
    """Build a Hermes scripted one-shot invocation."""
    cmd = [command, "-z", message]
    if model:
        cmd += ["--model", model]
    return cmd


def run_default_agent_task(
    *,
    backend: str = "auto",
    cwd: Path,
    system_prompt_file: Path | None = None,
    task_prompt: str,
    model: str | None = None,
    timeout_seconds: int = 120,
    codex_sandbox: str | None = "read-only",
    codex_output_schema: dict | None = None,
    codex_output_file: str | None = None,
    openclaw_message: str | None = None,
    claude_bare: bool = False,
) -> AgentResult:
    """Spawn an agent CLI subprocess and return the result.

    This is the single shared entry point for all AI-agent tasks
    (scoring, PII review). Backend-specific CLI
    flag logic lives here; callers own input preparation and output
    parsing.

    Args:
        backend: "auto", "claude", "codex", "hermes", or "openclaw".
        cwd: Working directory for the subprocess.
        system_prompt_file: Path to system prompt (used by Claude's
            ``--system-prompt-file``; ignored by other backends).
        task_prompt: The task instruction. Delivered via stdin (Claude),
            positional arg (Codex), scripted one-shot (Hermes), or
            ``--message`` (OpenClaw).
        model: Optional model override for Claude/Codex.
        timeout_seconds: Subprocess timeout.
        codex_sandbox: Codex sandbox mode ("read-only" or None for
            full access). Ignored by other backends.
        codex_output_schema: JSON schema dict for Codex
            ``--output-schema``. Written to a temp file automatically.
        codex_output_file: Filename for Codex ``--output-last-message``
            (resolved relative to *cwd*). Ignored by other backends.
            Must be a plain filename (no path separators).
        openclaw_message: Custom message for OpenClaw's ``--message``
            flag. Falls back to *task_prompt* if not provided.
        claude_bare: If True, pass ``--bare`` to Claude Code so it
            skips CLAUDE.md auto-discovery and hooks. Use when *cwd*
            points to an untrusted directory.
    """
    resolved = resolve_backend(backend)
    check_backend_runtime(resolved)
    command = require_backend_command(resolved)

    if codex_output_file and ("/" in codex_output_file or "\\" in codex_output_file):
        raise ValueError(
            f"codex_output_file must be a plain filename, got: {codex_output_file!r}"
        )

    if resolved == "claude":
        cmd = _build_claude_cmd(
            command,
            system_prompt_file=system_prompt_file,
            model=model,
            bare=claude_bare,
        )
        try:
            proc = subprocess.run(
                cmd,
                input=task_prompt,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out waiting for claude ({timeout_seconds}s)")

        if proc.returncode != 0:
            summary = summarize_process_error(proc.stderr, proc.stdout)
            raise RuntimeError(f"claude exited {proc.returncode}: {summary}")

        return AgentResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            cwd=cwd,
        )

    if resolved == "codex":
        # Write output-schema to a temp file if provided
        schema_path: Path | None = None
        output_path: Path | None = None
        if codex_output_schema:
            schema_path = cwd / "output_schema.json"
            schema_path.write_text(json.dumps(codex_output_schema), encoding="utf-8")
        if codex_output_file:
            output_path = cwd / codex_output_file

        cmd = _build_codex_cmd(
            command,
            cwd=cwd,
            model=model,
            sandbox=codex_sandbox,
            output_schema_path=schema_path,
            output_file_path=output_path,
        )
        cmd.append(task_prompt)

        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out waiting for codex ({timeout_seconds}s)")

        if proc.returncode != 0:
            raise RuntimeError(
                format_codex_runtime_error(
                    proc.returncode,
                    proc.stderr.strip() if proc.stderr else "",
                    proc.stdout.strip() if proc.stdout else "",
                )
            )

        return AgentResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            cwd=cwd,
        )

    if resolved == "openclaw":
        if model:
            raise RuntimeError(
                "OpenClaw backend does not support --model override from clawjournal"
            )
        message = openclaw_message or task_prompt
        cmd = _build_openclaw_cmd(
            command,
            message=message,
            timeout_seconds=timeout_seconds,
        )
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 10,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out waiting for openclaw ({timeout_seconds}s)")

        if proc.returncode != 0:
            summary = summarize_process_error(proc.stderr, proc.stdout)
            raise RuntimeError(f"openclaw exited {proc.returncode}: {summary}")

        return AgentResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            cwd=cwd,
        )

    if resolved == "hermes":
        cmd = _build_hermes_cmd(command, message=task_prompt, model=model)
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=timeout_seconds + 10,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"Timed out waiting for hermes ({timeout_seconds}s)")

        if proc.returncode != 0:
            summary = summarize_process_error(proc.stderr, proc.stdout)
            raise RuntimeError(f"hermes exited {proc.returncode}: {summary}")

        return AgentResult(
            stdout=proc.stdout or "",
            stderr=proc.stderr or "",
            returncode=proc.returncode,
            cwd=cwd,
        )

    raise RuntimeError(f"Unsupported backend: {resolved}")
