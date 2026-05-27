"""CLI for ClawJournal — export and manage coding agent conversation data."""

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, cast

from .redaction.anonymizer import Anonymizer
from .config import CONFIG_FILE, ClawJournalConfig, load_config, normalize_excluded_project_names, save_config
from .parsing.parser import CLAUDE_DIR, CODEX_DIR, COPILOT_DIR, CURSOR_DIR, CUSTOM_DIR, GEMINI_DIR, KIMI_DIR, LOCAL_AGENT_DIR, OPENCODE_DIR, OPENCLAW_DIR, discover_projects, parse_project_sessions
from .scoring.backends import BACKEND_CHOICES
from .redaction.pii import apply_findings_to_session, load_findings, load_jsonl_sessions, review_session_pii, review_session_pii_hybrid, review_session_pii_with_agent, write_findings, write_jsonl_sessions
from .scoring.scoring import SCORING_BACKEND_CHOICES
from .redaction.secrets import _has_mixed_char_types, _shannon_entropy, redact_session

REPO_URL = "https://github.com/rayward-external/clawjournal"
SKILL_URL = "https://raw.githubusercontent.com/rayward-external/clawjournal/main/skills/clawjournal/SKILL.md"

REQUIRED_REVIEW_ATTESTATIONS: dict[str, str] = {
    "asked_full_name": "I asked the user for their full name and scanned for it.",
    "asked_sensitive_entities": "I asked about company/client/internal names and private URLs.",
    "manual_scan_done": "I performed a manual sample scan of exported sessions.",
}
MIN_ATTESTATION_CHARS = 24
MIN_MANUAL_SCAN_SESSIONS = 20

CONFIRM_COMMAND_EXAMPLE = (
    "clawjournal confirm "
    "--full-name \"THEIR FULL NAME\" "
    "--attest-full-name \"Asked for full name and scanned export for THEIR FULL NAME.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE = (
    "clawjournal confirm "
    "--skip-full-name-scan "
    "--attest-full-name \"User declined to share full name; skipped exact-name scan.\" "
    "--attest-sensitive \"Asked about company/client/internal names and private URLs; user response recorded and redactions updated if needed.\" "
    "--attest-manual-scan \"Manually scanned 20 sessions across beginning/middle/end and reviewed findings with the user.\""
)

EXPORT_REVIEW_PUBLISH_STEPS = [
    "Step 1/2: Export locally: clawjournal export --output /tmp/clawjournal_export.jsonl",
    "Step 2/2: Review/redact, then confirm: clawjournal confirm ...",
]

SETUP_TO_PUBLISH_STEPS = [
    "Step 1/5: Run prep/list to review project scope: clawjournal prep && clawjournal list",
    "Step 2/5: Explicitly choose source scope: clawjournal config --source <claude|codex|gemini|all>",
    "Step 3/5: Configure exclusions/redactions and confirm projects: clawjournal config ...",
    "Step 4/5: Export locally: clawjournal export --output /tmp/clawjournal_export.jsonl",
    "Step 5/5: Review and confirm: clawjournal confirm ...",
]

EXPLICIT_SOURCE_CHOICES = {"claude", "codex", "custom", "gemini", "kimi", "opencode", "openclaw", "cursor", "copilot", "aider", "all", "both"}
SOURCE_CHOICES = ["auto", "claude", "codex", "custom", "gemini", "kimi", "opencode", "openclaw", "cursor", "copilot", "aider", "all"]
WORKBENCH_SOURCE_CHOICES = ["claude", "codex", "opencode", "openclaw", "cursor", "copilot", "aider", "gemini", "kimi"]
EVENT_SOURCE_CHOICES = ["auto", "claude", "codex", "openclaw", "all"]
PII_PROVIDER_CHOICES = ("rules", "ai", "hybrid")
FAILURE_VALUE_SOURCE_SCOPE = ("claude", "codex", "opencode", "openclaw")
SCORE_SOURCE_CHOICES = set(WORKBENCH_SOURCE_CHOICES) | {"failure-v1", "all", "auto", "both"}


def _mask_secret(s: str) -> str:
    """Mask a secret string for display, e.g. 'hf_OOgd...oEVH'."""
    if len(s) <= 8:
        return "***"
    return f"{s[:4]}...{s[-4:]}"


def _normalize_pii_provider(provider: str) -> str:
    normalized = (provider or "").strip().lower()
    if normalized == "claude":
        return "ai"
    if normalized in PII_PROVIDER_CHOICES:
        return normalized
    raise ValueError(f"Unsupported PII provider: {provider}")


def _normalize_score_source_filter(raw: str | None) -> str | list[str] | None:
    """Normalize `clawjournal score --source` for the failure-value rollout."""
    if raw is None:
        return list(FAILURE_VALUE_SOURCE_SCOPE)
    value = raw.strip().lower()
    if value in ("", "failure-v1"):
        return list(FAILURE_VALUE_SOURCE_SCOPE)
    if value == "both":
        print(
            "Warning: --source both is deprecated for scoring; use "
            "--source failure-v1 or --source claude,codex,opencode,openclaw.",
            file=sys.stderr,
        )
        return ["claude", "codex"]
    if value in ("all", "auto"):
        return None
    sources = [part.strip().lower() for part in value.split(",") if part.strip()]
    invalid = [source for source in sources if source not in SCORE_SOURCE_CHOICES]
    if invalid:
        allowed = ", ".join(sorted(SCORE_SOURCE_CHOICES))
        print(
            f"Unsupported --source value(s): {', '.join(invalid)}. Allowed: {allowed}",
            file=sys.stderr,
        )
        sys.exit(2)
    return sources or list(FAILURE_VALUE_SOURCE_SCOPE)


def _parse_pii_provider_arg(value: str) -> str:
    try:
        return _normalize_pii_provider(value)
    except ValueError as exc:
        choices = ", ".join((*PII_PROVIDER_CHOICES, "claude"))
        raise argparse.ArgumentTypeError(
            f"invalid choice: {value!r} (choose from {choices}; 'claude' is accepted as an alias for 'ai')"
        ) from exc


def _mask_config_for_display(config: Mapping[str, Any]) -> dict[str, Any]:
    """Return a copy of config with redact_strings values masked."""
    out = dict(config)
    if out.get("redact_strings"):
        out["redact_strings"] = [_mask_secret(s) for s in out["redact_strings"]]
    return out


def _source_label(source_filter: str) -> str:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "both":
        return "Claude Code and Codex"
    if source_filter == "claude":
        return "Claude Code"
    if source_filter == "codex":
        return "Codex"
    if source_filter == "gemini":
        return "Gemini CLI"
    if source_filter == "opencode":
        return "OpenCode"
    if source_filter == "openclaw":
        return "OpenClaw"
    if source_filter == "kimi":
        return "Kimi CLI"
    if source_filter == "cursor":
        return "Cursor"
    if source_filter == "copilot":
        return "Copilot CLI"
    if source_filter == "aider":
        return "Aider"
    if source_filter == "custom":
        return "Custom"
    return "Claude Code, Codex, Cursor, Copilot CLI, Aider, Gemini CLI, OpenCode, OpenClaw, Kimi CLI, or Custom"


def _normalize_source_filter(source_filter: str) -> str:
    if source_filter == "all":
        return "auto"
    return source_filter


def _is_explicit_source_choice(source_filter: str | None) -> bool:
    return source_filter in EXPLICIT_SOURCE_CHOICES


def _resolve_source_choice(
    requested_source: str,
    config: ClawJournalConfig | None = None,
) -> tuple[str, bool]:
    """Resolve source choice from CLI + config.

    Returns:
      (source_choice, explicit) where source_choice is one of
      "claude" | "codex" | "gemini" | "opencode" | "openclaw" | "all" | "auto".
    """
    if _is_explicit_source_choice(requested_source):
        return requested_source, True
    if config:
        configured_source = config.get("source")
        if _is_explicit_source_choice(configured_source):
            return str(configured_source), True
    return "auto", False


def _has_session_sources(source_filter: str = "auto") -> bool:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "claude":
        return CLAUDE_DIR.exists() or LOCAL_AGENT_DIR.exists()
    if source_filter == "codex":
        return CODEX_DIR.exists()
    if source_filter == "both":
        return CLAUDE_DIR.exists() or LOCAL_AGENT_DIR.exists() or CODEX_DIR.exists()
    if source_filter == "gemini":
        return GEMINI_DIR.exists()
    if source_filter == "opencode":
        return OPENCODE_DIR.exists()
    if source_filter == "openclaw":
        return OPENCLAW_DIR.exists()
    if source_filter == "kimi":
        return KIMI_DIR.exists()
    if source_filter == "custom":
        return CUSTOM_DIR.exists()
    if source_filter == "cursor":
        return CURSOR_DIR.exists()
    if source_filter == "copilot":
        return COPILOT_DIR.exists()
    if source_filter == "aider":
        from .parsing.parser import _get_aider_project_index
        return bool(_get_aider_project_index())
    return CLAUDE_DIR.exists() or LOCAL_AGENT_DIR.exists() or CODEX_DIR.exists() or CUSTOM_DIR.exists() or GEMINI_DIR.exists() or KIMI_DIR.exists() or OPENCODE_DIR.exists() or OPENCLAW_DIR.exists() or CURSOR_DIR.exists() or COPILOT_DIR.exists()


def _filter_projects_by_source(projects: list[dict], source_filter: str) -> list[dict]:
    source_filter = _normalize_source_filter(source_filter)
    if source_filter == "auto":
        return projects
    if source_filter == "both":
        return [p for p in projects if p.get("source", "unknown") in ("claude", "codex")]
    return [p for p in projects if p.get("source", "unknown") == source_filter]


def _format_size(size_bytes: int) -> str:
    size = float(size_bytes)
    for unit in ("B", "KB", "MB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} GB"


def _format_token_count(count: int) -> str:
    if count >= 1_000_000_000:
        return f"{count / 1_000_000_000:.1f}B"
    if count >= 1_000_000:
        return f"{count / 1_000_000:.1f}M"
    if count >= 1_000:
        return f"{count / 1_000:.0f}K"
    return str(count)


def _compute_stage(config: ClawJournalConfig) -> tuple[str, int, str | None]:
    """Return (stage_name, stage_number, hf_username).

    hf_username is always None.
    """
    saved = config.get("stage")
    last_export = config.get("last_export")
    if saved == "done" and last_export:
        return ("done", 4, None)
    if saved == "confirmed" and last_export:
        return ("confirmed", 3, None)
    if saved == "review" and last_export:
        return ("review", 3, None)
    return ("configure", 2, None)


def _build_status_next_steps(
    stage: str, config: ClawJournalConfig, hf_user: str | None, repo_id: str | None,
) -> tuple[list[str], str | None]:
    """Return (next_steps, next_command) for the given stage."""
    if stage == "configure":
        projects_confirmed = config.get("projects_confirmed", False)
        configured_source = config.get("source")
        source_confirmed = _is_explicit_source_choice(configured_source)
        list_command = (
            f"clawjournal list --source {configured_source}" if source_confirmed else "clawjournal list"
        )
        steps = []
        if not source_confirmed:
            steps.append(
                "Ask the user to explicitly choose export source scope: Claude Code, Codex, Gemini, or all. "
                "Then set it: clawjournal config --source <claude|codex|gemini|all>. "
                "Do not run export until source scope is explicitly confirmed."
            )
        else:
            steps.append(
                f"Source scope is currently set to '{configured_source}'. "
                "If the user wants a different scope, run: clawjournal config --source <claude|codex|gemini|all>."
            )
        if not projects_confirmed:
            steps.append(
                f"Run: {list_command} — then send the FULL project/folder list to the user in your next message "
                "(name, source, sessions, size, excluded), and ask which to EXCLUDE."
            )
            steps.append(
                "Configure project scope: clawjournal config --exclude \"exact_project_name\" "
                "or clawjournal config --confirm-projects (to include all listed projects). "
                "Do not run export until this folder review is confirmed."
            )
        steps.extend([
            "Ask about GitHub/Discord usernames to anonymize and sensitive strings to redact. "
            "Configure: clawjournal config --redact-usernames \"handle1\" and clawjournal config --redact \"string1\"",
            "When done configuring, export locally: clawjournal export --output /tmp/clawjournal_export.jsonl",
        ])
        # next_command is null because user input is needed before exporting
        return (steps, None)

    if stage == "review":
        return (
            [
                "Ask the user for their full name to run an exact-name privacy check against the export. If they decline, you may skip this check with --skip-full-name-scan and include a clear attestation.",
                "Run PII scan commands and review results with the user.",
                "Ask the user: 'Are there any company names, internal project names, client names, private URLs, or other people's names in your conversations that you'd want redacted? Any custom domains or internal tools?' Add anything they mention with clawjournal config --redact.",
                "Do a deep manual scan: sample ~20 sessions from the export (beginning, middle, end) and scan for names, private URLs, company names, credentials in conversation text, and anything else that looks sensitive. Report findings to the user.",
                "If PII found in any of the above, add redactions (clawjournal config --redact) and re-export: clawjournal export ",
                (
                    "Run: "
                    + CONFIRM_COMMAND_EXAMPLE
                    + " — scans for PII and shows project breakdown."
                ),
            ],
            "clawjournal confirm",
        )

    if stage == "confirmed":
        return (
            [
                "Review is complete. Export data is available locally.",
                "To re-export with updated settings: clawjournal export ",
            ],
            None,
        )

    # done
    return (
        [
            "Export complete. To update later: clawjournal export",
            "To reconfigure: clawjournal prep then clawjournal config",
        ],
        None,
    )


def list_projects(source_filter: str = "auto") -> None:
    """Print all projects as JSON (for agents to parse)."""
    projects = discover_projects(source_filter=source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.")
        return
    config = load_config()
    excluded = set(normalize_excluded_project_names(config.get("excluded_projects", [])))
    print(json.dumps(
        [{"name": p["display_name"], "sessions": p["session_count"],
          "size": _format_size(p["total_size_bytes"]),
          "excluded": p["display_name"] in excluded,
          "source": p.get("source", "unknown")}
         for p in projects],
        indent=2,
    ))


def _merge_config_list(config: ClawJournalConfig, key: str, new_values: list[str]) -> None:
    """Append new_values to a config list (deduplicated, sorted)."""
    existing = set(config.get(key, []))
    existing.update(new_values)
    config[key] = sorted(existing)


def configure(
    repo: str | None = None,
    source: str | None = None,
    exclude: list[str] | None = None,
    redact: list[str] | None = None,
    redact_usernames: list[str] | None = None,
    confirm_projects: bool = False,
):
    """Set config values non-interactively. Lists are MERGED (append), not replaced."""
    config = load_config()
    if repo is not None:
        config["repo"] = repo
    if source is not None:
        config["source"] = source
    if exclude is not None:
        if config.get("excluded_projects"):
            config["excluded_projects"] = normalize_excluded_project_names(
                config["excluded_projects"],
            )
        _merge_config_list(
            config,
            "excluded_projects",
            normalize_excluded_project_names(exclude),
        )
    if redact is not None:
        _merge_config_list(config, "redact_strings", redact)
    if redact_usernames is not None:
        _merge_config_list(config, "redact_usernames", redact_usernames)
    if confirm_projects:
        config["projects_confirmed"] = True
    save_config(config)
    print(f"Config saved to {CONFIG_FILE}")
    print(json.dumps(_mask_config_for_display(config), indent=2))


# Common words that should not be scrubbed during username fragment removal
_FRAGMENT_STOPLIST = frozenset({
    "admin", "code", "data", "home", "info", "main", "node",
    "root", "test", "user", "work", "temp", "prod", "stage",
    "build", "deploy", "agent", "server", "client", "local",
})


def _coarsen_timestamp(ts: str) -> str:
    """Reduce timestamp precision to the hour to limit metadata fingerprinting."""
    if not ts or not isinstance(ts, str):
        return ts
    # ISO format: 2026-03-21T09:15:30.123Z → 2026-03-21T09:00:00Z
    # Preserve original timezone suffix (Z, +05:30, -08:00, etc.)
    m = re.match(r"(\d{4}-\d{2}-\d{2}T\d{2}):\d{2}:\d{2}[^A-Z+\-]*(Z|[+-]\d{2}:\d{2})?", ts)
    if m:
        tz = m.group(2) or ""
        return m.group(1) + ":00:00" + tz
    return ts


def _anonymize_session_metadata(session: dict, anonymizer: Anonymizer, custom_strings: list[str] | None = None) -> None:
    """Anonymize top-level session metadata fields that could leak PII.

    This catches fields that the parser, secrets redactor, and PII scanner
    don't process: project names, git branches, and timestamps.
    """
    # Build hyphen-variant of username for project name matching
    # Claude encodes /home/jane_doe as -home-jane-doe, so _build_project_name
    # may leave username fragments like "aiagent" in the project field.
    username = anonymizer.username
    username_hash = anonymizer.username_hash
    hyphen_username = username.replace("_", "-")
    hyphen_parts = hyphen_username.split("-")

    def _scrub_username_fragments(text: str) -> str:
        """Remove username and its hyphen-split fragments from text."""
        result = anonymizer.text(text)
        # Also try the hyphen-encoded variant
        if hyphen_username != username and len(hyphen_username) >= 4:
            result = re.sub(re.escape(hyphen_username), username_hash, result, flags=re.IGNORECASE)
        # Remove individual fragments of hyphen-split username (e.g., "doe" from "jane-doe")
        # Skip common English/tech words to avoid over-redaction
        for part in hyphen_parts:
            if len(part) >= 4 and part.lower() not in _FRAGMENT_STOPLIST:
                result = re.sub(rf"\b{re.escape(part)}\b", username_hash, result, flags=re.IGNORECASE)
        # Collapse consecutive hashes (e.g., "user_abc-user_abc" → "user_abc")
        escaped_hash = re.escape(username_hash)
        result = re.sub(rf"({escaped_hash}[-_]?)+{escaped_hash}", username_hash, result)
        return result

    # Anonymize project name — strip username-derived prefixes
    project = session.get("project")
    if isinstance(project, str) and project:
        session["project"] = _scrub_username_fragments(project)

    # Anonymize git branch — can contain JIRA tickets, customer names, developer names
    branch = session.get("git_branch")
    if isinstance(branch, str) and branch:
        session["git_branch"] = _scrub_username_fragments(branch)
        # Also redact custom strings from branch names
        if custom_strings:
            for s in custom_strings:
                if s and len(s) >= 3 and s.lower() in session["git_branch"].lower():
                    session["git_branch"] = re.sub(
                        re.escape(s), "[REDACTED]", session["git_branch"], flags=re.IGNORECASE
                    )

    # Anonymize display_title if present (secrets.py handles content but not usernames)
    title = session.get("display_title")
    if isinstance(title, str) and title:
        session["display_title"] = _scrub_username_fragments(title)

    # Coarsen timestamps to hour-level precision
    for ts_field in ("start_time", "end_time"):
        ts = session.get(ts_field)
        if isinstance(ts, str):
            session[ts_field] = _coarsen_timestamp(ts)

    # Coarsen message-level timestamps
    for msg in session.get("messages", []):
        if isinstance(msg, dict) and isinstance(msg.get("timestamp"), str):
            msg["timestamp"] = _coarsen_timestamp(msg["timestamp"])


def export_to_jsonl(
    selected_projects: list[dict],
    output_path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    custom_strings: list[str] | None = None,
) -> dict:
    """Export selected projects to JSONL. Returns metadata."""
    total = 0
    skipped = 0
    total_redactions = 0
    models: dict[str, int] = {}
    total_input_tokens = 0
    total_output_tokens = 0
    project_names = []

    try:
        fh = open(output_path, "w")
    except OSError as e:
        print(f"Error: cannot write to {output_path}: {e}", file=sys.stderr)
        sys.exit(1)

    with fh as f:
        for project in selected_projects:
            print(f"  Parsing {project['display_name']}...", end="", flush=True)
            sessions = parse_project_sessions(
                project["dir_name"], anonymizer=anonymizer,
                include_thinking=include_thinking,
                source=project.get("source", "unknown"),
                locator=project.get("locator"),
            )
            proj_count = 0
            for session in sessions:
                model = session.get("model")
                if not model or model == "<synthetic>":
                    skipped += 1
                    continue

                session, n_redacted, _ = redact_session(session, custom_strings=custom_strings)
                total_redactions += n_redacted

                # Anonymize metadata fields that could leak PII
                _anonymize_session_metadata(session, anonymizer, custom_strings)

                f.write(json.dumps(session, ensure_ascii=False) + "\n")
                total += 1
                proj_count += 1
                models[model] = models.get(model, 0) + 1
                stats = session.get("stats", {})
                total_input_tokens += stats.get("input_tokens", 0)
                total_output_tokens += stats.get("output_tokens", 0)
            if proj_count:
                project_names.append(project["display_name"])
            print(f" {proj_count} sessions")

    return {
        "sessions": total,
        "skipped": skipped,
        "redactions": total_redactions,
        "models": models,
        "projects": project_names,
        "total_input_tokens": total_input_tokens,
        "total_output_tokens": total_output_tokens,
        "exported_at": datetime.now(tz=timezone.utc).isoformat(),
    }


SKILL_TARGETS: dict[str, dict[str, str]] = {
    "claude": {
        "dest_template": ".claude/skills/clawjournal/SKILL.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "openclaw": {
        "dest_template": "CLAWJOURNAL_AGENTS.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "codex": {
        "dest_template": "CLAWJOURNAL_AGENTS.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
    "cline": {
        "dest_template": ".cline/clawjournal/SKILL.md",
        "source_file": "SKILL.md",
        "source_url": SKILL_URL,
    },
}


def update_skill(target: str) -> None:
    """Download and install the clawjournal skill for a coding agent."""
    target_config = SKILL_TARGETS.get(target)
    if not target_config:
        print(f"Error: unknown target '{target}'. Supported: {', '.join(SKILL_TARGETS)}", file=sys.stderr)
        sys.exit(1)

    dest = Path.cwd() / target_config["dest_template"]
    dest.parent.mkdir(parents=True, exist_ok=True)

    # Prefer local skills/ copy (works in dev checkout)
    bundled = Path(__file__).resolve().parent.parent / "skills" / "clawjournal" / target_config["source_file"]
    if bundled.exists():
        content = bundled.read_text()
    else:
        # Fall back to downloading from GitHub
        url = target_config["source_url"]
        print(f"Downloading skill from {url}...")
        try:
            with urllib.request.urlopen(url, timeout=15) as resp:
                content = resp.read().decode()
        except (OSError, urllib.error.URLError) as e:
            print(f"Error downloading skill: {e}", file=sys.stderr)
            sys.exit(1)

    dest.write_text(content)
    print(f"Skill installed to {dest}")
    print(json.dumps({
        "installed": str(dest),
        "target": target,
        "next_steps": [
            "Run: clawjournal scan",
            "Then: clawjournal inbox --json",
            "Or open the full UI: clawjournal serve",
        ],
        "next_command": "clawjournal scan",
    }, indent=2))


def status() -> None:
    """Show current stage and next steps (JSON). Read-only — does not modify config."""
    config = load_config()
    stage, stage_number, _ = _compute_stage(config)

    repo_id = config.get("repo")

    next_steps, next_command = _build_status_next_steps(stage, config, None, repo_id)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "repo": repo_id,
        "source": config.get("source"),
        "projects_confirmed": config.get("projects_confirmed", False),
        "last_export": config.get("last_export"),
        "next_steps": next_steps,
        "next_command": next_command,
    }
    print(json.dumps(result, indent=2))


def _find_export_file(file_path: Path | None) -> Path:
    """Resolve the export file path, or exit with an error."""
    if file_path and file_path.exists():
        return file_path
    if file_path is None:
        for c in [Path("/tmp/clawjournal_export.jsonl"), Path("clawjournal_conversations.jsonl")]:
            if c.exists():
                return c
    print(json.dumps({
        "error": "No export file found.",
        "hint": "Run step 1 first to generate a local export file.",
        "blocked_on_step": "Step 1/2",
        "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
        "next_command": "clawjournal export --output /tmp/clawjournal_export.jsonl",
    }, indent=2))
    sys.exit(1)


def _scan_high_entropy_strings(content: str, max_results: int = 15) -> list[dict]:
    """Scan for high-entropy random strings that might be leaked secrets.

    Complements the regex-based _scan_pii by catching unquoted tokens
    that slipped through Layer 1 (secrets.py) redaction.
    """
    if not content:
        return []

    _CANDIDATE_RE = re.compile(r'[A-Za-z0-9_/+=.-]{20,}')

    # Prefixes already caught by other scans
    _KNOWN_PREFIXES = ("eyJ", "ghp_", "gho_", "ghs_", "ghr_", "sk-", "hf_",
                       "AKIA", "pypi-", "npm_", "xox")

    # Benign prefixes that look random but aren't secrets
    _BENIGN_PREFIXES = ("https://", "http://", "sha256-", "sha384-", "sha512-",
                        "sha1-", "data:", "file://", "mailto:")

    # Substrings that indicate non-secret content
    _BENIGN_SUBSTRINGS = ("node_modules", "[REDACTED]", "package-lock",
                          "webpack", "babel", "eslint", ".chunk.",
                          "vendor/", "dist/", "build/")

    # File extensions that indicate path-like strings
    _FILE_EXTENSIONS = (".py", ".js", ".ts", ".tsx", ".jsx", ".css", ".html",
                        ".json", ".yaml", ".yml", ".toml", ".md", ".rst",
                        ".txt", ".sh", ".go", ".rs", ".java", ".rb", ".php",
                        ".c", ".h", ".cpp", ".hpp", ".swift", ".kt",
                        ".lock", ".cfg", ".ini", ".xml", ".svg", ".png",
                        ".jpg", ".gif", ".woff", ".ttf", ".map", ".vue",
                        ".scss", ".less", ".sql", ".env", ".log")

    _HEX_RE = re.compile(r'^[0-9a-fA-F]+$')
    _UUID_RE = re.compile(
        r'^[0-9a-fA-F]{8}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{4}-?[0-9a-fA-F]{12}$'
    )

    # Collect unique candidates first
    unique_candidates: dict[str, list[int]] = {}
    for m in _CANDIDATE_RE.finditer(content):
        token = m.group(0)
        if token not in unique_candidates:
            unique_candidates[token] = []
        unique_candidates[token].append(m.start())

    results = []
    for token, positions in unique_candidates.items():
        # --- cheap filters first ---

        # Skip known prefixes (already caught by other scans)
        if any(token.startswith(p) for p in _KNOWN_PREFIXES):
            continue

        # Skip hex-only strings (git hashes etc.)
        if _HEX_RE.match(token):
            continue

        # Skip UUIDs (with or without hyphens)
        if _UUID_RE.match(token):
            continue

        # Skip strings containing file extensions
        token_lower = token.lower()
        if any(ext in token_lower for ext in _FILE_EXTENSIONS):
            continue

        # Skip path-like strings (2+ slashes)
        if token.count("/") >= 2:
            continue

        # Skip 3+ dots (domain names, version strings)
        if token.count(".") >= 3:
            continue

        # Skip benign prefixes
        if any(token_lower.startswith(p) for p in _BENIGN_PREFIXES):
            continue

        # Skip benign substrings
        if any(sub in token_lower for sub in _BENIGN_SUBSTRINGS):
            continue

        # Require mixed char types (upper + lower + digit)
        if not _has_mixed_char_types(token):
            continue

        # --- entropy check (most expensive, done last) ---
        entropy = _shannon_entropy(token)
        if entropy < 4.0:
            continue

        # Build context from first occurrence
        pos = positions[0]
        ctx_start = max(0, pos - 40)
        ctx_end = min(len(content), pos + len(token) + 40)
        context = content[ctx_start:ctx_end].replace("\n", " ")

        results.append({
            "match": token,
            "entropy": round(entropy, 2),
            "context": context,
        })

    # Sort by entropy descending, cap at max_results
    results.sort(key=lambda r: r["entropy"], reverse=True)
    return results[:max_results]


def _scan_pii(file_path: Path) -> dict:
    """Run PII regex scans on the export file. Returns dict of findings."""
    import re

    p = str(file_path.resolve())
    scans = {
        "emails": r'[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\.[a-z]{2,}',
        "jwt_tokens": r'eyJ[A-Za-z0-9_-]{20,}',
        "api_keys": r'(ghp_|sk-|hf_)[A-Za-z0-9_-]{10,}',
        "ip_addresses": r'[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}',
    }
    # Known false positives
    fp_emails = {"noreply", "pytest.fixture", "mcp.tool", "mcp.resource",
                 "server.tool", "tasks.loop", "github.com"}
    fp_keys = {"sk-notification"}

    results = {}
    try:
        content = file_path.read_text(errors="replace")
    except OSError:
        return {}

    for name, pattern in scans.items():
        matches = set(re.findall(pattern, content))
        # Filter false positives
        if name == "emails":
            matches = {m for m in matches if not any(fp in m for fp in fp_emails)}
        if name == "api_keys":
            matches = {m for m in matches if m not in fp_keys}
        if matches:
            results[name] = sorted(matches)[:20]  # cap at 20

    high_entropy = _scan_high_entropy_strings(content)
    if high_entropy:
        results["high_entropy_strings"] = high_entropy

    return results


def _normalize_attestation_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return " ".join(value.split()).strip()
    return " ".join(str(value).split()).strip()


def _extract_manual_scan_sessions(attestation: str) -> int | None:
    numbers = [int(n) for n in re.findall(r"\b(\d+)\b", attestation)]
    return max(numbers) if numbers else None


def _scan_for_text_occurrences(
    file_path: Path, query: str, max_examples: int = 5,
) -> dict[str, object]:
    """Scan file for case-insensitive occurrences of query and return a compact summary."""
    pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches = 0
    examples: list[dict[str, object]] = []
    try:
        with open(file_path, errors="replace") as f:
            for line_no, line in enumerate(f, start=1):
                if pattern.search(line):
                    matches += 1
                    if len(examples) < max_examples:
                        excerpt = line.strip()
                        if len(excerpt) > 220:
                            excerpt = f"{excerpt[:220]}..."
                        examples.append({"line": line_no, "excerpt": excerpt})
    except OSError as e:
        return {
            "query": query,
            "match_count": 0,
            "examples": [],
            "error": str(e),
        }
    return {
        "query": query,
        "match_count": matches,
        "examples": examples,
    }


def _collect_review_attestations(
    attest_asked_full_name: object,
    attest_asked_sensitive: object,
    attest_manual_scan: object,
    full_name: str | None,
    skip_full_name_scan: bool = False,
) -> tuple[dict[str, str], dict[str, str], int | None]:
    provided = {
        "asked_full_name": _normalize_attestation_text(attest_asked_full_name),
        "asked_sensitive_entities": _normalize_attestation_text(attest_asked_sensitive),
        "manual_scan_done": _normalize_attestation_text(attest_manual_scan),
    }
    errors: dict[str, str] = {}

    full_name_attestation = provided["asked_full_name"]
    if len(full_name_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_full_name"] = "Provide a detailed text attestation for full-name review."
    else:
        lower = full_name_attestation.lower()
        if skip_full_name_scan:
            mentions_skip = any(
                token in lower
                for token in ("skip", "skipped", "declined", "opt out", "prefer not")
            )
            if "full name" not in lower or not mentions_skip:
                errors["asked_full_name"] = (
                    "When skipping full-name scan, attestation must say the user declined/skipped full name."
                )
        else:
            full_name_lower = (full_name or "").lower()
            full_name_tokens = [t for t in re.split(r"\s+", full_name_lower) if len(t) > 1]
            if "ask" not in lower or "scan" not in lower:
                errors["asked_full_name"] = (
                    "Full-name attestation must mention that you asked the user and scanned the export."
                )
            elif full_name_tokens and not all(token in lower for token in full_name_tokens):
                errors["asked_full_name"] = (
                    "Full-name attestation must reference the same full name passed in --full-name."
                )

    sensitive_attestation = provided["asked_sensitive_entities"]
    if len(sensitive_attestation) < MIN_ATTESTATION_CHARS:
        errors["asked_sensitive_entities"] = (
            "Provide a detailed text attestation for sensitive-entity review."
        )
    else:
        lower = sensitive_attestation.lower()
        asked = "ask" in lower
        topics = any(
            token in lower
            for token in ("company", "client", "internal", "url", "domain", "tool", "name")
        )
        outcome = any(
            token in lower
            for token in ("none", "no", "redact", "added", "updated", "configured")
        )
        if not asked or not topics or not outcome:
            errors["asked_sensitive_entities"] = (
                "Sensitive attestation must say what you asked and the outcome "
                "(none found or redactions updated)."
            )

    manual_attestation = provided["manual_scan_done"]
    manual_sessions = _extract_manual_scan_sessions(manual_attestation)
    if len(manual_attestation) < MIN_ATTESTATION_CHARS:
        errors["manual_scan_done"] = "Provide a detailed text attestation for the manual scan."
    else:
        lower = manual_attestation.lower()
        if "manual" not in lower or "scan" not in lower:
            errors["manual_scan_done"] = (
                "Manual scan attestation must explicitly mention a manual scan."
            )
        elif manual_sessions is None or manual_sessions < MIN_MANUAL_SCAN_SESSIONS:
            errors["manual_scan_done"] = (
                f"Manual scan attestation must include a reviewed-session count >= {MIN_MANUAL_SCAN_SESSIONS}."
            )

    return provided, errors, manual_sessions


def confirm(
    file_path: Path | None = None,
    full_name: str | None = None,
    attest_asked_full_name: str | None = None,
    attest_asked_sensitive: str | None = None,
    attest_manual_scan: str | None = None,
    skip_full_name_scan: bool = False,
) -> None:
    """Scan export for PII, summarize projects, and record review state. JSON output."""
    config = load_config()
    last_export = config.get("last_export", {})
    file_path = _find_export_file(file_path)

    normalized_full_name = _normalize_attestation_text(full_name)
    if skip_full_name_scan and normalized_full_name:
        print(json.dumps({
            "error": "Use either --full-name or --skip-full-name-scan, not both.",
            "hint": (
                "Provide --full-name for an exact-name scan, or use --skip-full-name-scan "
                "if the user declines sharing their name."
            ),
            "blocked_on_step": "Step 2/2",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
        sys.exit(1)
    if not normalized_full_name and not skip_full_name_scan:
        print(json.dumps({
            "error": "Missing required --full-name for verification scan.",
            "hint": (
                "Ask the user for their full name and pass it via --full-name "
                "to run an exact-name privacy check. If the user declines, rerun with "
                "--skip-full-name-scan and a full-name attestation describing the skip."
            ),
            "blocked_on_step": "Step 2/2",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_SKIP_FULL_NAME_EXAMPLE,
        }, indent=2))
        sys.exit(1)

    attestations, attestation_errors, manual_scan_sessions = _collect_review_attestations(
        attest_asked_full_name=attest_asked_full_name,
        attest_asked_sensitive=attest_asked_sensitive,
        attest_manual_scan=attest_manual_scan,
        full_name=normalized_full_name if normalized_full_name else None,
        skip_full_name_scan=skip_full_name_scan,
    )
    if attestation_errors:
        print(json.dumps({
            "error": "Missing or invalid review attestations.",
            "attestation_errors": attestation_errors,
            "required_attestations": REQUIRED_REVIEW_ATTESTATIONS,
            "blocked_on_step": "Step 2/2",
            "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
            "next_command": CONFIRM_COMMAND_EXAMPLE,
        }, indent=2))
        sys.exit(1)

    if skip_full_name_scan:
        full_name_scan = {
            "query": None,
            "match_count": 0,
            "examples": [],
            "skipped": True,
            "reason": "User declined sharing full name; exact-name scan skipped.",
        }
    else:
        full_name_scan = _scan_for_text_occurrences(file_path, normalized_full_name)

    # Read and summarize
    projects: dict[str, int] = {}
    models: dict[str, int] = {}
    total = 0
    try:
        with open(file_path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                total += 1
                proj = row.get("project", "<unknown>")
                projects[proj] = projects.get(proj, 0) + 1
                model = row.get("model", "<unknown>")
                models[model] = models.get(model, 0) + 1
    except (OSError, json.JSONDecodeError) as e:
        print(json.dumps({"error": f"Cannot read {file_path}: {e}"}))
        sys.exit(1)

    file_size = file_path.stat().st_size
    repo_id = config.get("repo")

    # Run PII scans
    pii_findings = _scan_pii(file_path)

    # Advance stage from review -> confirmed
    config["stage"] = "confirmed"
    config["review_attestations"] = attestations
    config["review_verification"] = {
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    config["last_confirm"] = {
        "timestamp": datetime.now(tz=timezone.utc).isoformat(),
        "file": str(file_path.resolve()),
        "pii_findings": bool(pii_findings),
        "full_name": normalized_full_name if not skip_full_name_scan else None,
        "full_name_scan_skipped": skip_full_name_scan,
        "full_name_matches": full_name_scan.get("match_count", 0),
        "manual_scan_sessions": manual_scan_sessions,
    }
    save_config(config)

    next_steps = [
        "Show the user the project breakdown, full-name scan, and PII scan results above.",
    ]
    if full_name_scan.get("skipped"):
        next_steps.append(
            "Full-name scan was skipped at user request. Ensure this was explicitly reviewed with the user."
        )
    elif full_name_scan.get("match_count", 0):
        next_steps.append(
            "Full-name scan found matches. Review them with the user and redact if needed, then re-export."
        )
    if pii_findings:
        next_steps.append(
            "PII findings detected — review each one with the user. "
            "If real: clawjournal config --redact \"string\" then re-export. "
            "False positives can be ignored."
        )
    if "high_entropy_strings" in pii_findings:
        next_steps.append(
            "High-entropy strings detected — these may be leaked secrets (API keys, tokens, "
            "passwords) that escaped automatic redaction. Review each one using the provided "
            "context snippets. If any are real secrets, redact with: "
            "clawjournal config --redact \"the_secret\" then re-export."
        )
    next_steps.extend([
        "If any project should be excluded, run: clawjournal config --exclude \"exact_project_name\" and re-export.",
        f"Review is complete. {total} sessions ({_format_size(file_size)}) exported locally.",
    ])

    result = {
        "stage": "confirmed",
        "stage_number": 3,
        "total_stages": 4,
        "file": str(file_path.resolve()),
        "file_size": _format_size(file_size),
        "total_sessions": total,
        "projects": [
            {"name": name, "sessions": count}
            for name, count in sorted(projects.items(), key=lambda x: -x[1])
        ],
        "models": {m: c for m, c in sorted(models.items(), key=lambda x: -x[1])},
        "pii_scan": pii_findings if pii_findings else "clean",
        "full_name_scan": full_name_scan,
        "manual_scan_sessions": manual_scan_sessions,
        "repo": repo_id,
        "last_export_timestamp": last_export.get("timestamp"),
        "next_steps": next_steps,
        "next_command": None,
        "attestations": attestations,
    }
    print(json.dumps(result, indent=2))


def prep(source_filter: str = "auto") -> None:
    """Data prep — discover projects, output JSON.

    Designed to be called by an agent which handles the interactive parts.
    Outputs pure JSON to stdout so agents can parse it directly.
    """
    config = load_config()
    resolved_source_choice, source_explicit = _resolve_source_choice(source_filter, config)
    effective_source_filter = _normalize_source_filter(resolved_source_choice)

    if not _has_session_sources(effective_source_filter):
        if effective_source_filter == "claude":
            err = "~/.claude was not found."
        elif effective_source_filter == "codex":
            err = "~/.codex was not found."
        elif effective_source_filter == "gemini":
            from .parsing.parser import GEMINI_DIR
            err = f"{GEMINI_DIR} was not found."
        else:
            err = "None of ~/.claude, ~/.codex, or ~/.gemini/tmp were found."
        print(json.dumps({"error": err}))
        sys.exit(1)

    projects = discover_projects(source_filter=effective_source_filter)
    if not projects:
        print(json.dumps({"error": f"No {_source_label(effective_source_filter)} sessions found."}))
        sys.exit(1)

    excluded = set(normalize_excluded_project_names(config.get("excluded_projects", [])))

    # Use _compute_stage to determine where we are
    stage, stage_number, _ = _compute_stage(config)

    repo_id = config.get("repo")

    # Build contextual next_steps
    stage_config = cast(ClawJournalConfig, dict(config))
    if source_explicit:
        stage_config["source"] = resolved_source_choice
    next_steps, next_command = _build_status_next_steps(stage, stage_config, None, repo_id)

    # Persist stage
    config["stage"] = stage
    save_config(config)

    result = {
        "stage": stage,
        "stage_number": stage_number,
        "total_stages": 4,
        "next_command": next_command,
        "requested_source_filter": source_filter,
        "source_filter": resolved_source_choice,
        "source_selection_confirmed": source_explicit,
        "repo": repo_id,
        "projects": [
            {
                "name": p["display_name"],
                "sessions": p["session_count"],
                "size": _format_size(p["total_size_bytes"]),
                "excluded": p["display_name"] in excluded,
                "source": p.get("source", "unknown"),
            }
            for p in projects
        ],
        "redact_strings": [_mask_secret(s) for s in config.get("redact_strings", [])],
        "redact_usernames": config.get("redact_usernames", []),
        "config_file": str(CONFIG_FILE),
        "next_steps": next_steps,
    }
    print(json.dumps(result, indent=2))


def _run_scan(source_filter: str | None = None) -> None:
    """One-shot scan: index sessions into the workbench database."""
    from .workbench.daemon import Scanner
    from .workbench.index import get_stats, open_index
    from .pricing import ensure_pricing_fresh

    ensure_pricing_fresh()
    scanner = Scanner(source_filter=source_filter)
    print("Scanning sessions...")
    results = scanner.scan_once()

    total_new = sum(results.values())
    if total_new:
        print(f"Indexed {total_new} new sessions:")
        for source, count in sorted(results.items()):
            if count > 0:
                print(f"  {source}: {count}")
    else:
        print("No new sessions found.")

    conn = open_index()
    try:
        linked = scanner.last_linked_count
        if linked:
            print(f"Linked {linked} subagent relationships.")

        stats = get_stats(conn)
    finally:
        conn.close()

    print(f"\nTotal indexed: {stats['total']}")
    if stats["by_status"]:
        for status, count in sorted(stats["by_status"].items()):
            print(f"  {status}: {count}")
    if stats["by_source"]:
        print("By source:")
        for source, count in sorted(stats["by_source"].items()):
            print(f"  {source}: {count}")


def _run_inbox(
    status: str | None = None,
    source: str | None = None,
    limit: int = 20,
    output_json: bool = False,
) -> None:
    """Show indexed sessions in the terminal."""
    from .workbench.index import get_stats, open_index, query_sessions

    conn = open_index()
    sessions = query_sessions(conn, status=status, source=source, limit=limit,
                              exclude_segmented_parents=True)
    stats = get_stats(conn)
    conn.close()

    if output_json:
        # Parse JSON fields for clean output
        items = []
        for i, s in enumerate(sessions, 1):
            value_badges = s.get("value_badges", [])
            if isinstance(value_badges, str):
                try:
                    value_badges = json.loads(value_badges)
                except (json.JSONDecodeError, ValueError):
                    value_badges = []
            risk_badges = s.get("risk_badges", [])
            if isinstance(risk_badges, str):
                try:
                    risk_badges = json.loads(risk_badges)
                except (json.JSONDecodeError, ValueError):
                    risk_badges = []
            items.append({
                "index": i,
                "session_id": s.get("session_id"),
                "display_title": s.get("display_title", ""),
                "source": s.get("source", ""),
                "model": s.get("model"),
                "messages": s.get("user_messages", 0) + s.get("assistant_messages", 0),
                "tokens": s.get("input_tokens", 0) + s.get("output_tokens", 0),
                "outcome_badge": s.get("outcome_badge"),
                "value_badges": value_badges,
                "risk_badges": risk_badges,
                "ai_failure_value_score": s.get("ai_failure_value_score"),
                "ai_quality_score": s.get("ai_quality_score"),
                "ai_score_reason": s.get("ai_score_reason"),
                "review_status": s.get("review_status", "new"),
                "project": s.get("project", ""),
                "task_type": s.get("task_type"),
                "start_time": s.get("start_time"),
            })
        print(json.dumps({
            "sessions": items,
            "total": stats["total"],
            "showing": len(items),
            "by_status": stats.get("by_status", {}),
        }, indent=2))
        return

    if not sessions:
        print("No sessions found. Run `clawjournal scan` first.")
        return

    # Print a compact table
    print(f"{'Status':<12} {'Source':<10} {'Model':<25} {'Msgs':>5} {'Tokens':>8}  Title")
    print("-" * 100)
    for s in sessions:
        title = (s.get("display_title") or "")[:45]
        model = (s.get("model") or "")[:24]
        msgs = s.get("user_messages", 0) + s.get("assistant_messages", 0)
        tokens = s.get("input_tokens", 0) + s.get("output_tokens", 0)
        status_str = s.get("review_status", "new")
        source_str = s.get("source", "")
        # Badges
        badges = []
        outcome = s.get("outcome_badge", "")
        if outcome and outcome != "unknown":
            badges.append(outcome)
        try:
            value_badges = json.loads(s.get("value_badges", "[]")) if isinstance(s.get("value_badges"), str) else (s.get("value_badges") or [])
        except (json.JSONDecodeError, ValueError):
            value_badges = []
        try:
            risk_badges = json.loads(s.get("risk_badges", "[]")) if isinstance(s.get("risk_badges"), str) else (s.get("risk_badges") or [])
        except (json.JSONDecodeError, ValueError):
            risk_badges = []
        badges.extend(value_badges[:2])
        badges.extend(risk_badges[:2])
        badge_str = f" [{', '.join(badges)}]" if badges else ""

        print(f"{status_str:<12} {source_str:<10} {model:<25} {msgs:>5} {tokens:>8}  {title}{badge_str}")

    print(f"\n{len(sessions)} sessions shown. Use `clawjournal serve` for the full review UI.")


def _run_review_action(
    action: str,
    session_ids: list[str],
    reason: str | None = None,
) -> None:
    """Update session review status for one or more sessions."""
    from .workbench.index import open_index, update_session

    if not session_ids:
        print(json.dumps({"error": "No session IDs provided."}))
        sys.exit(1)

    conn = open_index()
    results = []
    for sid in session_ids:
        ok = update_session(conn, sid, status=action, reason=reason)
        results.append({"session_id": sid, "ok": ok})
    conn.close()

    success = sum(1 for r in results if r["ok"])
    print(json.dumps({
        "action": action,
        "updated": success,
        "not_found": len(results) - success,
        "results": results,
    }, indent=2))


def _resolve_share_id(conn, prefix: str) -> str | None:
    """Resolve a bundle ID prefix to a full bundle ID.

    Returns the share_id, or None if not found.
    Prints an error and exits if the prefix is ambiguous.
    """
    from .workbench.index import get_share, get_shares

    # Try exact match first
    if get_share(conn, prefix) is not None:
        return prefix

    # Try prefix match
    shares = get_shares(conn)
    matches = [b["share_id"] for b in shares if b["share_id"].startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"Ambiguous prefix '{prefix}' — matches {len(matches)} bundles. Use a longer prefix.")
        sys.exit(1)
    return None


def _run_bundle_create(args) -> None:
    """Create a bundle from session IDs or by review status."""
    from .workbench.index import create_share, open_index, query_sessions

    conn = open_index()
    try:
        session_ids = list(args.session_ids) if args.session_ids else []

        if args.status and not session_ids:
            sessions = query_sessions(conn, status=args.status, limit=10000)
            session_ids = [s["session_id"] for s in sessions]

        if not session_ids:
            print("No sessions to bundle. Provide session IDs or use --status approved.")
            sys.exit(1)

        share_id = create_share(
            conn, session_ids,
            attestation=args.attestation,
            note=args.note,
        )
        # Get actual count from DB (create_share only links IDs that exist)
        from .workbench.index import get_share
        share = get_share(conn, share_id)
        actual_count = share["session_count"] if share else 0
        if getattr(args, "json", False):
            print(json.dumps({
                "share_id": share_id,
                "bundle_id": share_id,
                "session_count": actual_count,
                "status": "draft",
            }, indent=2))
        else:
            print(f"Created bundle {share_id[:8]} — {actual_count} sessions.")
    finally:
        conn.close()


def _run_bundle_list(args=None) -> None:
    """List all bundles."""
    from .workbench.index import get_shares, open_index

    output_json = getattr(args, "json", False) if args else False

    conn = open_index()
    try:
        shares = get_shares(conn)
        if output_json:
            items = []
            for b in shares:
                items.append({
                    "share_id": b["share_id"],
                    "bundle_id": b["share_id"],
                    "status": b.get("status", "draft"),
                    "session_count": b.get("session_count", 0),
                    "created_at": b.get("created_at"),
                    "shared_at": b.get("shared_at"),
                })
            print(json.dumps({
                "shares": items,
                "bundles": items,
                "total": len(items),
            }, indent=2))
        else:
            if not shares:
                print("No shares.")
                return
            print(f"{'ID':<10} {'Status':<10} {'Sessions':>8}  Created")
            print("-" * 55)
            for b in shares:
                bid = b["share_id"][:8]
                status = b.get("status", "draft")
                count = b.get("session_count", 0)
                created = (b.get("created_at") or "")[:10]
                print(f"{bid:<10} {status:<10} {count:>8}  {created}")
            print(f"\n{len(shares)} shares.")
    finally:
        conn.close()


def _run_bundle_view(args) -> None:
    """View bundle details with linked sessions."""
    from .workbench.index import get_share, open_index

    conn = open_index()
    try:
        share_id = _resolve_share_id(conn, args.share_id)
        if share_id is None:
            print(f"Bundle not found: {args.share_id}")
            sys.exit(1)

        share = get_share(conn, share_id)
        if share is None:
            print(f"Bundle not found: {share_id}")
            sys.exit(1)

        sessions = share.get("sessions", [])

        if getattr(args, "json", False):
            sessions_summary = []
            for s in sessions:
                sessions_summary.append({
                    "session_id": s.get("session_id"),
                    "display_title": s.get("display_title", ""),
                    "source": s.get("source", ""),
                    "model": s.get("model"),
                    "project": s.get("project", ""),
                    "review_status": s.get("review_status", "new"),
                })
            output = {
                "share_id": share["share_id"],
                "bundle_id": share["share_id"],
                "status": share.get("status", "draft"),
                "session_count": share.get("session_count", 0),
                "created_at": share.get("created_at"),
                "shared_at": share.get("shared_at"),
                "attestation": share.get("attestation"),
                "submission_note": share.get("submission_note"),
                "sessions": sessions_summary,
            }
            print(json.dumps(output, indent=2, default=str))
        else:
            print(f"Share {share_id[:8]}  status={share.get('status', 'draft')}  sessions={len(sessions)}")
            if share.get("submission_note"):
                print(f"  note: {share['submission_note']}")
            if sessions:
                print(f"\n{'Source':<10} {'Model':<25}  Title")
                print("-" * 70)
                for s in sessions:
                    title = (s.get("display_title") or "")[:35]
                    model = (s.get("model") or "")[:24]
                    print(f"{s.get('source', ''):<10} {model:<25}  {title}")
    finally:
        conn.close()


def _run_bundle_export(args) -> None:
    """Export a bundle to disk as JSONL + manifest."""
    from .workbench.index import (
        export_share_to_disk,
        get_effective_share_settings,
        get_share,
        open_index,
    )

    config = load_config()

    conn = open_index()
    try:
        settings = get_effective_share_settings(conn, config)
        share_id = _resolve_share_id(conn, args.share_id)
        if share_id is None:
            print(f"Bundle not found: {args.share_id}")
            sys.exit(1)

        share = get_share(conn, share_id)
        if share is None:
            print(f"Bundle not found: {share_id}")
            sys.exit(1)

        export_dir, manifest = export_share_to_disk(
            conn,
            share_id,
            share,
            output_path=args.output,
            custom_strings=settings["custom_strings"],
            extra_usernames=settings["extra_usernames"],
            excluded_projects=settings["excluded_projects"],
            blocked_domains=settings["blocked_domains"],
            allowlist_entries=settings["allowlist_entries"],
        )
        if export_dir is None:
            print("Output path must be under home directory or /tmp.")
            sys.exit(1)

        if manifest.get("blocked"):
            print(f"Share blocked: {manifest.get('block_reason', 'unknown')}")
            print(manifest.get("block_message", ""))
            print(f"Report: {export_dir}/trufflehog.json")
            sys.exit(2)

        session_count = len(manifest.get("sessions", []))
        files = ["sessions.jsonl", "manifest.json", "trufflehog.json"]

        # Optional training-format conversion
        training_summary = None
        if getattr(args, "training_format", False):
            from .export.training_data import convert_sessions_to_training
            sessions_path = export_dir / "sessions.jsonl"
            training_path = export_dir / "sessions.training.jsonl"
            sessions = load_jsonl_sessions(sessions_path)
            training_summary = convert_sessions_to_training(sessions, training_path)
            files.append("sessions.training.jsonl")

        if getattr(args, "json", False):
            result = {
                "export_path": str(export_dir),
                "session_count": session_count,
                "files": files,
            }
            if training_summary:
                result["training"] = training_summary
            print(json.dumps(result, indent=2))
        else:
            print(f"Exported {session_count} sessions to {export_dir}/")
            if training_summary:
                print(f"Training format: {training_summary['turns']} turns → {export_dir}/sessions.training.jsonl")
    finally:
        conn.close()


_REDACTION_TYPE_LABELS: dict[str, str] = {
    "jwt": "JWT tokens",
    "jwt_partial": "JWT tokens (partial)",
    "db_url": "database URLs",
    "anthropic_key": "Anthropic API keys",
    "openai_key": "OpenAI API keys",
    "hf_token": "Hugging Face tokens",
    "github_token": "GitHub tokens",
    "pypi_token": "PyPI tokens",
    "npm_token": "NPM tokens",
    "aws_key": "AWS access keys",
    "aws_secret": "AWS secret keys",
    "slack_token": "Slack tokens",
    "discord_webhook": "Discord webhook URLs",
    "private_key": "private keys",
    "cli_token_flag": "CLI token flags",
    "env_secret": "environment secrets",
    "generic_secret": "generic secrets",
    "bearer": "bearer tokens",
    "ip_address": "IP addresses",
    "url_token": "URL tokens",
    "email": "email addresses",
    "high_entropy": "high-entropy strings",
    "custom": "custom redaction strings",
    "blocked_domain": "blocked domains",
}


def _format_redaction_summary(redaction_summary: dict) -> str:
    """Format a redaction summary dict into a human-readable string."""
    total = redaction_summary.get("total_redactions", 0)
    by_type = redaction_summary.get("by_type", {})
    if total == 0:
        return "No secrets or sensitive data detected."
    parts = []
    for rtype, count in sorted(by_type.items(), key=lambda x: -x[1]):
        label = _REDACTION_TYPE_LABELS.get(rtype, rtype.replace("_", " "))
        parts.append(f"{count} {label}")
    if parts:
        return f"{total} redactions applied: {', '.join(parts)}."
    return f"{total} redactions applied."


def _share_pii_status() -> dict[str, Any]:
    config = load_config()
    last_export = config.get("last_export") or {}
    if not last_export:
        return {
            "level": "warn",
            "message": "No recent export record found. Consider exporting with --pii-review before sharing.",
            "last_export": None,
            "pii_review": None,
            "pii_apply": None,
        }
    pii_review = last_export.get("pii_review") if isinstance(last_export, dict) else None
    pii_apply = last_export.get("pii_apply") if isinstance(last_export, dict) else None
    if pii_apply:
        return {
            "level": "info",
            "message": f"Recent export has sanitized output available at {pii_apply.get('output')}",
            "last_export": last_export,
            "pii_review": pii_review,
            "pii_apply": pii_apply,
        }
    if pii_review:
        return {
            "level": "warn",
            "message": f"Recent export has findings ({pii_review.get('finding_count')}) but no sanitized JSONL was generated.",
            "last_export": last_export,
            "pii_review": pii_review,
            "pii_apply": None,
        }
    return {
        "level": "warn",
        "message": "Recent export does not include structured PII review. Consider rerunning export with --pii-review --pii-apply before sharing.",
        "last_export": last_export,
        "pii_review": None,
        "pii_apply": None,
    }


def _print_share_pii_warning(output_json: bool = False) -> dict[str, Any]:
    status = _share_pii_status()
    if output_json:
        return status
    if status["level"] == "info":
        print(f"PII status: {status['message']}")
    else:
        print(f"PII warning: {status['message']}")
    return status


def _run_verify_email(args) -> None:
    """Handle the verify-email CLI command."""
    from .workbench.daemon import (
        _is_edu_email,
        confirm_email_verification,
        request_email_verification,
    )

    config = load_config()
    existing = config.get("verified_email")
    existing_token = config.get("verified_email_token")

    if not args.email and not args.code:
        if existing and existing_token:
            import time as _time
            expires_at = config.get("verified_email_token_expires_at", 0)
            expired = not isinstance(expires_at, (int, float)) or _time.time() >= expires_at
            status = "expired" if expired else "verified"
            info: dict = {"verified_email": existing, "status": status}
            if expired:
                info["hint"] = f"Run `clawjournal verify-email {existing}` to get a fresh upload token."
            print(json.dumps(info, indent=2))
            if expired:
                sys.exit(1)
        elif existing:
            print(json.dumps({
                "error": "No active upload token. Re-verify your email before sharing.",
                "hint": "Run `clawjournal verify-email <your-email@university.edu>` again.",
            }, indent=2))
            sys.exit(1)
        else:
            print(json.dumps({
                "error": "No verified email. Usage: clawjournal verify-email <your-email@university.edu>",
                "hint": "Only .edu email addresses are accepted.",
            }, indent=2))
            sys.exit(1)
        return

    email = args.email
    if not email:
        print(json.dumps({"error": "Email address required."}, indent=2))
        sys.exit(1)

    if not _is_edu_email(email):
        print(json.dumps({
            "error": f"'{email}' is not a .edu email address.",
            "hint": "Only .edu email addresses are accepted for data sharing.",
        }, indent=2))
        sys.exit(1)

    if args.code:
        # Step 2: Confirm with verification code
        try:
            result = confirm_email_verification(email, args.code)
        except (OSError, ValueError) as e:
            print(json.dumps({"error": f"Verification failed: {e}"}, indent=2))
            sys.exit(1)

        if result.get("verified"):
            expires_at = result.get("upload_token_expires_at", 0)
            print(json.dumps({
                "verified_email": email.strip().lower(),
                "status": "verified",
                "message": "Email verified! Upload token valid for one upload within 1 hour.",
                "upload_token_expires_at": expires_at,
            }, indent=2))
        else:
            print(json.dumps({
                "error": result.get("error", "Verification failed."),
                "hint": "Check the code and try again, or request a new code.",
            }, indent=2))
            sys.exit(1)
    else:
        # Step 1: Request verification
        try:
            request_email_verification(email)
        except (OSError, ValueError) as e:
            print(json.dumps({"error": f"Request failed: {e}"}, indent=2))
            sys.exit(1)

        print(json.dumps({
            "status": "verification_sent",
            "email": email.strip().lower(),
            "message": "If the ingest service is configured correctly, you will receive a verification code by email.",
            "next_command": f"clawjournal verify-email {email} --code <CODE>",
        }, indent=2))


def _run_bundle_share(args) -> None:
    """Share a bundle via the ingest service."""
    from .workbench.daemon import ensure_share_upload_ready, upload_share
    from .workbench.index import get_effective_share_settings, open_index

    config = load_config()

    conn = open_index()
    try:
        settings = get_effective_share_settings(conn, config)
        share_id = _resolve_share_id(conn, args.share_id)
        if share_id is None:
            print(f"Bundle not found: {args.share_id}")
            sys.exit(1)

        ensure_share_upload_ready()

        pii_status = _print_share_pii_warning(output_json=getattr(args, "json", False))
        result = upload_share(
            conn,
            share_id,
            force=args.force,
            custom_strings=settings["custom_strings"],
            extra_usernames=settings["extra_usernames"],
            excluded_projects=settings["excluded_projects"],
            blocked_domains=settings["blocked_domains"],
            allowlist_entries=settings["allowlist_entries"],
        )
        if result.get("ok"):
            if getattr(args, "json", False):
                result["share_id"] = share_id
                result["bundle_id"] = share_id
                result.pop("status", None)
                result.pop("gcs_uri", None)
                result["pii_status"] = pii_status
                print(json.dumps(result, indent=2))
            else:
                count = result.get("session_count", "?")
                print(f"Bundle {share_id[:8]} uploaded successfully. {count} sessions shared.")
                redaction_summary = result.get("redaction_summary")
                if redaction_summary is not None:
                    print(f"Privacy: {_format_redaction_summary(redaction_summary)}")
        else:
            print(result.get("error", "Share failed."))
            sys.exit(1)
    except Exception as exc:
        print(f"Share failed: {exc}")
        sys.exit(1)
    finally:
        conn.close()


def _run_share(args) -> None:
    """One-step: create bundle + export + share. With --preview, show bundle contents."""
    from .workbench.daemon import ensure_share_upload_ready
    from .workbench.index import (
        get_effective_share_settings,
        open_index,
        query_sessions,
        session_matches_excluded_projects,
    )

    config = load_config()

    conn = open_index()
    try:
        settings = get_effective_share_settings(conn, config)
        session_ids = list(args.session_ids) if args.session_ids else []

        # Query once, reuse for both ID collection and preview
        if args.status and not session_ids:
            session_rows = query_sessions(conn, status=args.status, limit=10000)
            session_ids = [s["session_id"] for s in session_rows]
        elif session_ids:
            placeholders = ", ".join("?" for _ in session_ids)
            rows = conn.execute(
                f"SELECT * FROM sessions WHERE session_id IN ({placeholders})",
                session_ids,
            ).fetchall()
            session_rows = [dict(r) for r in rows]
        else:
            session_rows = []

        session_rows = [
            session for session in session_rows
            if not session_matches_excluded_projects(session, settings["excluded_projects"])
        ]

        if not session_ids or not session_rows:
            print("No sessions to share. Provide session IDs or use --status approved.")
            sys.exit(1)

        # Use found IDs (some provided IDs may not exist in DB)
        session_ids = [s["session_id"] for s in session_rows]

        if getattr(args, "preview", False):
            pii_status = _print_share_pii_warning(output_json=getattr(args, "json", False))
            if getattr(args, "json", False):
                payload = _share_preview(session_rows, output_json=True)
                payload["pii_status"] = pii_status
                print(json.dumps(payload, indent=2))
            else:
                _share_preview(session_rows, output_json=False)
            return

        ensure_share_upload_ready()

        # PII redaction is now mandatory inside upload_share() itself
        from .workbench.daemon import upload_share
        from .workbench.index import create_share

        pii_status = _print_share_pii_warning(output_json=getattr(args, "json", False))
        share_id = create_share(conn, session_ids, note=args.note)
        result = upload_share(
            conn,
            share_id,
            force=args.force,
            custom_strings=settings["custom_strings"],
            extra_usernames=settings["extra_usernames"],
            excluded_projects=settings["excluded_projects"],
            blocked_domains=settings["blocked_domains"],
            allowlist_entries=settings["allowlist_entries"],
        )
        if result.get("ok"):
            if getattr(args, "json", False):
                result["share_id"] = share_id
                result["bundle_id"] = share_id
                result["pii_status"] = pii_status
                result.pop("status", None)
                result.pop("gcs_uri", None)
                print(json.dumps(result, indent=2))
            else:
                count = result.get("session_count", len(session_ids))
                print(f"Shared {count} sessions.")
                print(f"Bundle {share_id[:8]} uploaded successfully.")
                redaction_summary = result.get("redaction_summary")
                if redaction_summary is not None:
                    print(f"Privacy: {_format_redaction_summary(redaction_summary)}")
        else:
            print(result.get("error", "Share failed."))
            sys.exit(1)
    except Exception as exc:
        print(f"Share failed: {exc}")
        sys.exit(1)
    finally:
        conn.close()


def _parse_badges(raw) -> list[str]:
    """Parse a badge field that may be a JSON string or already a list."""
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            pass
    return []


def _share_preview(sessions: list[dict], *, output_json: bool = False, limit: int = 10) -> dict[str, Any] | None:
    """Show a preview of the trace bundle contents that would be shared."""
    total = len(sessions)

    if output_json:
        items = []
        for i, s in enumerate(sessions, 1):
            items.append({
                "index": i,
                "session_id": s.get("session_id"),
                "display_title": s.get("display_title", ""),
                "source": s.get("source", ""),
                "model": s.get("model"),
                "ai_quality_score": s.get("ai_quality_score"),
                "ai_failure_value_score": s.get("ai_failure_value_score"),
                "ai_failure_attribution": s.get("ai_failure_attribution"),
                "ai_recovery_labels": _parse_badges(s.get("ai_recovery_labels", [])),
                "ai_failure_modes": _parse_badges(s.get("ai_failure_modes", [])),
                "ai_score_reason": s.get("ai_score_reason"),
                "outcome_badge": s.get("outcome_badge"),
                "risk_badges": _parse_badges(s.get("risk_badges", [])),
                "review_status": s.get("review_status", "new"),
            })
        return {
            "sessions": items,
            "total": total,
            "upload_auth": {
                "required": True,
                "note": "Upload sends the bundle separately from your verified .edu email authentication data.",
            },
        }

    print(f"{total} sessions ready to share. Showing top {min(limit, total)}:\n")
    print(f"  {'#':>3}  {'Fail':>5}  {'Prod':>5}  Title")
    print(f"  {'':>3}  {'':>5}  Summary")
    print("  " + "-" * 70)

    for i, s in enumerate(sessions[:limit], 1):
        title = (s.get("display_title") or "")[:60]
        score = s.get("ai_quality_score")
        failure_score = s.get("ai_failure_value_score")
        score_str = str(score) if score is not None else "-"
        failure_str = str(failure_score) if failure_score is not None else "-"
        reason = s.get("ai_score_reason") or ""
        outcome = s.get("outcome_badge") or ""

        risk_badges = _parse_badges(s.get("risk_badges", []))
        risk_str = f" [{', '.join(risk_badges)}]" if risk_badges else ""

        print(f"  {i:>3}    {failure_str:>1}      {score_str:>1}    {title}")
        if reason:
            summary = reason[:65]
            print(f"  {'':>3}  {'':>5}    {summary}{risk_str}")
        elif outcome:
            print(f"  {'':>3}  {'':>5}    ({outcome}){risk_str}")

    if total > limit:
        print(f"\n  ... and {total - limit} more sessions.")
    print("\nNote: upload authentication is separate from this preview and uses your verified .edu email plus a short-lived upload token.")
    return None


def _run_insights(args) -> None:
    """Token efficiency advisor — analyze usage patterns."""
    from .workbench.index import open_index
    from .scoring.insights import collect_advisor_stats, generate_recommendations
    from .pricing import format_cost

    conn = open_index()
    try:
        stats = collect_advisor_stats(conn, days=args.days)
        advisor = generate_recommendations(stats)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(advisor, indent=2))
        return

    print()
    print("=" * 56)
    print("  Token Efficiency Advisor")
    print(f"  {stats['period']}")
    print("=" * 56)
    print()
    print(f"  {advisor['headline']}")
    print()

    summary = advisor.get("summary_stats", {})
    if summary:
        print(f"  API equivalent: {format_cost(summary.get('total_cost_usd', 0))} "
              f"across {summary.get('total_sessions', 0)} sessions "
              f"({format_cost(summary.get('cost_per_session', 0))}/session)")
        if summary.get("most_efficient_model"):
            print(f"  Most efficient: {summary['most_efficient_model']}")
        if summary.get("highest_quality_model"):
            print(f"  Highest productivity: {summary['highest_quality_model']}")
        savings = summary.get("potential_savings_usd", 0)
        if savings > 0:
            print(f"  Potential savings: {format_cost(savings)}/period")
        print()

    recs = advisor.get("recommendations", [])
    if recs:
        priority_icons = {"high": "!", "medium": ">", "low": "-"}
        for rec in recs:
            icon = priority_icons.get(rec.get("priority", "low"), "-")
            print(f"  {icon} {rec['priority'].upper()}: {rec['title']}")
            if args.detail and rec.get("detail"):
                # Wrap detail text
                detail = rec["detail"]
                for i in range(0, len(detail), 52):
                    print(f"    {detail[i:i+52]}")
            print()
    else:
        print("  No specific optimization suggestions.\n")


def _run_search(args) -> None:
    """Full-text search across sessions."""
    from .workbench.index import open_index, search_fts

    conn = open_index()
    try:
        results = search_fts(conn, args.query, limit=args.limit)

        # Filter by source if specified
        if args.source:
            results = [s for s in results if s.get("source") == args.source]

        if args.json:
            items = []
            for i, s in enumerate(results, 1):
                items.append({
                    "index": i,
                    "session_id": s.get("session_id"),
                    "display_title": s.get("display_title", ""),
                    "source": s.get("source", ""),
                    "model": s.get("model"),
                    "project": s.get("project", ""),
                    "review_status": s.get("review_status", "new"),
                    "outcome_badge": s.get("outcome_badge"),
                })
            print(json.dumps({"query": args.query, "results": items, "total": len(items)}, indent=2))
        else:
            if not results:
                print(f"No results for '{args.query}'.")
                return
            print(f"{'Source':<10} {'Status':<12} {'Model':<25}  Title")
            print("-" * 90)
            for s in results:
                title = (s.get("display_title") or "")[:45]
                model = (s.get("model") or "")[:24]
                print(f"{s.get('source', ''):<10} {s.get('review_status', 'new'):<12} {model:<25}  {title}")
            print(f"\n{len(results)} results.")
    finally:
        conn.close()


def _truncate(text: str, max_len: int = 80) -> str:
    """Truncate text to max_len, appending '...' if shortened."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_len:
        return text
    return text[:max_len - 3] + "..."


def _format_duration(seconds: int | None) -> str:
    """Format duration in seconds to a human-readable string like '12m' or '1h 5m'."""
    if seconds is None:
        return "?"
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    remaining = minutes % 60
    if remaining:
        return f"{hours}h {remaining}m"
    return f"{hours}h"


def _format_tokens(count: int) -> str:
    """Format token count to compact form like '15.2k'."""
    if count < 1000:
        return str(count)
    if count < 10000:
        return f"{count / 1000:.1f}k"
    return f"{count // 1000}k"


def _get_message_text(msg: dict) -> str:
    """Extract text content from a message dict."""
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, str):
                return block
            if isinstance(block, dict) and block.get("text"):
                return block["text"]
    return ""


def _extract_tool_uses(msg: dict) -> list[dict]:
    """Extract tool uses from a message, handling both parsed and raw formats."""
    tool_uses = msg.get("tool_uses", [])
    if tool_uses:
        return tool_uses
    # Check content blocks for tool use
    content = msg.get("content")
    if isinstance(content, list):
        uses = []
        for block in content:
            if isinstance(block, dict) and block.get("tool"):
                inp = block.get("input", {})
                first_arg = ""
                if isinstance(inp, dict):
                    for v in inp.values():
                        if isinstance(v, str) and v.strip():
                            first_arg = v.strip()
                            break
                uses.append({
                    "tool": block["tool"],
                    "input": inp,
                    "output": block.get("output", ""),
                    "status": block.get("status", ""),
                    "first_arg": first_arg,
                })
        return uses
    return []


def _run_score_view(args) -> None:
    """Show condensed session view for AI scoring."""
    from .scoring.overrides import failure_evidence_from_detail
    from .workbench.index import get_session_detail, open_index, query_sessions

    conn = open_index()

    if args.batch:
        # Batch mode: load multiple sessions in compact format
        sessions = query_sessions(
            conn,
            source=args.source,
            limit=args.limit,
            offset=args.offset,
        )
        if not sessions:
            print("No sessions found.")
            conn.close()
            return

        total = len(sessions)
        for idx, s in enumerate(sessions, 1):
            sid = s["session_id"]
            detail = get_session_detail(conn, sid)
            if not detail:
                continue

            source = detail.get("source", "?")
            model = detail.get("model", "?")
            project = detail.get("project", "?")
            duration = _format_duration(detail.get("duration_seconds"))
            total_tokens = _format_tokens(
                (detail.get("input_tokens") or 0) + (detail.get("output_tokens") or 0)
            )
            task_type = detail.get("task_type", "unknown")
            outcome = detail.get("outcome_badge", "unknown")
            user_msgs = detail.get("user_messages", 0)
            asst_msgs = detail.get("assistant_messages", 0)

            # First user message
            first_msg = ""
            messages = detail.get("messages", [])
            for msg in messages:
                if msg.get("role") == "user":
                    first_msg = _get_message_text(msg)
                    break

            # Condensed flow
            flow_parts = []
            for msg in messages:
                role = msg.get("role", "")
                text = _get_message_text(msg)
                summary = _truncate(text, 30) if text else ""
                tool_uses = _extract_tool_uses(msg)
                tool_names = [t.get("tool", "") for t in tool_uses if t.get("tool")]
                if role == "user":
                    label = f"User→{summary}" if summary else "User"
                elif role == "assistant":
                    if tool_names:
                        tool_str = "+".join(tool_names[:3])
                        # Get output snippet from last tool
                        out_snippet = ""
                        if tool_uses:
                            last_out = tool_uses[-1].get("output", "")
                            if isinstance(last_out, str) and last_out.strip():
                                out_snippet = f"→{_truncate(last_out.strip(), 20)}"
                        label = f"Asst→{tool_str}({_truncate(summary, 15)}){out_snippet}"
                    else:
                        label = f"Asst→{summary}" if summary else "Asst"
                else:
                    continue
                flow_parts.append(label)

            flow_str = ", ".join(flow_parts) if flow_parts else "(empty)"

            # Files
            files = detail.get("files_touched", [])
            if isinstance(files, str):
                try:
                    files = json.loads(files)
                except (json.JSONDecodeError, ValueError):
                    files = []
            files_str = ", ".join(files) if files else "(none)"

            print(f"=== SESSION {idx}/{total}: {sid} ===")
            print(f"Source: {source} | Model: {model} | Project: {project} | {duration} | {total_tokens} tokens")
            print(f"Task: {task_type} | Outcome: {outcome} | {user_msgs} user + {asst_msgs} asst msgs")
            print(f"First msg: \"{_truncate(first_msg, 120)}\"")
            print(f"Flow: {flow_str}")
            print(f"Files: {files_str}")
            print()

        conn.close()
        return

    # Single session mode
    session_ids = args.session_ids or []
    if not session_ids:
        print(json.dumps({"error": "Provide a session_id or use --batch."}))
        conn.close()
        sys.exit(1)

    for sid in session_ids:
        detail = get_session_detail(conn, sid)
        if not detail:
            print(f"Session not found: {sid}")
            continue

        source = detail.get("source", "?")
        model = detail.get("model", "?")
        project = detail.get("project", "?")
        duration = _format_duration(detail.get("duration_seconds"))
        input_tok = _format_tokens(detail.get("input_tokens") or 0)
        output_tok = _format_tokens(detail.get("output_tokens") or 0)
        user_msgs = detail.get("user_messages", 0)
        asst_msgs = detail.get("assistant_messages", 0)
        task_type = detail.get("task_type", "unknown")
        outcome = detail.get("outcome_badge", "unknown")
        sensitivity = detail.get("sensitivity_score", 0.0)

        # Value/risk badges
        value_badges = detail.get("value_badges", [])
        if isinstance(value_badges, str):
            try:
                value_badges = json.loads(value_badges)
            except (json.JSONDecodeError, ValueError):
                value_badges = []
        risk_badges = detail.get("risk_badges", [])
        if isinstance(risk_badges, str):
            try:
                risk_badges = json.loads(risk_badges)
            except (json.JSONDecodeError, ValueError):
                risk_badges = []

        value_str = ", ".join(value_badges) if value_badges else "(none)"
        risk_str = ", ".join(risk_badges) if risk_badges else "(none)"
        failure_score = detail.get("ai_failure_value_score")
        quality_score = detail.get("ai_quality_score")
        failure_modes = detail.get("ai_failure_modes", [])
        if isinstance(failure_modes, str):
            try:
                failure_modes = json.loads(failure_modes)
            except (json.JSONDecodeError, ValueError):
                failure_modes = []
        recovery_labels = detail.get("ai_recovery_labels", [])
        if isinstance(recovery_labels, str):
            try:
                recovery_labels = json.loads(recovery_labels)
            except (json.JSONDecodeError, ValueError):
                recovery_labels = []
        failure_attribution = detail.get("ai_failure_attribution")
        learning_summary = detail.get("ai_learning_summary")
        score_reason = detail.get("ai_score_reason")
        failure_evidence = failure_evidence_from_detail(
            detail.get("ai_scoring_detail")
        )

        print(f"Session: {sid}")
        print(f"Source: {source} | Model: {model} | Project: {project}")
        print(f"Duration: {duration} | Tokens: {input_tok} in / {output_tok} out | Messages: {user_msgs} user / {asst_msgs} asst")
        print(f"Task type: {task_type} | Outcome: {outcome}")
        failure_display = f"{failure_score}/5" if failure_score else "(unscored)"
        productivity_display = f"{quality_score}/5" if quality_score else "(unscored)"
        print(f"Failure value: {failure_display} | Productivity: {productivity_display}")
        if failure_attribution:
            print(f"Attribution: {failure_attribution}")
        if failure_modes:
            print(f"Failure modes: {', '.join(failure_modes)}")
        if recovery_labels:
            print(f"Recovery: {', '.join(recovery_labels)}")
        if learning_summary:
            print(f"Lesson: {learning_summary}")
        elif score_reason:
            print(f"Reason: {score_reason}")
        if failure_evidence:
            print("Evidence:")
            for snippet in failure_evidence[:3]:
                print(f"- {_truncate(snippet, 160)}")
        print(f"Value: {value_str} | Risk: {risk_str} | Sensitivity: {sensitivity}")
        print()

        messages = detail.get("messages", [])

        # First user message
        first_user_text = ""
        for msg in messages:
            if msg.get("role") == "user":
                first_user_text = _get_message_text(msg)
                break

        print("--- FIRST USER MESSAGE ---")
        print(_truncate(first_user_text, 500))
        print()

        # Conversation flow
        print("--- CONVERSATION FLOW ---")
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role == "user":
                role_label = "User"
            elif role == "assistant":
                role_label = "Asst"
            else:
                continue

            text = _get_message_text(msg)
            print(f"#{i} [{role_label}] {_truncate(text, 80)}")

            tool_uses = _extract_tool_uses(msg)
            for tu in tool_uses:
                tool_name = tu.get("tool", "?")
                inp = tu.get("input", {})
                first_arg = tu.get("first_arg", "")
                if not first_arg and isinstance(inp, dict):
                    for v in inp.values():
                        if isinstance(v, str) and v.strip():
                            first_arg = v.strip()
                            break
                first_arg_str = f"({_truncate(first_arg, 30)})" if first_arg else "()"
                status_str = tu.get("status", "")
                output = tu.get("output", "")
                output_str = ""
                if isinstance(output, str) and output.strip():
                    output_str = f" — \"{_truncate(output.strip(), 40)}\""
                status_display = f" {status_str}" if status_str else ""
                print(f"   → {tool_name}{first_arg_str}{status_display}{output_str}")
        print()

        # Files touched
        files = detail.get("files_touched", [])
        if isinstance(files, str):
            try:
                files = json.loads(files)
            except (json.JSONDecodeError, ValueError):
                files = []
        print("--- FILES TOUCHED ---")
        print(", ".join(files) if files else "(none)")
        print()

        # Commands run
        commands = detail.get("commands_run", [])
        if isinstance(commands, str):
            try:
                commands = json.loads(commands)
            except (json.JSONDecodeError, ValueError):
                commands = []
        print("--- COMMANDS RUN ---")
        if commands:
            for cmd in commands:
                print(cmd)
        else:
            print("(none)")
        print()

    conn.close()


def _run_set_score(args) -> None:
    """Record manual score overrides for one or more sessions."""
    from .scoring.overrides import (
        failure_evidence_from_detail,
        merge_failure_evidence,
        normalize_failure_evidence,
        requires_failure_evidence,
    )
    from .workbench.index import get_session_detail, open_index, update_session

    session_ids = args.session_ids
    quality = args.quality
    failure_value = args.failure_value
    reason = args.reason
    failure_evidence = normalize_failure_evidence(
        getattr(args, "failure_evidence", None)
    )

    if not session_ids:
        print(json.dumps({"error": "No session IDs provided."}))
        sys.exit(1)
    if quality is None and failure_value is None:
        print(json.dumps({"error": "Provide --failure-value, --quality, or both."}))
        sys.exit(1)
    if failure_evidence and failure_value is None:
        print(json.dumps({"error": "Provide --failure-value with --failure-evidence."}))
        sys.exit(1)

    conn = open_index()
    details: dict[str, dict[str, Any] | None] = {}
    if requires_failure_evidence(failure_value):
        missing_evidence: list[str] = []
        for sid in session_ids:
            detail = get_session_detail(conn, sid)
            details[sid] = detail
            if detail is None:
                continue
            existing_evidence = failure_evidence_from_detail(
                detail.get("ai_scoring_detail")
            )
            if not failure_evidence and not existing_evidence:
                missing_evidence.append(sid)
        if missing_evidence:
            conn.close()
            print(json.dumps({
                "error": "Failure-value 4-5 overrides require --failure-evidence.",
                "session_ids": missing_evidence,
            }))
            sys.exit(1)

    results = []
    for sid in session_ids:
        detail = details.get(sid)
        if failure_evidence and sid not in details:
            detail = get_session_detail(conn, sid)
        updates: dict[str, Any] = {}
        result: dict[str, Any] = {"session_id": sid}
        if quality is not None:
            updates["ai_quality_score"] = quality
            result["ai_quality_score"] = quality
        if failure_value is not None:
            updates["ai_failure_value_score"] = failure_value
            result["ai_failure_value_score"] = failure_value
        if reason is not None:
            updates["ai_score_reason"] = reason
        if failure_evidence:
            updates["ai_scoring_detail"] = merge_failure_evidence(
                detail.get("ai_scoring_detail") if detail else None,
                failure_evidence,
            )
            result["ai_failure_evidence"] = failure_evidence
        ok = update_session(
            conn, sid,
            **updates,
        )
        result["ok"] = ok
        results.append(result)
    conn.close()

    success = sum(1 for r in results if r["ok"])
    payload: dict[str, Any] = {
        "action": "set-score",
        "updated": success,
        "results": results,
    }
    if failure_value is not None:
        payload["failure_value"] = failure_value
    if failure_evidence:
        payload["failure_evidence"] = failure_evidence
    if quality is not None:
        payload["quality"] = quality
    print(json.dumps(payload, indent=2))


def _run_score_batch(args) -> None:
    """List unscored sessions as JSON."""
    from .workbench.index import open_index, query_unscored_sessions

    conn = open_index()
    source_filter = _normalize_score_source_filter(args.source)
    sessions = query_unscored_sessions(conn, limit=args.limit, source=source_filter)
    conn.close()

    print(json.dumps(sessions, indent=2))


def _generate_score_view_text(conn, session_id: str) -> str | None:
    """Generate score-view text for a session as a compact scoring prompt."""
    from .workbench.index import get_session_detail

    detail = get_session_detail(conn, session_id)
    if not detail:
        return None

    lines: list[str] = []

    source = detail.get("source", "?")
    model = detail.get("model", "?")
    project = detail.get("project", "?")
    duration = _format_duration(detail.get("duration_seconds"))
    input_tok = _format_tokens(detail.get("input_tokens") or 0)
    output_tok = _format_tokens(detail.get("output_tokens") or 0)
    user_msgs = detail.get("user_messages", 0)
    asst_msgs = detail.get("assistant_messages", 0)
    task_type = detail.get("task_type", "unknown")
    outcome = detail.get("outcome_badge", "unknown")
    sensitivity = detail.get("sensitivity_score", 0.0)

    value_badges = detail.get("value_badges", [])
    if isinstance(value_badges, str):
        try:
            value_badges = json.loads(value_badges)
        except (json.JSONDecodeError, ValueError):
            value_badges = []
    risk_badges = detail.get("risk_badges", [])
    if isinstance(risk_badges, str):
        try:
            risk_badges = json.loads(risk_badges)
        except (json.JSONDecodeError, ValueError):
            risk_badges = []

    value_str = ", ".join(value_badges) if value_badges else "(none)"
    risk_str = ", ".join(risk_badges) if risk_badges else "(none)"

    lines.append(f"Session: {session_id}")
    lines.append(f"Source: {source} | Model: {model} | Project: {project}")
    lines.append(f"Duration: {duration} | Tokens: {input_tok} in / {output_tok} out | Messages: {user_msgs} user / {asst_msgs} asst")
    lines.append(f"Task type: {task_type} | Outcome: {outcome}")
    lines.append(f"Value: {value_str} | Risk: {risk_str} | Sensitivity: {sensitivity}")
    lines.append("")

    messages = detail.get("messages", [])

    # First user message
    first_user_text = ""
    for msg in messages:
        if msg.get("role") == "user":
            first_user_text = _get_message_text(msg)
            break

    lines.append("--- FIRST USER MESSAGE ---")
    lines.append(_truncate(first_user_text, 500))
    lines.append("")

    # Conversation flow
    lines.append("--- CONVERSATION FLOW ---")
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        if role == "user":
            role_label = "User"
        elif role == "assistant":
            role_label = "Asst"
        else:
            continue

        text = _get_message_text(msg)
        lines.append(f"#{i} [{role_label}] {_truncate(text, 80)}")

        tool_uses = _extract_tool_uses(msg)
        for tu in tool_uses:
            tool_name = tu.get("tool", "?")
            inp = tu.get("input", {})
            first_arg = tu.get("first_arg", "")
            if not first_arg and isinstance(inp, dict):
                for v in inp.values():
                    if isinstance(v, str) and v.strip():
                        first_arg = v.strip()
                        break
            first_arg_str = f"({_truncate(first_arg, 30)})" if first_arg else "()"
            status_str = tu.get("status", "")
            output = tu.get("output", "")
            output_str = ""
            if isinstance(output, str) and output.strip():
                output_str = f' — "{_truncate(output.strip(), 40)}"'
            status_display = f" {status_str}" if status_str else ""
            lines.append(f"   → {tool_name}{first_arg_str}{status_display}{output_str}")
    lines.append("")

    # Files touched
    files = detail.get("files_touched", [])
    if isinstance(files, str):
        try:
            files = json.loads(files)
        except (json.JSONDecodeError, ValueError):
            files = []
    lines.append("--- FILES TOUCHED ---")
    lines.append(", ".join(files) if files else "(none)")
    lines.append("")

    # Commands run
    commands = detail.get("commands_run", [])
    if isinstance(commands, str):
        try:
            commands = json.loads(commands)
        except (json.JSONDecodeError, ValueError):
            commands = []
    lines.append("--- COMMANDS RUN ---")
    if commands:
        for cmd in commands:
            lines.append(cmd)
    else:
        lines.append("(none)")
    lines.append("")

    return "\n".join(lines)


def _score_single_session(
    conn,
    session_id: str,
    *,
    model: str | None = None,
    backend: str = "auto",
    dry_run: bool = False,
) -> dict[str, Any]:
    """Score a single session using the structured pipeline. Returns result dict."""
    from .workbench.index import get_session_detail, update_session
    from .scoring.scoring import (
        compute_basic_metrics,
        format_session_for_judge,
        score_session,
        segment_session,
    )

    if dry_run:
        detail = get_session_detail(conn, session_id)
        if not detail:
            return {"session_id": session_id, "error": "Session not found"}
        segments = segment_session(detail.get("messages", []))
        metrics = compute_basic_metrics(segments, detail)
        from .scoring.scoring import _extract_task_context
        task_context = _extract_task_context(detail.get("messages", []))
        prompt = format_session_for_judge(segments, task_context, metrics)
        print(prompt, file=sys.stderr)
        return {
            "session_id": session_id,
            "dry_run": True,
            "segments": len(segments),
            "total_steps": metrics["total_steps"],
        }

    try:
        result = score_session(conn, session_id, model=model, backend=backend)
    except RuntimeError as e:
        return {
            "session_id": session_id,
            "error": f"Judge failed: {e}",
        }

    ok = update_session(
        conn, session_id,
        ai_quality_score=result.quality,
        ai_score_reason=result.reason,
        ai_scoring_detail=result.detail_json,
        ai_task_type=result.task_type,
        ai_outcome_badge=result.outcome_label or None,
        ai_value_badges=json.dumps(result.value_labels),
        ai_risk_badges=json.dumps(result.risk_level),
        ai_display_title=result.display_title or None,
        ai_effort_estimate=result.effort_estimate,
        ai_summary=result.summary or None,
        ai_failure_value_score=getattr(result, "failure_value_score", None),
        ai_recovery_labels=json.dumps(getattr(result, "recovery_labels", [])),
        ai_failure_attribution=getattr(result, "failure_attribution", "") or None,
        ai_failure_modes=json.dumps(getattr(result, "failure_modes", [])),
        ai_learning_summary=getattr(result, "learning_summary", "") or None,
        ai_scorer_backend=getattr(result, "scorer_backend", "") or None,
        ai_scorer_model=getattr(result, "scorer_model", "") or None,
        ai_rubric_git_sha=getattr(result, "rubric_git_sha", "") or None,
        ai_scored_at=getattr(result, "scored_at", "") or None,
    )

    return {
        "session_id": session_id,
        "ai_quality_score": result.quality,
        "ai_failure_value_score": getattr(result, "failure_value_score", None),
        "ai_recovery_labels": getattr(result, "recovery_labels", []),
        "ai_failure_attribution": getattr(result, "failure_attribution", ""),
        "ai_failure_modes": getattr(result, "failure_modes", []),
        "ai_learning_summary": getattr(result, "learning_summary", ""),
        "reason": result.reason,
        "task_type": result.task_type,
        "resolution": result.outcome_label,
        "session_tags": result.value_labels,
        "privacy_flags": result.risk_level,
        "effort_estimate": result.effort_estimate,
        "summary": result.summary,
        "ok": ok,
    }


def _is_auto_triage_noise(result: dict[str, Any]) -> bool:
    """Return True when auto-triage should block a scored noise session."""
    failure_value = result.get("ai_failure_value_score")
    return (
        result.get("ai_quality_score") == 1
        and isinstance(failure_value, int)
        and failure_value <= 2
    )


def _run_score(args) -> None:
    """Score sessions using the current agent's automation CLI or an explicit backend."""
    from .workbench.index import open_index, query_unscored_sessions, update_session

    conn = open_index()

    model = args.model
    backend = args.backend
    dry_run = args.dry_run
    batch = args.batch
    auto_triage = getattr(args, "auto_triage", False)
    source_filter = _normalize_score_source_filter(args.source)

    if batch:
        # Batch mode: score unscored sessions
        sessions = query_unscored_sessions(conn, limit=args.limit, source=source_filter)
        if not sessions:
            print(json.dumps({"message": "No unscored sessions found.", "scored": 0}))
            conn.close()
            return

        results = []
        for i, s in enumerate(sessions, 1):
            sid = s["session_id"]
            title = s.get("display_title", sid)
            print(f"[{i}/{len(sessions)}] Scoring: {_truncate(title, 60)} ({sid[:12]}...)", file=sys.stderr)
            result = _score_single_session(conn, sid, model=model, backend=backend, dry_run=dry_run)
            results.append(result)
            if result.get("ai_quality_score"):
                failure = result.get("ai_failure_value_score")
                failure_prefix = f"failure {failure}/5; " if failure is not None else ""
                print(
                    f"  -> {failure_prefix}productivity {result['ai_quality_score']}/5: "
                    f"{result.get('reason', '')[:100]}",
                    file=sys.stderr,
                )
            elif result.get("error"):
                print(f"  -> Error: {result['error']}", file=sys.stderr)
            elif result.get("dry_run"):
                print(f"  -> (dry run)", file=sys.stderr)

        scored = [r for r in results if r.get("ok")]
        errors = [r for r in results if r.get("error")]
        summary = {
            "scored": len(scored),
            "errors": len(errors),
            "results": results,
        }
        if scored:
            scores = [r["ai_quality_score"] for r in scored]
            failure_scores = [
                r["ai_failure_value_score"]
                for r in scored
                if r.get("ai_failure_value_score") is not None
            ]
            summary["score_distribution"] = {
                "excellent_5": sum(1 for q in scores if q == 5),
                "good_4": sum(1 for q in scores if q == 4),
                "average_3": sum(1 for q in scores if q == 3),
                "low_2": sum(1 for q in scores if q == 2),
                "poor_1": sum(1 for q in scores if q == 1),
            }
            if failure_scores:
                summary["failure_value_distribution"] = {
                    "high_5": sum(1 for q in failure_scores if q == 5),
                    "valuable_4": sum(1 for q in failure_scores if q == 4),
                    "some_signal_3": sum(1 for q in failure_scores if q == 3),
                    "weak_2": sum(1 for q in failure_scores if q == 2),
                    "none_1": sum(1 for q in failure_scores if q == 1),
                }

        # Auto-triage: archive productivity noise, but keep high failure-value
        # traces visible even when the legacy productivity score is low.
        if auto_triage and scored and not dry_run:
            noise_ids = [
                r["session_id"]
                for r in scored
                if _is_auto_triage_noise(r)
            ]
            triage = {"archived": 0, "visible": 0}
            if noise_ids:
                for sid in noise_ids:
                    update_session(
                        conn, sid,
                        status="blocked",
                        reason="Auto-triage: noise session (productivity 1, failure value 1-2)",
                    )
                triage["archived"] = len(noise_ids)
                print(
                    f"  Auto-archived {len(noise_ids)} noise sessions "
                    "(productivity 1, failure value 1-2)",
                    file=sys.stderr,
                )
            triage["visible"] = len(scored) - len(noise_ids)
            summary["auto_triage"] = triage

        print(json.dumps(summary, indent=2))
    else:
        # Single session mode
        session_ids = args.session_ids or []
        if not session_ids:
            print(json.dumps({"error": "Provide session ID(s) or use --batch"}))
            conn.close()
            sys.exit(1)

        results = []
        for sid in session_ids:
            result = _score_single_session(conn, sid, model=model, backend=backend, dry_run=dry_run)
            results.append(result)

        if len(results) == 1:
            print(json.dumps(results[0], indent=2))
        else:
            print(json.dumps({"results": results}, indent=2))

    conn.close()


def _parse_window_days(value: str) -> int:
    """Parse a window string like ``7d`` / ``14`` / ``30d`` into integer days."""
    if value is None:
        raise argparse.ArgumentTypeError("--window value is required")
    text = str(value).strip().lower()
    if text.endswith("d"):
        text = text[:-1]
    try:
        days = int(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"invalid --window value: {value!r}. Use e.g. '7d', '14d', '30d'."
        ) from exc
    if days <= 0 or days > 365:
        raise argparse.ArgumentTypeError(
            f"--window must be between 1 and 365 days (got {days})."
        )
    return days


def _run_rescore(args) -> None:
    """Rescore recent in-scope sessions, ignoring existing scores."""
    from .workbench.index import open_index, query_sessions_for_rescore

    conn = open_index()

    model = args.model
    backend = args.backend
    dry_run = args.dry_run
    window_days = args.window
    source_filter = _normalize_score_source_filter(args.source)

    sessions = query_sessions_for_rescore(
        conn,
        window_days=window_days,
        source=source_filter,
        limit=args.limit,
    )
    if not sessions:
        print(json.dumps({
            "message": f"No sessions in the last {window_days} days for the selected source scope.",
            "scored": 0,
        }))
        conn.close()
        return

    label = "all sources" if source_filter is None else ",".join(source_filter)
    print(
        f"Rescoring {len(sessions)} session(s) from the last {window_days} days "
        f"(source: {label})",
        file=sys.stderr,
    )

    results = []
    for i, s in enumerate(sessions, 1):
        sid = s["session_id"]
        title = s.get("display_title", sid)
        print(
            f"[{i}/{len(sessions)}] Rescoring: {_truncate(title, 60)} ({sid[:12]}...)",
            file=sys.stderr,
        )
        result = _score_single_session(conn, sid, model=model, backend=backend, dry_run=dry_run)
        results.append(result)
        if result.get("ai_quality_score"):
            failure = result.get("ai_failure_value_score")
            failure_prefix = f"failure {failure}/5; " if failure is not None else ""
            print(
                f"  -> {failure_prefix}productivity {result['ai_quality_score']}/5: "
                f"{result.get('reason', '')[:100]}",
                file=sys.stderr,
            )
        elif result.get("error"):
            print(f"  -> Error: {result['error']}", file=sys.stderr)
        elif result.get("dry_run"):
            print(f"  -> (dry run)", file=sys.stderr)

    scored = [r for r in results if r.get("ok")]
    errors = [r for r in results if r.get("error")]
    summary = {
        "rescored": len(scored),
        "errors": len(errors),
        "window_days": window_days,
        "results": results,
    }
    if scored:
        failure_scores = [
            r["ai_failure_value_score"]
            for r in scored
            if r.get("ai_failure_value_score") is not None
        ]
        if failure_scores:
            summary["failure_value_distribution"] = {
                "high_5": sum(1 for q in failure_scores if q == 5),
                "valuable_4": sum(1 for q in failure_scores if q == 4),
                "some_signal_3": sum(1 for q in failure_scores if q == 3),
                "weak_2": sum(1 for q in failure_scores if q == 2),
                "none_1": sum(1 for q in failure_scores if q == 1),
            }

    print(json.dumps(summary, indent=2))
    conn.close()


def _resolve_since(since: str | None) -> str | None:
    """Convert --since value to an ISO date string for filtering.

    Uses local time so 'today' matches the user's actual day.
    """
    if not since:
        return None
    lower = since.lower().strip()
    from datetime import timedelta
    now = datetime.now()  # local time
    if lower == "today":
        return now.strftime("%Y-%m-%d")
    if lower == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    if lower in ("this week", "week"):
        monday = now - timedelta(days=now.weekday())
        return monday.strftime("%Y-%m-%d")
    # Assume ISO date
    return since


def _parse_time_gap(value: str) -> int:
    """Parse a time gap string like '30m' or '1h' into minutes."""
    value = value.strip().lower()
    try:
        if value.endswith("h"):
            return int(value[:-1]) * 60
        if value.endswith("m"):
            return int(value[:-1])
        return int(value)
    except ValueError:
        print(f"Invalid time gap: '{value}'. Use format like '30m' or '1h'. Defaulting to 30m.")
        return 30


def _run_segment(args: argparse.Namespace) -> None:
    """Segment multi-task OpenClaw sessions into child traces."""
    from .workbench.index import get_session_detail, open_index, query_sessions, update_session, upsert_sessions
    from .parsing.segmenter import segment_openclaw_session

    threshold_minutes = _parse_time_gap(getattr(args, "time_gap", "30m"))
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)
    output_json = getattr(args, "json", False)
    session_ids = getattr(args, "session_ids", [])

    conn = open_index()
    try:
        # Get sessions to segment
        if session_ids:
            sessions_to_segment = []
            for sid in session_ids:
                detail = get_session_detail(conn, sid)
                if not detail:
                    if not output_json:
                        print(f"Session not found: {sid}")
                elif detail.get("source") != "openclaw":
                    if not output_json:
                        print(f"Skipping non-OpenClaw session: {sid} (source={detail.get('source')})")
                else:
                    sessions_to_segment.append(detail)
        else:
            # Query all OpenClaw sessions that haven't been segmented
            candidates = query_sessions(
                conn, source="openclaw", limit=1000, sort="start_time", order="desc",
            )
            sessions_to_segment = []
            for s in candidates:
                # Skip child traces (already segmented)
                if s.get("parent_session_id"):
                    continue
                # Skip already-segmented parents unless --force
                if s.get("review_status") == "segmented" and not force:
                    continue
                detail = get_session_detail(conn, s["session_id"])
                if detail and detail.get("messages"):
                    sessions_to_segment.append(detail)

        segmented_count = 0
        skipped_count = 0
        total_children = 0
        results: list[dict] = []

        for session in sessions_to_segment:
            children = segment_openclaw_session(
                session, threshold_minutes=threshold_minutes,
            )

            # If only one child and it equals the original, no split needed
            if len(children) <= 1:
                skipped_count += 1
                continue

            segmented_count += 1
            total_children += len(children)

            result_entry = {
                "parent_session_id": session["session_id"],
                "parent_title": session.get("display_title", ""),
                "parent_messages": len(session.get("messages", [])),
                "segments": [],
            }

            for child in children:
                seg_info = {
                    "session_id": child["session_id"],
                    "title": child.get("segment_title", ""),
                    "reason": child.get("segment_reason", ""),
                    "message_range": child.get("segment_message_range", []),
                    "user_messages": child["stats"]["user_messages"],
                    "tool_uses": child["stats"]["tool_uses"],
                }
                result_entry["segments"].append(seg_info)

            results.append(result_entry)

            if not dry_run:
                # Store child traces
                upsert_sessions(conn, children)
                # Mark parent as segmented
                update_session(conn, session["session_id"], status="segmented")
    finally:
        conn.close()

    if output_json:
        print(json.dumps({
            "segmented": segmented_count,
            "skipped": skipped_count,
            "total_child_traces": total_children,
            "dry_run": dry_run,
            "results": results,
            "next_steps": ["clawjournal score --batch --source openclaw"] if not dry_run else [
                "clawjournal segment --source openclaw"
            ],
        }, indent=2))
    else:
        if dry_run:
            print("[dry-run] No changes written.\n")
        print(f"Segmented: {segmented_count} sessions → {total_children} child traces")
        print(f"Skipped:   {skipped_count} sessions (single-task or too short)")
        for r in results:
            print(f"\n  {r['parent_session_id'][:12]}... ({r['parent_messages']} msgs)")
            for seg in r["segments"]:
                msgs = seg["message_range"]
                print(f"    {seg['session_id'][-8:]:>10} │ {seg['title'][:50]:50} │ {seg['reason']}")


def _run_recent(args: argparse.Namespace) -> None:
    """Show recent sessions, auto-scanning if the index is stale."""
    from .workbench.daemon import Scanner
    from .workbench.index import open_index, query_sessions

    conn = open_index()
    auto_scanned = False

    # Auto-scan if stale: check most recent indexed_at timestamp
    existing = query_sessions(conn, source=args.source, limit=1,
                              sort="indexed_at", order="desc")
    stale = True
    if existing:
        last_indexed = existing[0].get("indexed_at", "")
        if last_indexed:
            try:
                indexed_dt = datetime.fromisoformat(last_indexed)
                age = (datetime.now(timezone.utc) - indexed_dt).total_seconds()
                stale = age > 300  # 5 minutes
            except (ValueError, TypeError):
                stale = True
    if stale:
        conn.close()
        scanner = Scanner(source_filter=args.source)
        scanner.scan_once()
        auto_scanned = True
        conn = open_index()

    # Query recent sessions
    # When --since is set, query more rows so the date filter doesn't cut us short
    since_date = _resolve_since(args.since)
    query_limit = args.limit * 10 if since_date else args.limit
    sessions = query_sessions(
        conn, source=args.source, limit=query_limit,
        sort="start_time", order="desc",
        exclude_segmented_parents=True,
    )
    conn.close()

    # Filter by --since if provided
    if since_date and sessions:
        # since_date is "YYYY-MM-DD"; start_time is ISO like "2026-03-24T10:00:00Z"
        # Compare only the date portion of start_time
        sessions = [
            s for s in sessions
            if (s.get("start_time") or "")[:10] >= since_date
        ]

    # Apply user's limit after filtering
    sessions = sessions[:args.limit]

    if args.json:
        items = []
        for i, s in enumerate(sessions, 1):
            items.append({
                "index": i,
                "session_id": s.get("session_id"),
                "display_title": s.get("display_title", ""),
                "source": s.get("source", ""),
                "model": s.get("model"),
                "duration_seconds": s.get("duration_seconds"),
                "ai_failure_value_score": s.get("ai_failure_value_score"),
                "ai_quality_score": s.get("ai_quality_score"),
                "outcome_badge": s.get("outcome_badge"),
                "start_time": s.get("start_time"),
                "user_messages": s.get("user_messages", 0),
                "assistant_messages": s.get("assistant_messages", 0),
                "tool_uses": s.get("tool_uses", 0),
                "input_tokens": s.get("input_tokens", 0),
                "output_tokens": s.get("output_tokens", 0),
            })
        print(json.dumps({
            "sessions": items,
            "auto_scanned": auto_scanned,
            "total_count": len(items),
            "next_steps": ["clawjournal card <session_id> --depth summary --json"],
        }, indent=2))
        return

    if not sessions:
        print("No recent sessions found. Try `clawjournal scan` first.")
        return

    # Human-readable list (one session per block for easy copy-paste of IDs)
    print()
    for i, s in enumerate(sessions, 1):
        title = s.get("display_title") or "Untitled"
        dur = s.get("duration_seconds")
        if dur and dur > 0:
            dur_str = f"{dur // 60} min" if dur >= 60 else f"{dur}s"
        else:
            dur_str = ""
        failure_score = s.get("ai_failure_value_score")
        score_str = f" · FV {failure_score}/5" if failure_score else ""
        outcome = s.get("outcome_badge") or ""
        sid = s.get("session_id", "")

        parts = [p for p in [dur_str, outcome] if p]
        if score_str:
            parts.append(score_str.lstrip(" · "))
        meta = " · ".join(parts)

        print(f"  {i}. {title}")
        if meta:
            print(f"     {meta}")
        print(f"     ID: {sid}")
        print()

    print("Use: clawjournal card <session_id> to generate a share card")


def _run_card(args: argparse.Namespace) -> None:
    """Generate share card(s) for the given session(s)."""
    from .workbench.card import generate_card
    from .workbench.index import get_session_detail, open_index
    from .redaction.secrets import redact_session

    conn = open_index()
    cards = []

    for sid in args.session_ids:
        session = get_session_detail(conn, sid)
        if session is None:
            print(json.dumps({"error": f"Session not found: {sid}"}))
            conn.close()
            sys.exit(1)

        # Apply redaction
        session_redacted, redact_count, _log = redact_session(session)
        session_redacted["_redaction_count"] = redact_count

        card_result = generate_card(session_redacted, depth=args.depth)
        cards.append(card_result)

    conn.close()

    if args.json:
        if len(cards) == 1:
            print(json.dumps(cards[0], indent=2))
        else:
            print(json.dumps({"cards": cards}, indent=2))
    else:
        for i, card_result in enumerate(cards):
            if i > 0:
                print("---")
            print(card_result["card_text"])


# ----------------------------------------------------------------------
# Trace note subcommands
# ----------------------------------------------------------------------

def _load_session_row(conn, session_id: str) -> dict | None:
    """Fetch a sessions row as a dict, without loading the blob."""
    row = conn.execute(
        "SELECT * FROM sessions WHERE session_id = ?", (session_id,)
    ).fetchone()
    return dict(row) if row is not None else None


def _has_unsynced_edits(path, reviewer_notes) -> bool:
    """Return True if the file's ## Notes differs from reviewer_notes under
    the canonical normalizer."""
    from .workbench.trace_note import (
        _normalize_notes,
        extract_trace_note_notes,
    )
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    on_disk = extract_trace_note_notes(text)
    if on_disk is None:
        # Malformed file (no ## Notes heading). Treat as unsynced so the
        # user has to pass --force — avoids silently fixing their file.
        return True
    return _normalize_notes(on_disk) != _normalize_notes(reviewer_notes)


def _render_one_note(conn, session_id: str, *, force: bool) -> tuple[str, str | None]:
    """Render a single session's note.

    Returns (status, message). status ∈ {'rendered', 'skipped', 'missing'}.
    """
    from .workbench.trace_note import (
        render_trace_note,
        trace_note_path,
        write_note_atomically,
    )

    session = _load_session_row(conn, session_id)
    if session is None:
        return ("missing", f"session not found: {session_id}")

    path = trace_note_path(session_id)
    reviewer_notes = session.get("reviewer_notes")
    if not force and _has_unsynced_edits(path, reviewer_notes):
        return (
            "skipped",
            f"unsynced edits in {path.name} — run `note sync {session_id}` first "
            "or pass --force",
        )

    text = render_trace_note(session, reviewer_notes)
    write_note_atomically(path, text)
    return ("rendered", str(path))


def _run_note(args: argparse.Namespace) -> None:
    import sys

    from .workbench.index import open_index
    from .workbench.trace_note import (
        NOTES_DIR,
        extract_rendered_updated_at,
        extract_trace_note_notes,
        render_trace_note,
        trace_note_path,
        write_note_atomically,
    )

    op = getattr(args, "note_op", None)

    if op == "path":
        print(trace_note_path(args.session_id))
        return

    if op == "render":
        modes = [bool(args.session_id), args.all_scored, args.stale]
        if sum(1 for m in modes if m) != 1:
            print(
                "error: exactly one of <session_id>, --all-scored, or --stale "
                "is required",
                file=sys.stderr,
            )
            sys.exit(2)

        conn = open_index()
        try:
            if args.session_id:
                status, message = _render_one_note(
                    conn, args.session_id, force=args.force
                )
                if status == "missing":
                    print(f"error: {message}", file=sys.stderr)
                    sys.exit(1)
                if status == "skipped":
                    print(f"skipped: {message}")
                    sys.exit(1)
                print(f"rendered: {message}")
                return

            if args.all_scored:
                rows = conn.execute(
                    "SELECT session_id FROM sessions "
                    "WHERE ai_summary IS NOT NULL AND ai_summary != '' "
                    "ORDER BY start_time DESC"
                ).fetchall()
                rendered = 0
                skipped = 0
                unchanged = 0
                for row in rows:
                    sid = row["session_id"]
                    path = trace_note_path(sid)
                    if path.exists() and not args.force:
                        unchanged += 1
                        continue
                    status, _ = _render_one_note(conn, sid, force=args.force)
                    if status == "rendered":
                        rendered += 1
                    elif status == "skipped":
                        skipped += 1
                print(
                    f"{rendered} rendered, {skipped} skipped (unsynced edits), "
                    f"{unchanged} unchanged"
                )
                return

            # --stale
            rendered = 0
            skipped = 0
            unchanged = 0
            NOTES_DIR.mkdir(parents=True, exist_ok=True)
            for note_file in sorted(NOTES_DIR.glob("*.md")):
                sid = note_file.stem
                row = conn.execute(
                    "SELECT updated_at FROM sessions WHERE session_id = ?",
                    (sid,),
                ).fetchone()
                if row is None:
                    continue
                db_updated_at = row["updated_at"] or ""
                try:
                    text = note_file.read_text(encoding="utf-8")
                except OSError:
                    continue
                stamp = extract_rendered_updated_at(text) or ""
                if stamp >= db_updated_at:
                    unchanged += 1
                    continue
                status, message = _render_one_note(
                    conn, sid, force=args.force
                )
                if status == "rendered":
                    rendered += 1
                elif status == "skipped":
                    skipped += 1
                    print(f"skipped: {message}", file=sys.stderr)
            print(
                f"{rendered} refreshed, {skipped} skipped (unsynced edits), "
                f"{unchanged} unchanged"
            )
            return
        finally:
            conn.close()

    if op == "sync":
        path = trace_note_path(args.session_id)
        if not path.exists():
            print(f"error: note file not found: {path}", file=sys.stderr)
            sys.exit(1)

        text = path.read_text(encoding="utf-8")
        block = extract_trace_note_notes(text)
        if block is None:
            print(
                "error: file is missing a `## Notes` heading; refusing to sync",
                file=sys.stderr,
            )
            sys.exit(1)

        # Block is empty string (user cleared) or their text. Both paths
        # route through update_session(notes=...) with an explicit
        # non-None value so the column actually gets written (update_session
        # skips `notes is None`).
        conn = open_index()
        try:
            from .workbench.index import update_session
            ok = update_session(conn, args.session_id, notes=block)
            if not ok:
                print(
                    f"error: session not found: {args.session_id}",
                    file=sys.stderr,
                )
                sys.exit(1)

            # Best-effort re-render so `rendered_updated_at` matches the new
            # sessions.updated_at. A crash between these two operations is
            # recoverable via `note render --stale` (the next pass re-renders
            # to byte-identical content with a fresh stamp).
            session = _load_session_row(conn, args.session_id)
            if session is None:
                print(
                    f"warning: session disappeared mid-sync: {args.session_id}",
                    file=sys.stderr,
                )
                sys.exit(1)
            new_text = render_trace_note(session, session.get("reviewer_notes"))
            write_note_atomically(path, new_text)
            print(f"synced: {path}")
            return
        finally:
            conn.close()

    # No subcommand given
    print("error: note requires a subcommand (render, sync, path)", file=sys.stderr)
    sys.exit(2)


_AUTO_UPDATE_SKIP_FLAGS = frozenset({"-h", "--help", "--version"})


def _should_auto_update(argv: list[str] | None = None) -> bool:
    """Return False for commands where a pre-parse update changes semantics.

    Suppresses in two cases:
      - The user explicitly invoked `clawjournal selfupdate` — let
        that command be the only updater for this invocation.
      - The user invoked help/version (`-h`, `--help`, `--version`)
        with no real subcommand — argparse prints and exits, so a
        background fetch is wasted work that surprises the user.

    Matching `"selfupdate"` anywhere in argv produced false positives
    like `clawjournal export --output ./selfupdate`.
    """
    tokens = (sys.argv if argv is None else argv)[1:]
    if not tokens:
        return True
    for token in tokens:
        if token.startswith("-"):
            continue
        return token != "selfupdate"
    # All tokens were flags — no subcommand. Skip when any are help/version.
    return not any(t in _AUTO_UPDATE_SKIP_FLAGS for t in tokens)


def main() -> None:
    # Fire the silent, throttled auto-update before any work. Returns
    # immediately on opt-out, non-git installs, or throttle hits.
    if _should_auto_update():
        try:
            from .selfupdate import maybe_self_update
            maybe_self_update()
        except Exception:
            # Auto-update must never block the CLI. Swallow anything.
            pass

    parser = argparse.ArgumentParser(description="ClawJournal — coding agent conversation exporter")
    sub = parser.add_subparsers(dest="command")

    prep_parser = sub.add_parser("prep", help="Data prep — discover projects, output JSON")
    prep_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
    sub.add_parser("status", help="Show current stage and next steps (JSON)")
    cf = sub.add_parser("confirm", help="Scan for PII, summarize export, and record review state (JSON)")
    cf.add_argument("--file", "-f", type=Path, default=None, help="Path to export JSONL file")
    cf.add_argument("--full-name", type=str, default=None,
                    help="User's full name to scan for in the export file (exact-name privacy check).")
    cf.add_argument("--skip-full-name-scan", action="store_true",
                    help="Skip exact full-name scan when the user declines sharing their name.")
    cf.add_argument("--attest-full-name", type=str, default=None,
                    help="Text attestation describing how full-name scan was done.")
    cf.add_argument("--attest-sensitive", type=str, default=None,
                    help="Text attestation describing sensitive-entity review and outcome.")
    cf.add_argument("--attest-manual-scan", type=str, nargs="?", const="__DEPRECATED_FLAG__", default=None,
                    help=f"Text attestation describing manual scan ({MIN_MANUAL_SCAN_SESSIONS}+ sessions).")
    # Deprecated boolean attestations retained only for a guided migration error.
    cf.add_argument("--attest-asked-full-name", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-sensitive", action="store_true", help=argparse.SUPPRESS)
    cf.add_argument("--attest-asked-manual-scan", action="store_true", help=argparse.SUPPRESS)
    list_parser = sub.add_parser("list", help="List all projects")
    list_parser.add_argument("--source", choices=SOURCE_CHOICES, default="auto")

    us = sub.add_parser("update-skill", help="Install/update the clawjournal skill for a coding agent")
    us.add_argument("target", choices=["claude", "openclaw", "codex", "cline"],
                    help="Agent to install skill for")

    su = sub.add_parser("selfupdate",
                        help="Pull the latest clawjournal from the public repo (manual sync update)")
    su.add_argument("--check", action="store_true",
                    help="Report whether updates are available without applying them")
    su.add_argument("--force", action="store_true",
                    help="Discard local changes on main before applying a fast-forward update")
    su.add_argument("--json", action="store_true", help="Output result as JSON")

    cfg = sub.add_parser("config", help="View or set config")
    cfg.add_argument("--repo", type=str, help=argparse.SUPPRESS)
    cfg.add_argument("--source", choices=sorted(EXPLICIT_SOURCE_CHOICES),
                     help="Set export source scope explicitly: claude, codex, gemini, or all")
    cfg.add_argument("--exclude", type=str, help="Comma-separated projects to exclude")
    cfg.add_argument("--redact", type=str,
                     help="Comma-separated strings to always redact (API keys, usernames, domains)")
    cfg.add_argument("--redact-usernames", type=str,
                     help="Comma-separated usernames to anonymize (GitHub handles, Discord names)")
    cfg.add_argument("--confirm-projects", action="store_true",
                     help="Mark project selection as confirmed (include all)")

    events_parser = sub.add_parser("events", help="Execution recorder commands")
    events_sub = events_parser.add_subparsers(dest="events_command", required=True)
    events_ingest = events_sub.add_parser(
        "ingest",
        help="Drain pending raw execution events into index.db",
    )
    events_ingest.add_argument("--source", choices=EVENT_SOURCE_CHOICES, default="auto")
    events_ingest.add_argument("--json", action="store_true", help="Output JSON summary")
    events_sub.add_parser(
        "capabilities",
        help="Dump the execution-recorder capability matrix as JSON",
    )
    events_inspect = events_sub.add_parser(
        "inspect",
        help="Inspect a single event, its vendor line, and any override",
    )
    events_inspect.add_argument(
        "event_id",
        nargs="?",
        type=int,
        help="events.id primary key (omit when using --session/--event-key)",
    )
    events_inspect.add_argument("--session", help="session_key (with --event-key)")
    events_inspect.add_argument("--event-key", dest="event_key", help="event_key (with --session)")
    events_inspect.add_argument("--json", action="store_true", help="Output structured JSON")
    events_inspect.add_argument(
        "--truncate",
        type=int,
        default=None,
        metavar="N",
        help="Truncate raw_json / payload_json / vendor line to N chars "
             "(default: 1024 in human mode, no truncation in --json mode)",
    )

    events_cost = events_sub.add_parser(
        "cost",
        help="Cost ledger commands (token attribution + anomaly detection)",
    )
    events_cost_sub = events_cost.add_subparsers(
        dest="cost_command", required=True
    )
    events_cost_ingest = events_cost_sub.add_parser(
        "ingest",
        help="Extract token usage from already-recorded events and detect anomalies",
    )
    events_cost_ingest.add_argument(
        "--rebuild",
        action="store_true",
        help="Replay the full cost ledger from raw events, bypassing the incremental cursor",
    )
    events_cost_ingest.add_argument(
        "--json", action="store_true", help="Output JSON summary"
    )

    events_incidents = events_sub.add_parser(
        "incidents",
        help="Incidents pipeline (loop detector lite, future: more kinds)",
    )
    events_incidents_sub = events_incidents.add_subparsers(
        dest="incidents_command", required=True
    )
    events_incidents_detect = events_incidents_sub.add_parser(
        "detect",
        help="Detect exact-repeat command/tool-call loops in recorded events",
    )
    events_incidents_detect.add_argument(
        "--rebuild",
        action="store_true",
        help="Clear loop incidents + cursor, re-evaluate every session",
    )
    events_incidents_detect.add_argument(
        "--json", action="store_true", help="Output JSON summary"
    )

    events_export = events_sub.add_parser(
        "export",
        help="Export a session to a single self-describing JSON bundle (plan 07)",
    )
    events_export.add_argument(
        "session_key",
        help="event_sessions.session_key (per ADR-001) — never the workbench session_id",
    )
    events_export.add_argument(
        "--out",
        dest="output",
        type=str,
        default=None,
        help="Output path (default: ~/.clawjournal/exports/clawjournal-bundle-<hash>.json)",
    )
    events_export.add_argument(
        "--no-snippets",
        action="store_true",
        help="Omit source_snippets from the bundle (smaller file, breaks off-machine inspect)",
    )
    events_export.add_argument(
        "--no-children",
        action="store_true",
        help="Don't include subagent child sessions whose parent_session_key matches",
    )
    events_export.add_argument(
        "--allow-no-workbench-row",
        action="store_true",
        help="Opt past the hold-state gate for events-only sessions (no workbench review)",
    )
    pretty_group = events_export.add_mutually_exclusive_group()
    pretty_group.add_argument(
        "--pretty",
        dest="pretty",
        action="store_true",
        help="Pretty-print the bundle JSON (default)",
    )
    pretty_group.add_argument(
        "--compact",
        dest="pretty",
        action="store_false",
        help="Compact bundle JSON (smaller file, harder to diff)",
    )
    events_export.set_defaults(pretty=True)
    events_export.add_argument(
        "--json", action="store_true", help="Output JSON status summary"
    )

    events_import = events_sub.add_parser(
        "import",
        help="Import a bundle JSON file produced by `events export` (plan 07)",
    )
    events_import.add_argument("bundle_path", help="Path to bundle.json")
    events_import.add_argument(
        "--rebuild-derived",
        action="store_true",
        help="Re-run cost ledger + loop detector against imported events instead of "
             "importing the bundle's token_usage / cost_anomalies / incidents rows",
    )
    events_import.add_argument(
        "--json", action="store_true", help="Output JSON status summary"
    )

    events_doctor = events_sub.add_parser(
        "doctor",
        help="Diagnose this clawjournal install (versions, schema, ingest health)",
    )
    events_doctor.add_argument(
        "--json", action="store_true", help="Output structured JSON report"
    )
    events_doctor.add_argument(
        "--fix",
        action="store_true",
        help="Write a capability_overlay.yaml for known additive drift "
             "(refuses structural drift)",
    )
    events_doctor.add_argument(
        "--request-id",
        dest="request_id",
        help="Opaque ID echoed into _meta.request_id when --json is set",
    )

    events_features = events_sub.add_parser(
        "features",
        help="Static product feature surface (commands, limits, schema versions); "
             "always emits JSON",
    )
    events_features.add_argument(
        "--request-id",
        dest="request_id",
        help="Opaque ID echoed into _meta.request_id",
    )

    events_docs = events_sub.add_parser(
        "docs",
        help="Topic-based docs corpus for AI agents",
    )
    events_docs.add_argument(
        "topic",
        nargs="?",
        help="One of: guide, commands, schemas, examples, exit-codes, errors",
    )
    events_docs.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON instead of markdown",
    )
    events_docs.add_argument(
        "--request-id",
        dest="request_id",
        help="Opaque ID echoed into _meta.request_id when --json is set",
    )

    # Aggregation subcommands (plan 10): events aggregate,
    # events incidents aggregate, events cost aggregate. Shared flag
    # shape registered via a helper so all three stay in lockstep.
    events_aggregate = events_sub.add_parser(
        "aggregate",
        help="Cross-session aggregation over events (counts/sums/avgs by dimension)",
    )
    _add_aggregate_flags(events_aggregate, domain="events")
    events_aggregate.add_argument(
        "--canonical",
        action="store_true",
        help="(reserved; not yet wired in v0.1) — when shipped, will route "
             "the aggregation through canonical_events() so override-promoted "
             "rows win and native+LA duplicates dedup. Today the flag is "
             "rejected with a usage error rather than silently no-opping",
    )

    events_incidents_aggregate = events_incidents_sub.add_parser(
        "aggregate",
        help="Cross-session aggregation over incidents",
    )
    _add_aggregate_flags(events_incidents_aggregate, domain="incidents")

    events_cost_aggregate = events_cost_sub.add_parser(
        "aggregate",
        help="Cross-session aggregation over token_usage / cost_anomalies "
             "(auto-partitions by data_source)",
    )
    _add_aggregate_flags(events_cost_aggregate, domain="cost")

    # Search subcommand (plan 11): cross-session FTS5 over events.raw_json.
    events_search = events_sub.add_parser(
        "search",
        help="Cross-session full-text search over events (FTS5 over raw_json)",
    )
    events_search.add_argument(
        "query",
        nargs="?",
        default=None,
        help="FTS5 MATCH expression (phrase \"...\", AND/OR/NOT, prefix foo*, "
             "NEAR/N). Required unless --rebuild-index is set",
    )
    events_search.add_argument(
        "--client", action="append", default=[], metavar="CLIENT",
        help="Filter to this client (repeat or comma-separate)",
    )
    events_search.add_argument(
        "--type", action="append", default=[], metavar="TYPE",
        dest="event_type",
        help="Filter to these event types (repeat or comma-separate)",
    )
    events_search.add_argument(
        "--confidence", action="append", default=[], metavar="LEVEL",
        help="Filter to these confidence levels (repeat or comma-separate)",
    )
    events_search.add_argument(
        "--session", default=None, metavar="KEY",
        help="Filter to a single session_key",
    )
    events_search.add_argument(
        "--source", action="append", default=[], metavar="SRC",
        help="Filter to these sources (e.g. claude-jsonl). Repeat or "
             "comma-separate, same as --client / --type / --confidence",
    )
    events_search.add_argument(
        "--since", default=None, metavar="DURATION",
        help="Lower-bound time filter: Nd / Nh / Nm / today / thisweek",
    )
    events_search.add_argument(
        "--limit", type=int, default=None, metavar="N",
        help="Top-N hits to return (default: 50, ceiling: 1000)",
    )
    events_search.add_argument(
        "--snippet-tokens", type=int, default=None, metavar="N",
        dest="snippet_tokens",
        help="Snippet window length in tokens, not characters (default: 16, "
             "range 1-64). FTS5 caps at 64 tokens internally; values above "
             "64 are silently clamped, so the cap is enforced here. v0.1 "
             "originally called this --snippet-length and claimed "
             "characters — round-1 fix renames to match the FTS5 contract",
    )
    events_search.add_argument(
        "--include-held", action="store_true", dest="include_held",
        help="Include events from held sessions (pending_review / embargoed). "
             "Default: held sessions are excluded so search results don't "
             "surface content under redaction review",
    )
    events_search.add_argument(
        "--rebuild-index", action="store_true", dest="rebuild_index",
        help="One-shot reindex of events_fts from events. Use after a "
             "DELETE FROM events_fts or any surgery on events that bypassed "
             "the triggers",
    )
    events_search.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human output",
    )
    events_search.add_argument(
        "--request-id", dest="request_id",
        help="Opaque ID echoed into _meta.request_id when --json is set",
    )

    # Workbench commands
    serve_parser = sub.add_parser("serve", help="Start the workbench daemon + web UI")
    serve_parser.add_argument("--port", type=int, default=8384, help="Port (default: 8384)")
    serve_parser.add_argument("--no-browser", action="store_true", help="Don't open browser")
    serve_parser.add_argument("--remote", action="store_true",
                              help="Print SSH tunnel command for remote VM access")
    serve_parser.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None,
                              help="Only scan this source")

    scan_parser = sub.add_parser("scan", help="One-shot index sessions into local workbench DB")
    scan_parser.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None,
                             help="Only scan this source")
    scan_parser.add_argument("--force", action="store_true",
                             help="Force-rebuild findings, bypassing settle + revision checks")
    scan_parser.add_argument("--all", action="store_true",
                             help="With --force: rebuild findings for every session")
    scan_parser.add_argument("session_ids", nargs="*",
                             help="With --force: rebuild findings for these session IDs")

    # Hold-state lifecycle
    hold_p = sub.add_parser("hold", help="Move session to pending_review hold")
    hold_p.add_argument("session_id")
    hold_p.add_argument("--reason", type=str, default=None)

    release_p = sub.add_parser("release", help="Release a session for hosted share")
    release_p.add_argument("session_id")
    release_p.add_argument("--reason", type=str, default=None)

    embargo_p = sub.add_parser("embargo", help="Embargo a session until a future date")
    embargo_p.add_argument("session_id")
    embargo_p.add_argument("--until", required=True,
                           help="Embargo-until date (YYYY-MM-DD or ISO 8601, must be future)")
    embargo_p.add_argument("--reason", type=str, default=None)

    hh_p = sub.add_parser("hold-history", help="Print the full hold-state timeline for a session")
    hh_p.add_argument("session_id")

    # Findings review
    fnd_p = sub.add_parser("findings", help="List or decide findings for a session")
    fnd_p.add_argument("session_id")
    fnd_p.add_argument("--all", action="store_true", help="Show already-decided findings too")
    fnd_p.add_argument("--accept", action="append", metavar="REF",
                       help="Accept finding(s) by finding_id or entity_hash prefix")
    fnd_p.add_argument("--ignore", action="append", metavar="REF",
                       help="Ignore finding(s) by finding_id or entity_hash prefix")
    fnd_p.add_argument("--accept-all", action="store_true",
                       help="Accept every open finding in the session")
    fnd_p.add_argument("--ignore-all", action="store_true",
                       help="Ignore every open finding in the session")
    fnd_p.add_argument("--accept-engine", metavar="NAME",
                       help="Accept all open findings from a specific engine (e.g. regex_secrets)")
    fnd_p.add_argument("--ignore-engine", metavar="NAME",
                       help="Ignore all open findings from a specific engine")
    fnd_p.add_argument("--reason", type=str, default=None)
    fnd_p.add_argument("--global", dest="global_", action="store_true",
                       help="With --ignore: also add the entity to the cross-session allowlist")

    # Allowlist
    al_p = sub.add_parser("allowlist", help="Manage the cross-session findings allowlist")
    al_sub = al_p.add_subparsers(dest="op")
    al_sub.add_parser("list", help="Show every allowlist entry")
    al_add = al_sub.add_parser("add", help="Allowlist an entity (hashed locally, plaintext discarded)")
    al_add.add_argument("entity_text")
    al_add.add_argument("--type", dest="type", default=None,
                        help="Entity type (jwt, email, etc.); NULL matches any type")
    al_add.add_argument("--label", default=None, help="Short non-sensitive mnemonic")
    al_add.add_argument("--reason", default=None)
    al_rm = al_sub.add_parser("remove", help="Remove allowlist entry and revert/reassign findings")
    al_rm.add_argument("allowlist_id", nargs="?", default=None)
    al_rm.add_argument("--by-text", default=None, help="Hash plaintext locally and remove matching entries")
    al_rm.add_argument("--type", dest="type", default=None,
                       help="With --by-text: filter by entity_type")

    inbox_parser = sub.add_parser("inbox", help="List indexed sessions in terminal")
    inbox_parser.add_argument("--status", choices=["new", "shortlisted", "approved", "blocked"],
                              default=None)
    inbox_parser.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None)
    inbox_parser.add_argument("--limit", type=int, default=20)
    inbox_parser.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    # Review action commands
    for action_name in ("approve", "block", "shortlist"):
        action_parser = sub.add_parser(action_name, help=f"{action_name.title()} sessions by ID")
        action_parser.add_argument("session_ids", nargs="+", help="Session IDs to update")
        action_parser.add_argument("--reason", type=str, default=None, help="Reason for the action")

    # Scoring commands
    sv = sub.add_parser("score-view", help="Show condensed session view for AI scoring")
    sv.add_argument("session_ids", nargs="*", help="Session IDs to view")
    sv.add_argument("--batch", action="store_true", help="Compact batch format")
    sv.add_argument("--limit", type=int, default=5, help="Sessions per batch")
    sv.add_argument("--offset", type=int, default=0, help="Offset for batch")
    sv.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None)

    ss = sub.add_parser("set-score", help="Record manual AI score overrides for sessions")
    ss.add_argument("session_ids", nargs="+", help="Session IDs")
    ss.add_argument("--failure-value", type=int, choices=range(1, 6), help="Failure value 1-5")
    ss.add_argument(
        "--failure-evidence",
        action="append",
        default=[],
        help="Trace-backed evidence for manual failure-value 4-5 overrides; repeat for multiple snippets",
    )
    ss.add_argument("--quality", type=int, choices=range(1, 6), help="Legacy productivity quality 1-5")
    ss.add_argument("--reason", type=str, default=None, help="Reason for the score")

    sb = sub.add_parser("score-batch", help="List unscored sessions for AI scoring")
    sb.add_argument("--limit", type=int, default=50)
    sb.add_argument("--source", default="failure-v1",
                    help="Source scope: failure-v1, comma list, all/auto, or a single source")

    sc = sub.add_parser("score", help="Auto-score sessions via the current agent's automation CLI or an explicit backend")
    sc.add_argument("session_ids", nargs="*", help="Session IDs to score")
    sc.add_argument("--batch", action="store_true", help="Score all unscored sessions")
    sc.add_argument("--limit", type=int, default=10, help="Max sessions for batch mode (default: 10)")
    sc.add_argument("--source", default="failure-v1",
                    help="Batch source scope: failure-v1, comma list, all/auto, or a single source")
    sc.add_argument("--backend", choices=SCORING_BACKEND_CHOICES, default="auto",
                    help="Scoring backend (default: auto = current agent's automation CLI)")
    sc.add_argument("--model", type=str, default=None,
                    help="Optional model override for the selected backend")
    sc.add_argument("--dry-run", action="store_true", help="Show score-view without calling a scoring backend")
    sc.add_argument("--auto-triage", action="store_true",
                    help="After scoring, auto-archive 1/5 noise sessions")

    rs = sub.add_parser(
        "rescore",
        help="Rescore recent in-scope sessions, ignoring existing scores",
    )
    rs.add_argument(
        "--window",
        type=_parse_window_days,
        default="7d",
        help="Rolling time window by start_time (e.g. 7d, 14d, 30d). Default: 7d",
    )
    rs.add_argument(
        "--source",
        default="failure-v1",
        help="Source scope: failure-v1, comma list, all/auto, or a single source",
    )
    rs.add_argument("--limit", type=int, default=200, help="Max sessions to rescore (default: 200)")
    rs.add_argument(
        "--backend",
        choices=SCORING_BACKEND_CHOICES,
        default="auto",
        help="Scoring backend (default: auto = current agent's automation CLI)",
    )
    rs.add_argument(
        "--model",
        type=str,
        default=None,
        help="Optional model override for the selected backend",
    )
    rs.add_argument(
        "--dry-run",
        action="store_true",
        help="Skip the scoring backend; report which sessions would be rescored",
    )

    # Bundle commands
    bc = sub.add_parser("bundle-create", help="Create a bundle from approved sessions")
    bc.add_argument("session_ids", nargs="*", help="Session IDs to bundle (omit to use --status)")
    bc.add_argument("--status", choices=["approved", "shortlisted"],
                    help="Auto-select all sessions with this review status")
    bc.add_argument("--note", type=str, default=None, help="Submission note")
    bc.add_argument("--attestation", type=str, default=None, help="Attestation text")
    bc.add_argument("--json", action="store_true", help="Output JSON")

    bl = sub.add_parser("bundle-list", help="List all bundles")
    bl.add_argument("--json", action="store_true", help="Output JSON")

    bv = sub.add_parser("bundle-view", help="View bundle details")
    bv.add_argument("share_id", help="Bundle ID (or prefix)")
    bv.add_argument("--json", action="store_true", help="Output JSON")

    be = sub.add_parser("bundle-export", help="Export bundle to disk as JSONL + manifest")
    be.add_argument("share_id", help="Bundle ID (or prefix)")
    be.add_argument("--output", "-o", type=str, default=None, help="Custom output directory")
    be.add_argument("--training-format", action="store_true",
                    help="Also produce a training-format JSONL (turn-based, cleaned)")
    be.add_argument("--json", action="store_true", help="Output JSON")

    bs = sub.add_parser("bundle-share", help="Share bundle via ingest service")
    bs.add_argument("share_id", help="Bundle ID (or prefix)")
    bs.add_argument("--force", action="store_true", help="Override duplicate check")
    bs.add_argument("--json", action="store_true", help="Output JSON")

    # Share command (one-step: create + export + share)
    sh = sub.add_parser("share", help="Bundle and share sessions in one step")
    sh.add_argument("session_ids", nargs="*", help="Session IDs (omit to use --status)")
    sh.add_argument("--status", choices=["approved", "shortlisted"],
                    help="Auto-select sessions with this review status")
    sh.add_argument("--note", type=str, default=None, help="Submission note")
    sh.add_argument("--force", action="store_true", help="Override duplicate check")
    sh.add_argument("--preview", action="store_true",
                    help="Show the trace bundle contents that would be shared without uploading")
    sh.add_argument("--json", action="store_true", help="Output JSON")

    ve = sub.add_parser("verify-email", help="Verify a .edu email address for a short-lived upload token")
    ve.add_argument("email", nargs="?", help="Your .edu email address")
    ve.add_argument("--code", type=str, default=None, help="Verification code from email")

    # PII review/apply commands
    pr = sub.add_parser("pii-review", help="Generate structured PII findings from an export JSONL")
    pr.add_argument("--file", "-f", required=True, type=Path, help="Path to export JSONL file")
    pr.add_argument("--output", "-o", required=True, type=Path, help="Path to findings JSON output")
    pr.add_argument("--limit-sessions", type=int, default=None, help="Optional max sessions to review")
    pr.add_argument("--min-confidence", type=float, default=0.0, help="Minimum confidence to keep")
    pr.add_argument("--provider", type=_parse_pii_provider_arg, default="hybrid", metavar="PROVIDER",
                    help="PII review strategy: rules (regex only), ai (AI agent), or hybrid (both). Legacy 'claude' is accepted as an alias for 'ai'. Default: hybrid")
    pr.add_argument("--rubric-file", type=Path, default=None,
                    help="Custom PII review rubric file (replaces built-in prompt for ai/hybrid providers)")
    pr.add_argument("--backend", choices=list(BACKEND_CHOICES), default="auto",
                    help="Agent backend for AI-based PII review (default: auto = current agent's CLI)")
    pr.add_argument("--json", action="store_true", help="Output JSON summary")

    sub.add_parser("pii-rubric", help="Print the built-in PII review rubric for iteration")

    pa = sub.add_parser("pii-apply", help="Apply structured PII findings to an export JSONL")
    pa.add_argument("--file", "-f", required=True, type=Path, help="Path to export JSONL file")
    pa.add_argument("--findings", required=True, type=Path, help="Path to findings JSON")
    pa.add_argument("--output", "-o", required=True, type=Path, help="Path to sanitized JSONL output")
    pa.add_argument("--min-confidence", type=float, default=0.0, help="Minimum confidence to apply")
    pa.add_argument("--json", action="store_true", help="Output JSON summary")

    # Training format conversion
    tf = sub.add_parser("training-format",
                        help="Convert exported JSONL to provider-agnostic training format (turn-based)")
    tf.add_argument("--file", "-f", required=True, type=Path,
                    help="Path to exported sessions JSONL (or bundle sessions.jsonl)")
    tf.add_argument("--output", "-o", required=True, type=Path,
                    help="Path to training-format JSONL output")
    tf.add_argument("--json", action="store_true", help="Output JSON summary")

    # Segmentation command
    seg_parser = sub.add_parser("segment", help="Segment multi-task OpenClaw sessions into child traces")
    seg_parser.add_argument("session_ids", nargs="*", help="Session IDs to segment (omit for all)")
    seg_parser.add_argument("--source", choices=["openclaw"], default="openclaw",
                            help="Source to segment (only openclaw supported)")
    seg_parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    seg_parser.add_argument("--force", action="store_true", help="Re-segment already-segmented sessions")
    seg_parser.add_argument("--time-gap", type=str, default="30m",
                            help="Time gap threshold for boundary detection (e.g., 30m, 1h)")
    seg_parser.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    # Quick Share commands
    recent_parser = sub.add_parser("recent", help="Show recent sessions (auto-scans if stale)")
    recent_parser.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None)
    recent_parser.add_argument("--since", type=str, default=None,
                               help="Filter: 'today', 'yesterday', 'this week', or ISO date")
    recent_parser.add_argument("--limit", type=int, default=5, help="Max sessions (default: 5)")
    recent_parser.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    card_parser = sub.add_parser("card", help="Generate a share card for a session")
    card_parser.add_argument("session_ids", nargs="+", help="Session IDs (or index from `recent`)")
    card_parser.add_argument("--depth", choices=["workflow", "summary", "full"],
                             default="summary", help="Content depth (default: summary)")
    card_parser.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    # Trace note commands — DB-connected markdown summary per session.
    # See docs/db-refactor.md.
    note_p = sub.add_parser("note", help="Render / sync per-session markdown trace notes")
    note_sub = note_p.add_subparsers(dest="note_op")

    note_render = note_sub.add_parser(
        "render",
        help="Render trace note(s) from DB. One session id, or --all-scored, or --stale.",
    )
    note_render.add_argument("session_id", nargs="?", default=None)
    note_render.add_argument("--all-scored", action="store_true",
                             help="Create missing notes for every session with ai_summary")
    note_render.add_argument("--stale", action="store_true",
                             help="Re-render notes whose stamp is behind sessions.updated_at")
    note_render.add_argument("--force", action="store_true",
                             help="Overwrite files with unsynced ## Notes edits")

    note_sync = note_sub.add_parser(
        "sync",
        help="Write the file's ## Notes back into sessions.reviewer_notes, then re-render",
    )
    note_sync.add_argument("session_id")

    note_path_p = note_sub.add_parser(
        "path",
        help="Print the canonical note path for a session id",
    )
    note_path_p.add_argument("session_id")

    # Insights command
    ins = sub.add_parser("insights", help="Token efficiency advisor — analyze usage patterns")
    ins.add_argument("--days", type=int, default=7, help="Days to analyze (default: 7)")
    ins.add_argument("--json", action="store_true", help="Output JSON")
    ins.add_argument("--detail", action="store_true", help="Show detailed recommendations")

    # Refresh pricing
    sub.add_parser("refresh-pricing", help="Refresh model pricing cache from OpenRouter")

    # Search command
    srch = sub.add_parser("search", help="Full-text search across sessions")
    srch.add_argument("query", help="Search query")
    srch.add_argument("--limit", type=int, default=20, help="Max results (default: 20)")
    srch.add_argument("--source", choices=WORKBENCH_SOURCE_CHOICES, default=None)
    srch.add_argument("--json", action="store_true", help="Output JSON for agent parsing")

    exp = sub.add_parser("export", help="Export conversation data locally.")
    for target in (exp, parser):
        target.add_argument("--output", "-o", type=Path, default=None)
        target.add_argument("--repo", "-r", type=str, default=None, help=argparse.SUPPRESS)
        target.add_argument("--source", choices=SOURCE_CHOICES, default="auto")
        target.add_argument("--all-projects", action="store_true")
        target.add_argument("--no-thinking", action="store_true")
        target.add_argument("--format", choices=["jsonl", "md", "md-summary"], default="jsonl",
                            help="Export format: jsonl (default), md (full markdown), md-summary (AI summary)")
        target.add_argument("--pii-review", action="store_true",
                            help="After export, automatically generate structured PII findings")
        target.add_argument("--pii-provider", type=_parse_pii_provider_arg, default="rules", metavar="PROVIDER",
                            help="PII review strategy for local export: rules, ai, or hybrid. Legacy 'claude' is accepted as an alias for 'ai'. Default: rules.")
        target.add_argument("--pii-findings-output", type=Path, default=None,
                            help="Custom findings JSON path for --pii-review")
        target.add_argument("--pii-apply", action="store_true",
                            help="After PII review, automatically apply findings to produce a sanitized JSONL")
        target.add_argument("--pii-sanitized-output", type=Path, default=None,
                            help="Custom output path for sanitized JSONL when --pii-apply is enabled")
        target.add_argument("--pii-backend", choices=list(BACKEND_CHOICES), default="auto",
                            help="Agent backend for AI-based PII review (default: auto = current agent's CLI)")

    args = parser.parse_args()
    command = args.command or "export"

    if command == "serve":
        from .pricing import ensure_pricing_fresh
        ensure_pricing_fresh()
        from .workbench.daemon import run_server
        run_server(
            port=args.port,
            open_browser=not args.no_browser,
            source_filter=args.source,
            remote=args.remote,
        )
        return

    if command == "scan":
        if args.force or args.all or args.session_ids:
            from .cli_security import run_scan_force
            run_scan_force(args)
            return
        _run_scan(source_filter=args.source)
        return

    if command == "hold":
        from .cli_security import run_hold
        run_hold(args)
        return

    if command == "release":
        from .cli_security import run_release
        run_release(args)
        return

    if command == "embargo":
        from .cli_security import run_embargo
        run_embargo(args)
        return

    if command == "hold-history":
        from .cli_security import run_hold_history
        run_hold_history(args)
        return

    if command == "findings":
        from .cli_security import run_findings
        run_findings(args)
        return

    if command == "allowlist":
        from .cli_security import run_allowlist
        run_allowlist(args)
        return

    if command == "inbox":
        _run_inbox(status=args.status, source=args.source, limit=args.limit,
                   output_json=args.json)
        return

    if command in ("approve", "block", "shortlist"):
        status_map = {"approve": "approved", "block": "blocked", "shortlist": "shortlisted"}
        _run_review_action(status_map[command], args.session_ids, reason=args.reason)
        return

    if command == "score-view":
        _run_score_view(args)
        return

    if command == "set-score":
        _run_set_score(args)
        return

    if command == "score-batch":
        _run_score_batch(args)
        return

    if command == "score":
        _run_score(args)
        return

    if command == "rescore":
        _run_rescore(args)
        return

    if command == "bundle-create":
        _run_bundle_create(args)
        return

    if command == "bundle-list":
        _run_bundle_list(args)
        return

    if command == "bundle-view":
        _run_bundle_view(args)
        return

    if command == "bundle-export":
        _run_bundle_export(args)
        return

    if command == "bundle-share":
        _run_bundle_share(args)
        return

    if command == "share":
        _run_share(args)
        return

    if command == "verify-email":
        _run_verify_email(args)
        return

    if command == "pii-rubric":
        from .redaction.pii import PII_REVIEW_RUBRIC
        print(PII_REVIEW_RUBRIC)
        return

    if command == "pii-review":
        from .cli_security import emit_legacy_pii_notice
        emit_legacy_pii_notice()
        _run_pii_review(args)
        return

    if command == "pii-apply":
        from .cli_security import emit_legacy_pii_notice
        emit_legacy_pii_notice()
        _run_pii_apply(args)
        return

    if command == "training-format":
        _run_training_format(args)
        return

    if command == "insights":
        _run_insights(args)
        return

    if command == "refresh-pricing":
        from .pricing import refresh_pricing
        success = refresh_pricing(quiet=False)
        if not success:
            sys.exit(1)
        return

    if command == "search":
        _run_search(args)
        return

    if command == "segment":
        _run_segment(args)
        return

    if command == "recent":
        _run_recent(args)
        return

    if command == "card":
        _run_card(args)
        return

    if command == "note":
        _run_note(args)
        return

    if command == "prep":
        prep(source_filter=args.source)
        return

    if command == "status":
        status()
        return

    if command == "confirm":
        if (
            args.attest_asked_full_name
            or args.attest_asked_sensitive
            or args.attest_asked_manual_scan
            or args.attest_manual_scan == "__DEPRECATED_FLAG__"
        ):
            print(json.dumps({
                "error": "Deprecated boolean attestation flags were provided.",
                "hint": (
                    "Use text attestations instead so the command can validate what was reviewed."
                ),
                "blocked_on_step": "Step 2/2",
                "process_steps": EXPORT_REVIEW_PUBLISH_STEPS,
                "next_command": CONFIRM_COMMAND_EXAMPLE,
            }, indent=2))
            sys.exit(1)
        confirm(
            file_path=args.file,
            full_name=args.full_name,
            attest_asked_full_name=args.attest_full_name,
            attest_asked_sensitive=args.attest_sensitive,
            attest_manual_scan=args.attest_manual_scan,
            skip_full_name_scan=args.skip_full_name_scan,
        )
        return

    if command == "update-skill":
        update_skill(args.target)
        return

    if command == "selfupdate":
        from .selfupdate import selfupdate_sync
        result = selfupdate_sync(check_only=args.check, force=args.force)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            status = result.get("status")
            if status == "not-a-checkout":
                print("clawjournal is not installed from an editable git checkout — nothing to update.")
            elif status == "up-to-date":
                print(f"Already up to date ({str(result.get('head', ''))[:7]}).")
            elif status == "behind":
                print(
                    f"Update available: {str(result.get('head', ''))[:7]} -> "
                    f"{str(result.get('upstream', ''))[:7]}. Run `clawjournal selfupdate` to apply."
                )
            elif status == "updated":
                print(
                    f"Updated {str(result.get('head', ''))[:7]} -> "
                    f"{str(result.get('upstream', ''))[:7]}."
                )
            elif status == "dirty":
                print("Skipped: the checkout has local changes. Commit/stash or pass --force.")
            elif isinstance(status, str) and status.startswith("branch-"):
                print(f"Skipped: not on the main branch ({status[len('branch-'):]}). Check out main first.")
            elif status == "ahead":
                print("Skipped: local main has commits not present on origin/main. Update manually.")
            elif status == "diverged":
                print("Skipped: local main has diverged from origin/main. Update manually.")
            elif status == "ancestry-failed":
                print("Skipped: could not compare local main with origin/main.")
            elif status == "fetch-failed":
                print(f"Fetch failed: {result.get('stderr', '')}".rstrip())
            elif status == "update-failed":
                print(f"Update failed: {result.get('stderr', '')}".rstrip())
            else:
                print(f"selfupdate status: {status}")
        return

    if command == "list":
        config = load_config()
        resolved_source_choice, _ = _resolve_source_choice(args.source, config)
        list_projects(source_filter=resolved_source_choice)
        return

    if command == "config":
        _handle_config(args)
        return

    if command == "events":
        _run_events(args)
        return

    _run_export(args)


def _parse_csv_arg(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [item.strip() for item in value.split(",") if item.strip()]


def _handle_config(args) -> None:
    """Handle the config subcommand."""
    has_changes = (
        args.repo
        or args.source
        or args.exclude
        or args.redact
        or args.redact_usernames
        or args.confirm_projects
    )
    if not has_changes:
        print(json.dumps(_mask_config_for_display(load_config()), indent=2))
        return
    configure(
        repo=args.repo,
        source=args.source,
        exclude=_parse_csv_arg(args.exclude),
        redact=_parse_csv_arg(args.redact),
        redact_usernames=_parse_csv_arg(args.redact_usernames),
        confirm_projects=args.confirm_projects or bool(args.exclude),
    )


_PII_SESSION_WORKERS = 4


_PII_AI_SESSION_CAP = 10


def _collect_pii_findings(sessions: list[dict[str, Any]], provider: str, rubric: str | None = None, backend: str = "auto", limit_sessions: int | None = None) -> list[dict[str, Any]]:
    """Dispatch PII review to the requested provider and return raw findings."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    provider = _normalize_pii_provider(provider)

    if provider == "rules":
        findings: list[dict[str, Any]] = []
        for session in sessions:
            findings.extend(review_session_pii(session))
        return findings

    # Cap AI review sessions — AI calls are expensive
    cap = limit_sessions if limit_sessions is not None else _PII_AI_SESSION_CAP
    if len(sessions) > cap:
        print(f"  AI PII review limited to {cap} sessions (of {len(sessions)}). Use --limit-sessions to override.", file=sys.stderr)
        sessions = sessions[:cap]

    total = len(sessions)

    def _review_one(session: dict[str, Any]) -> list[dict[str, Any]]:
        if provider == "ai":
            return review_session_pii_with_agent(session, backend=backend, rubric=rubric)
        return review_session_pii_hybrid(session, ignore_llm_errors=True, rubric=rubric, backend=backend)

    # Progress counter (thread-safe)
    counter = threading.Lock()
    done_count = 0

    findings = []
    workers = min(_PII_SESSION_WORKERS, total)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        future_to_sid = {}
        for session in sessions:
            sid = str(session.get("session_id") or "?")[:12]
            future_to_sid[pool.submit(_review_one, session)] = sid

        for future in as_completed(future_to_sid):
            sid = future_to_sid[future]
            with counter:
                done_count += 1
                print(f"  [{done_count}/{total}] Completed session {sid}...", file=sys.stderr)
            try:
                findings.extend(future.result())
            except RuntimeError as exc:
                print(f"  Warning: session {sid} failed: {exc}", file=sys.stderr)

    return findings


def _run_pii_review(args) -> None:
    sessions = load_jsonl_sessions(args.file)
    limit = args.limit_sessions
    if limit is not None:
        sessions = sessions[:limit]

    rubric = None
    rubric_file = getattr(args, "rubric_file", None)
    if rubric_file is not None:
        rubric = Path(rubric_file).read_text(encoding="utf-8")

    backend = getattr(args, "backend", "auto") or "auto"
    provider = _normalize_pii_provider(args.provider)
    # When --limit-sessions is set explicitly, pass it as the cap so the
    # internal AI cap doesn't re-truncate the user's explicit choice.
    findings = _collect_pii_findings(sessions, provider, rubric=rubric, backend=backend, limit_sessions=limit)
    filtered = [f for f in findings if float(f.get("confidence", 0.0)) >= args.min_confidence]
    write_findings(args.output, filtered, meta={
        "provider": provider,
        "session_count": len(sessions),
        "finding_count": len(filtered),
    })
    summary = {
        "ok": True,
        "provider": provider,
        "backend": backend,
        "file": str(args.file),
        "output": str(args.output),
        "session_count": len(sessions),
        "finding_count": len(filtered),
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"PII review complete: {len(filtered)} findings from {len(sessions)} sessions")
    print(f"Findings written to {args.output}")


def _run_pii_apply(args) -> None:
    sessions = load_jsonl_sessions(args.file)
    findings = load_findings(args.findings)
    total_replacements = 0
    out_sessions = []
    for session in sessions:
        redacted, count = apply_findings_to_session(session, findings, min_confidence=args.min_confidence)
        out_sessions.append(redacted)
        total_replacements += count
    write_jsonl_sessions(args.output, out_sessions)
    summary = {
        "ok": True,
        "file": str(args.file),
        "findings": str(args.findings),
        "output": str(args.output),
        "session_count": len(out_sessions),
        "replacements": total_replacements,
    }
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"PII apply complete: {total_replacements} replacements across {len(out_sessions)} sessions")
    print(f"Sanitized output written to {args.output}")


def _run_training_format(args) -> None:
    from .export.training_data import convert_sessions_to_training
    sessions = load_jsonl_sessions(args.file)
    summary = convert_sessions_to_training(sessions, args.output)
    summary["ok"] = True
    summary["file"] = str(args.file)
    if args.json:
        print(json.dumps(summary, indent=2))
        return
    print(f"Training format: {summary['turns']} turns from {summary['sessions']} sessions")
    print(f"Output written to {args.output}")


def _run_events(args) -> None:
    from .events import capabilities_json, ingest_pending
    from .workbench.index import open_index

    if args.events_command == "capabilities":
        print(json.dumps(capabilities_json(), indent=2, sort_keys=True))
        return

    if args.events_command == "inspect":
        _run_events_inspect(args)
        return

    if args.events_command == "cost":
        _run_events_cost(args)
        return

    if args.events_command == "incidents":
        _run_events_incidents(args)
        return

    if args.events_command == "export":
        _run_events_export(args)
        return

    if args.events_command == "import":
        _run_events_import(args)
        return

    if args.events_command == "doctor":
        _run_events_doctor(args)
        return

    if args.events_command == "features":
        _run_events_features(args)
        return

    if args.events_command == "docs":
        _run_events_docs(args)
        return

    if args.events_command == "aggregate":
        _run_events_aggregate(args)
        return

    if args.events_command == "search":
        _run_events_search(args)
        return

    conn = open_index()
    try:
        summary = ingest_pending(conn, source_filter=args.source)
    finally:
        conn.close()

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        f"Ingested {payload['event_rows']} event rows from "
        f"{payload['files_with_changes']} changed files "
        f"({payload['lines_read']} lines, {payload['sessions_touched']} sessions)."
    )


def _run_events_cost(args) -> None:
    from .events.cost import ingest_cost_pending
    from .workbench.index import open_index

    if args.cost_command == "aggregate":
        _run_events_cost_aggregate(args)
        return

    if args.cost_command != "ingest":
        print(f"Unknown cost command: {args.cost_command}", file=sys.stderr)
        sys.exit(2)

    conn = open_index()
    try:
        summary = ingest_cost_pending(conn, rebuild=args.rebuild)
    finally:
        conn.close()

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        f"Cost ledger: scanned {payload['events_scanned']} events, "
        f"wrote {payload['token_rows_written']} token_usage rows + "
        f"{payload['anomalies_written']} anomalies "
        f"across {payload['sessions_touched']} sessions."
    )


def _run_events_incidents(args) -> None:
    from .events.incidents import ingest_loop_incidents
    from .workbench.index import open_index

    if args.incidents_command == "aggregate":
        _run_events_incidents_aggregate(args)
        return

    if args.incidents_command != "detect":
        print(f"Unknown incidents command: {args.incidents_command}", file=sys.stderr)
        sys.exit(2)

    conn = open_index()
    try:
        summary = ingest_loop_incidents(conn, rebuild=args.rebuild)
    finally:
        conn.close()

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(
        f"Loop detector: scanned {payload['events_scanned']} new events, "
        f"evaluated {payload['sessions_evaluated']} sessions, "
        f"wrote {payload['incidents_written']} loop incidents "
        f"across {payload['sessions_touched']} sessions."
    )


def _run_events_export(args) -> None:
    from .events.export import (
        ExportError,
        ExportGateBlocked,
        export_session_bundle,
    )
    from .workbench.index import open_index

    output_path = Path(args.output) if args.output else None

    conn = open_index()
    try:
        try:
            summary = export_session_bundle(
                conn,
                args.session_key,
                output_path=output_path,
                include_snippets=not args.no_snippets,
                include_children=not args.no_children,
                allow_no_workbench_row=args.allow_no_workbench_row,
                pretty=args.pretty,
            )
        except ExportGateBlocked as exc:
            print(exc.message, file=sys.stderr)
            sys.exit(exc.exit_code)
        except ExportError as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        if summary.blocked:
            print(
                f"Export BLOCKED ({summary.block_reason}): "
                f"{summary.bundle_path} (manifest-only diagnostic artifact)"
            )
        else:
            print(
                f"Wrote {summary.bundle_path} "
                f"({summary.event_count} events, "
                f"{summary.override_count} overrides, "
                f"{summary.token_usage_count} token_usage rows, "
                f"{summary.incident_count} incidents, "
                f"{summary.snippet_count} snippets, "
                f"sha256={summary.sha256[:12] if summary.sha256 else '?'})"
            )

    if summary.blocked:
        sys.exit(2)


def _run_events_import(args) -> None:
    from .events.export import ImportError_, import_session_bundle
    from .workbench.index import open_index

    conn = open_index()
    try:
        try:
            summary = import_session_bundle(
                conn,
                args.bundle_path,
                rebuild_derived=args.rebuild_derived,
            )
        except ImportError_ as exc:
            print(str(exc), file=sys.stderr)
            sys.exit(1)
    finally:
        conn.close()

    payload = summary.to_dict()
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(
            f"Imported {summary.events_inserted} events "
            f"(skipped {summary.events_skipped_existing} existing), "
            f"{summary.overrides_inserted} overrides, "
            f"{summary.token_usage_inserted} token_usage, "
            f"{summary.cost_anomalies_inserted} cost_anomalies, "
            f"{summary.incidents_inserted} incidents, "
            f"{summary.snippets_inserted} snippets across "
            f"{len(summary.session_keys)} session(s)."
        )
        unresolved = (
            summary.token_usage_skipped_unresolved
            + summary.cost_anomalies_skipped_unresolved
            + summary.incidents_skipped_unresolved
        )
        if unresolved:
            print(
                f"  (dropped {summary.token_usage_skipped_unresolved} token_usage, "
                f"{summary.cost_anomalies_skipped_unresolved} cost_anomalies, "
                f"{summary.incidents_skipped_unresolved} incidents with unresolvable "
                f"cross-references — typically from events that already exist under a "
                f"different session)",
                file=sys.stderr,
            )


def _run_events_doctor(args) -> None:
    from .events.doctor import (
        collect,
        exit_code_for,
        fix_additive_drift,
        render_human,
        render_json,
    )
    from .events.doctor.envelope import KIND_UNSPECIFIED, emit_error

    request_id = getattr(args, "request_id", None)
    try:
        report = collect()

        if getattr(args, "fix", False):
            result = fix_additive_drift(report)
            if result["added"]:
                sys.stderr.write(
                    f"events doctor --fix: wrote {len(result['added'])} additive "
                    f"entries to {result['path']}\n"
                )
                for entry in result["added"]:
                    sys.stderr.write(
                        f"  + {entry['client']}/{entry['event_type']}: "
                        f"{entry['reason']}\n"
                    )
            else:
                sys.stderr.write(
                    "events doctor --fix: no additive drift detected\n"
                )
            if result["skipped_structural"]:
                sys.stderr.write(
                    f"events doctor --fix: refusing structural drift "
                    f"({len(result['skipped_structural'])} unknown event type(s)); "
                    f"file an issue\n"
                )
                for entry in result["skipped_structural"]:
                    sys.stderr.write(
                        f"  ! {entry['client']}/{entry['event_type']} "
                        f"(observed in {entry['client_version']!r})\n"
                    )
            # Re-collect after overlay write so the post-fix report reflects
            # the new effective_matrix() state.
            report = collect()

        if args.json:
            text = render_json(report, request_id=request_id)
            sys.stdout.write(text)
            sys.stdout.write("\n")
        else:
            sys.stdout.write(render_human(report))
    except Exception as exc:
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"events doctor failed: {exc!r}",
                hint="re-run with --json for a structured report",
                request_id=request_id,
                json_mode=bool(args.json),
            )
        )
    sys.exit(exit_code_for(report))


def _run_events_features(args) -> None:
    from .events.doctor.envelope import KIND_UNSPECIFIED, emit_error
    from .events.doctor.features import features_payload

    request_id = getattr(args, "request_id", None)
    try:
        payload = features_payload(request_id=request_id)
    except Exception as exc:
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"events features failed: {exc!r}",
                hint="check that the wheel ships clawjournal/events/docs/_features.yaml",
                request_id=request_id,
                json_mode=True,
            )
        )
    print(json.dumps(payload, indent=2, sort_keys=True))


def _run_events_docs(args) -> None:
    from .events.doctor.docs import (
        TOPIC_NAMES,
        DocsTopicError,
        render_topic,
    )
    from .events.doctor.envelope import (
        KIND_TOPIC_UNKNOWN,
        KIND_USAGE_ERROR,
        emit_error,
    )

    topic = getattr(args, "topic", None)
    request_id = getattr(args, "request_id", None)
    json_mode = bool(args.json)

    if not topic:
        sys.exit(
            emit_error(
                code=2,
                kind=KIND_USAGE_ERROR,
                message="missing required argument: topic",
                hint=f"valid topics: {', '.join(TOPIC_NAMES)}",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        rendered = render_topic(topic, json_mode=json_mode, request_id=request_id)
    except DocsTopicError as exc:
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_TOPIC_UNKNOWN,
                message=str(exc),
                hint=f"valid topics: {', '.join(TOPIC_NAMES)}",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    print(rendered)


def _add_aggregate_flags(parser, *, domain: str) -> None:
    """Register the shared --by/--metric/--where/--since/--limit/--json/--request-id
    flags on an aggregation subparser. ``domain`` is stashed on the
    namespace so the handler doesn't have to disambiguate by parser.
    """

    parser.add_argument(
        "--by",
        action="append",
        default=[],
        metavar="DIM[,DIM...]",
        help="Dimension(s) to group by (repeat or comma-separate; up to 3 total)",
    )
    parser.add_argument(
        "--metric",
        action="append",
        default=[],
        metavar="METRIC",
        help="Metric: count | sum:<field> | avg:<field> "
             "(repeat or comma-separate for multi-metric)",
    )
    parser.add_argument(
        "--where",
        action="append",
        default=[],
        metavar="FIELD<op>VALUE",
        help="Allowlisted filter (op: =, !=, >, >=, <, <=, in:v1|v2|...). "
             "Repeat to AND together",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="DURATION",
        help="Lower-bound time filter: Nd / Nh / Nm / today / thisweek",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        metavar="N",
        help="Top-N buckets to return (default: 10, ceiling: 1000)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Emit JSON instead of human table"
    )
    parser.add_argument(
        "--request-id",
        dest="request_id",
        help="Opaque ID echoed into _meta.request_id when --json is set",
    )
    parser.set_defaults(_aggregate_domain=domain)


def _run_events_aggregate(args) -> None:
    _run_aggregate(args, domain="events")


def _run_events_cost_aggregate(args) -> None:
    _run_aggregate(args, domain="cost")


def _run_events_incidents_aggregate(args) -> None:
    _run_aggregate(args, domain="incidents")


def _run_aggregate(args, *, domain: str) -> None:
    """Shared entry point for the three aggregation subcommands.

    Builds a validated ``AggregationSpec`` from CLI args, opens the
    workbench index, executes the query, and renders. Errors flow
    through the structured error envelope (plan 08).
    """

    import sqlite3

    from .events.aggregate import (
        AggregationSpec,
        DEFAULT_LIMIT,
        Metric,
        get_registry,
        parse_since,
        parse_where_clauses,
        render_human,
        render_json,
        run as run_aggregation,
    )
    from .events.doctor.envelope import (
        KIND_INDEX_MISSING,
        KIND_UNSPECIFIED,
        KIND_USAGE_ERROR,
        emit_error,
    )
    from .workbench.index import open_index

    request_id = getattr(args, "request_id", None)
    json_mode = bool(getattr(args, "json", False))

    try:
        registry = get_registry(domain)
        dimensions = _split_csv_flag(args.by)
        if not dimensions:
            sys.exit(
                emit_error(
                    code=2,
                    kind=KIND_USAGE_ERROR,
                    message="missing required flag: --by",
                    hint=f"available dimensions for {domain!r}: "
                         f"{', '.join(sorted(registry.dimensions))}",
                    request_id=request_id,
                    json_mode=json_mode,
                )
            )
        for dim in dimensions:
            if dim not in registry.dimensions:
                sys.exit(
                    emit_error(
                        code=2,
                        kind=KIND_USAGE_ERROR,
                        message=f"unknown dimension: {dim!r}",
                        hint=f"available dimensions for {domain!r}: "
                             f"{', '.join(sorted(registry.dimensions))}",
                        request_id=request_id,
                        json_mode=json_mode,
                    )
                )

        metric_tokens = _split_csv_flag(args.metric) or ["count"]
        metrics = tuple(_parse_metric_token(t, registry) for t in metric_tokens)

        filters = parse_where_clauses(list(args.where), registry)
        since_iso = parse_since(args.since)
        limit = args.limit if args.limit is not None else DEFAULT_LIMIT
        canonical = bool(getattr(args, "canonical", False))
        if canonical:
            # Plan 10 specifies --canonical but the wire-up to
            # canonical_events() is deferred. Refuse loudly so users
            # don't get raw-event results when they asked for deduped.
            raise ValueError(
                "--canonical is not yet wired in v0.1; it will route the "
                "aggregation through canonical_events() in a follow-up. "
                "Drop the flag to aggregate over raw events."
            )

        # Plan 10 §Time windows: --since and a `--where <time_field>
        # >=...` filter are mutually exclusive — otherwise the user
        # silently gets the AND of both bounds. Reject the combination
        # so they pick one. Other operators on the time field (`<`,
        # `=`, `<=`) are still allowed alongside --since because they
        # bound the *upper* end / pin a specific point and don't
        # conflict with --since's lower bound.
        time_field_logical_name = registry.time_field.split(".", 1)[-1]
        if since_iso is not None and any(
            p.field == time_field_logical_name and p.op in (">=", ">")
            for p in filters
        ):
            raise ValueError(
                f"--since and --where {time_field_logical_name}>=... / >... "
                f"are mutually exclusive (both bound the same lower edge of "
                f"the time window). Drop one."
            )

        spec = AggregationSpec(
            domain=domain,
            dimensions=tuple(dimensions),
            metrics=metrics,
            filters=filters,
            since_iso=since_iso,
            limit=limit,
            canonical=canonical,
        )
    except ValueError as exc:
        sys.exit(
            emit_error(
                code=2,
                kind=KIND_USAGE_ERROR,
                message=str(exc),
                hint=f"see `clawjournal events docs commands` for "
                     f"{domain!r} aggregation reference",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        conn = open_index()
    except FileNotFoundError as exc:
        sys.exit(
            emit_error(
                code=3,
                kind=KIND_INDEX_MISSING,
                message=f"index database not found: {exc}",
                hint="run `clawjournal scan` then `clawjournal events ingest`",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        result = run_aggregation(spec, conn)
    except ValueError as exc:
        # Auto-partition refusing to drop a user-supplied --by dim
        # (cost domain), or a metric/filter field rejected by the
        # registry. These are usage errors, not catch-all faults.
        conn.close()
        sys.exit(
            emit_error(
                code=2,
                kind=KIND_USAGE_ERROR,
                message=str(exc),
                hint=f"see `clawjournal events docs commands` for "
                     f"{domain!r} aggregation reference",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    except sqlite3.OperationalError as exc:
        # The most common shape here is "no such table: events" /
        # "no such table: incidents" / "no such table: token_usage" —
        # the user hasn't run `events ingest` (or `cost ingest` /
        # `incidents detect`) yet for this domain. Map to
        # index_missing so agents see exit code 3 and a directed
        # hint, rather than the unspecified catch-all.
        conn.close()
        msg = str(exc)
        if "no such table" in msg:
            domain_hint = {
                "events": "run `clawjournal events ingest` to populate the "
                          "events tables",
                "incidents": "run `clawjournal events incidents detect` to "
                             "populate the incidents table",
                "cost": "run `clawjournal events cost ingest` to populate "
                        "token_usage / cost_anomalies",
            }.get(
                domain,
                "run the relevant `clawjournal events ...` ingest command",
            )
            sys.exit(
                emit_error(
                    code=3,
                    kind=KIND_INDEX_MISSING,
                    message=msg,
                    hint=domain_hint,
                    request_id=request_id,
                    json_mode=json_mode,
                )
            )
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"aggregation failed: {msg}",
                hint="re-run with --json for the structured payload",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    except Exception as exc:
        conn.close()
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"aggregation failed: {exc!r}",
                hint="re-run with --json for the structured payload",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    conn.close()

    if json_mode:
        sys.stdout.write(render_json(result, request_id=request_id))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(result))


def _split_csv_flag(values: list[str]) -> list[str]:
    """Combine repeated flags + comma-separated tokens into a flat list."""

    out: list[str] = []
    for v in values or []:
        for token in v.split(","):
            token = token.strip()
            if token:
                out.append(token)
    return out


def _parse_metric_token(token: str, registry) -> "Metric":  # type: ignore[name-defined]
    """Parse one ``count`` / ``sum:field`` / ``avg:field`` token."""

    from .events.aggregate import Metric

    if token == "count":
        return Metric(kind="count")
    if ":" in token:
        kind, field = token.split(":", 1)
        kind = kind.strip()
        field = field.strip()
        if kind in ("sum", "avg"):
            return Metric(kind=kind, field=field)
    raise ValueError(
        f"unrecognized metric: {token!r} "
        f"(expected count | sum:<field> | avg:<field>)"
    )


def _run_events_search(args) -> None:
    """Cross-session FTS over events.raw_json (plan 11).

    Wraps schema bootstrap, validation, query, and render. Errors flow
    through plan 08's structured envelope: usage_error → 2,
    index_missing → 3 (FTS table absent or events table absent),
    unspecified → 9.
    """

    import sqlite3

    from .events.aggregate import parse_since
    from .events.doctor.envelope import (
        KIND_INDEX_MISSING,
        KIND_UNSPECIFIED,
        KIND_USAGE_ERROR,
        emit_error,
    )
    from .events.search import (
        DEFAULT_LIMIT,
        DEFAULT_SNIPPET_TOKENS,
        ensure_search_schema,
        parse_search_spec,
        rebuild_search_index,
        render_human,
        render_json,
        run as run_search,
    )
    from .workbench.index import open_index

    request_id = getattr(args, "request_id", None)
    json_mode = bool(getattr(args, "json", False))

    rebuild_only = bool(getattr(args, "rebuild_index", False))
    if not rebuild_only and not args.query:
        sys.exit(
            emit_error(
                code=2,
                kind=KIND_USAGE_ERROR,
                message="missing required positional: <query>",
                hint="pass an FTS5 MATCH expression, or --rebuild-index to "
                     "reindex the FTS table",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        conn = open_index()
    except FileNotFoundError as exc:
        sys.exit(
            emit_error(
                code=3,
                kind=KIND_INDEX_MISSING,
                message=f"index database not found: {exc}",
                hint="run `clawjournal scan` then `clawjournal events ingest`",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        ensure_search_schema(conn)
    except sqlite3.OperationalError as exc:
        # Most likely "no such table: events" — workbench DB exists
        # but events ingest never ran. Map to index_missing.
        conn.close()
        msg = str(exc)
        if "no such table" in msg:
            sys.exit(
                emit_error(
                    code=3,
                    kind=KIND_INDEX_MISSING,
                    message=msg,
                    hint="run `clawjournal events ingest` to populate the "
                         "events tables before searching",
                    request_id=request_id,
                    json_mode=json_mode,
                )
            )
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"could not initialize FTS schema: {msg}",
                hint="re-run with --json for the structured payload",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    if rebuild_only:
        try:
            rebuild_search_index(conn)
            conn.commit()
        except sqlite3.OperationalError as exc:
            conn.close()
            sys.exit(
                emit_error(
                    code=9,
                    kind=KIND_UNSPECIFIED,
                    message=f"FTS rebuild failed: {exc}",
                    hint="check `~/.clawjournal/index.db` is writable",
                    request_id=request_id,
                    json_mode=json_mode,
                )
            )
        # Capture indexed-doc count post-rebuild so JSON consumers
        # can verify the rebuild actually populated the index.
        indexed = conn.execute(
            "SELECT count(*) FROM events_fts_docsize"
        ).fetchone()[0]
        conn.close()
        if json_mode:
            from .events.search import EVENTS_SEARCH_SCHEMA_VERSION
            payload: dict[str, Any] = {
                "events_search_schema_version": EVENTS_SEARCH_SCHEMA_VERSION,
                "rebuild": "ok",
                "indexed_documents": int(indexed),
            }
            if request_id is not None:
                payload["_meta"] = {"request_id": request_id}
            sys.stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        else:
            sys.stdout.write(
                f"events_fts rebuild complete ({indexed} documents indexed)\n"
            )
        return

    try:
        clients = tuple(_split_csv_flag(args.client))
        types = tuple(_split_csv_flag(args.event_type))
        confidences = tuple(_split_csv_flag(args.confidence))
        sources = tuple(_split_csv_flag(args.source))
        since_iso = parse_since(args.since)
        spec = parse_search_spec(
            query=args.query,
            client=clients,
            type_=types,
            confidence=confidences,
            session=args.session,
            source=sources,
            since_iso=since_iso,
            limit=args.limit if args.limit is not None else DEFAULT_LIMIT,
            snippet_tokens=(
                args.snippet_tokens if args.snippet_tokens is not None
                else DEFAULT_SNIPPET_TOKENS
            ),
            include_held=bool(args.include_held),
        )
    except ValueError as exc:
        conn.close()
        sys.exit(
            emit_error(
                code=2,
                kind=KIND_USAGE_ERROR,
                message=str(exc),
                hint="see `clawjournal events docs commands` for events search "
                     "reference",
                request_id=request_id,
                json_mode=json_mode,
            )
        )

    try:
        result = run_search(spec, conn)
    except sqlite3.OperationalError as exc:
        conn.close()
        msg = str(exc)
        # FTS5 raises OperationalError on a malformed MATCH expression.
        # Common shapes: "fts5: syntax error", "unterminated string",
        # "no such column: <X>" (FTS5 treats `foo-bar` as a column
        # filter `foo` ⇒ `bar`; users who typed a hyphen-bareword
        # actually want the phrase query `"foo-bar"`). Schema and
        # filter shape are validated upstream, so any OperationalError
        # here is almost certainly query-shape — map to usage_error
        # so agents see exit code 2 rather than the catch-all 9.
        # Schema-level OperationalError ("no such table: events_fts")
        # was already caught by ensure_search_schema above.
        lowered = msg.lower()
        looks_like_user_query_error = any(
            tok in lowered
            for tok in (
                "fts5",
                "syntax error",
                "unterminated",
                " near ",
                "no such column",
            )
        )
        if looks_like_user_query_error:
            sys.exit(
                emit_error(
                    code=2,
                    kind=KIND_USAGE_ERROR,
                    message=f"FTS5 rejected query: {msg}",
                    hint="quote phrase queries (\"foo bar\"), use prefix as "
                         "foo*, escape literal punctuation",
                    request_id=request_id,
                    json_mode=json_mode,
                )
            )
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"search failed: {msg}",
                hint="re-run with --json for the structured payload",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    except Exception as exc:
        conn.close()
        sys.exit(
            emit_error(
                code=9,
                kind=KIND_UNSPECIFIED,
                message=f"search failed: {exc!r}",
                hint="re-run with --json for the structured payload",
                request_id=request_id,
                json_mode=json_mode,
            )
        )
    conn.close()

    if json_mode:
        sys.stdout.write(render_json(result, request_id=request_id))
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_human(result))


_INSPECT_HUMAN_DEFAULT_TRUNCATE = 1024


def _run_events_inspect(args) -> None:
    from .events import CAPABILITY_MATRIX, ensure_view_schema
    from .events.schema import ensure_schema as ensure_events_schema
    from .workbench.index import open_index

    has_id = args.event_id is not None
    has_sk_ek = args.session is not None and args.event_key is not None
    if not (has_id ^ has_sk_ek):
        print(
            "inspect requires either <event-id> OR both --session and --event-key",
            file=sys.stderr,
        )
        sys.exit(2)

    conn = open_index()
    try:
        ensure_events_schema(conn)
        ensure_view_schema(conn)
        if has_id:
            payload = _inspect_by_event_id(conn, args.event_id)
        else:
            payload = _inspect_by_session_event_key(conn, args.session, args.event_key)

        if payload is None:
            print("event not found", file=sys.stderr)
            sys.exit(1)

        payload["capability"] = _capability_reason(
            CAPABILITY_MATRIX, payload["client"], payload["type"]
        )
        if payload.get("raw_ref") is not None:
            source_path, source_offset, _seq = payload["raw_ref"]
            payload["vendor_line"] = _resolve_vendor_line_text(
                conn, source_path, source_offset, payload["raw_ref"][2]
            )
        else:
            payload["vendor_line"] = None
    finally:
        conn.close()

    _apply_inspect_truncation(payload, _effective_truncate(args))

    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return

    _print_inspect_human(payload)


def _effective_truncate(args) -> int | None:
    """Return the truncation limit in chars, or None for unlimited.

    - `--truncate 0` means unlimited (explicit opt-out).
    - `--truncate N` (N>0) always truncates.
    - No flag: human mode defaults to 1024; --json defaults to unlimited.
    """
    if args.truncate is not None:
        return None if args.truncate <= 0 else args.truncate
    return None if args.json else _INSPECT_HUMAN_DEFAULT_TRUNCATE


def _apply_inspect_truncation(payload: dict, limit: int | None) -> None:
    if limit is None:
        return
    payload["raw_json"] = _truncate_str(payload.get("raw_json"), limit)
    override = payload.get("override")
    if override is not None:
        override["payload_json"] = _truncate_str(override.get("payload_json"), limit)
    payload["vendor_line"] = _truncate_str(payload.get("vendor_line"), limit)


def _truncate_str(value, limit: int):
    if not isinstance(value, str) or len(value) <= limit:
        return value
    return value[:limit] + f" … [truncated at {limit} of {len(value)} chars]"


def _inspect_by_event_id(conn, event_id: int):
    row = conn.execute(
        """
        SELECT events.*, event_sessions.session_key, event_sessions.client
          FROM events
          JOIN event_sessions ON event_sessions.id = events.session_id
         WHERE events.id = ?
        """,
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    base = dict(row)
    override = _load_override(conn, base["session_id"], base["event_key"])
    return _payload_for_base_and_override(base, override)


def _inspect_by_session_event_key(conn, session_key: str, event_key: str):
    session_row = conn.execute(
        "SELECT id, session_key, client FROM event_sessions WHERE session_key = ?",
        (session_key,),
    ).fetchone()
    if session_row is None:
        return None
    session_id = int(session_row["id"])
    client = session_row["client"]

    # Prefer base row if one exists for this event_key; otherwise hook-only.
    base_row = conn.execute(
        """
        SELECT *
          FROM events
         WHERE session_id = ? AND event_key = ?
         ORDER BY event_at IS NULL, event_at, source_path, source_offset, seq
         LIMIT 1
        """,
        (session_id, event_key),
    ).fetchone()
    override = _load_override(conn, session_id, event_key)

    if base_row is None and override is None:
        return None

    if base_row is None:
        # hook-only
        return {
            "id": None,
            "session_id": session_id,
            "session_key": session_key,
            "client": client,
            "type": override["type"],
            "event_key": override["event_key"],
            "event_at": override["event_at"],
            "ingested_at": None,
            "source": override["source"],
            "confidence": override["confidence"],
            "lossiness": override["lossiness"],
            "raw_json": None,
            "raw_ref": None,
            "override": _override_payload(override),
            "hook_only": True,
        }

    base = dict(base_row)
    base["session_key"] = session_key
    base["client"] = client
    return _payload_for_base_and_override(base, override)


def _load_override(conn, session_id: int, event_key):
    if event_key is None:
        return None
    row = conn.execute(
        """
        SELECT event_key, type, source, confidence, lossiness,
               event_at, payload_json, origin, created_at
          FROM event_overrides
         WHERE session_id = ? AND event_key = ?
        """,
        (session_id, event_key),
    ).fetchone()
    return dict(row) if row is not None else None


def _override_payload(override: dict) -> dict:
    return {
        "source": override["source"],
        "confidence": override["confidence"],
        "lossiness": override["lossiness"],
        "event_at": override["event_at"],
        "payload_json": override["payload_json"],
        "origin": override["origin"],
        "created_at": override["created_at"],
    }


def _payload_for_base_and_override(base: dict, override):
    override_applied = override is not None and _override_wins_base(base, override)
    payload = {
        "id": base["id"],
        "session_id": base["session_id"],
        "session_key": base["session_key"],
        "client": base["client"],
        "type": override["type"] if override_applied else base["type"],
        "event_key": base["event_key"],
        "event_at": override["event_at"] if override_applied else base["event_at"],
        "ingested_at": base["ingested_at"],
        "source": override["source"] if override_applied else base["source"],
        "confidence": override["confidence"] if override_applied else base["confidence"],
        "lossiness": override["lossiness"] if override_applied else base["lossiness"],
        "raw_json": base["raw_json"],
        "raw_ref": [base["source_path"], base["source_offset"], base["seq"]],
        "override": _override_payload(override) if override is not None else None,
        "hook_only": False,
    }
    return payload


def _override_wins_base(base: dict, override: dict) -> bool:
    from .events import CONFIDENCE_RANK

    return (
        CONFIDENCE_RANK.get(override["confidence"], 0)
        >= CONFIDENCE_RANK.get(base["confidence"], 0)
    )


def _capability_reason(matrix, client: str, event_type: str) -> str:
    supported, reason = matrix.get(
        (client, event_type), (False, "not emitted by this client")
    )
    prefix = "emitted" if supported else "NOT emitted"
    return f"{prefix} by {client} ({reason})"


def _resolve_vendor_line_text(conn, source_path: str, source_offset: int, seq: int):
    # First try a bundle-imported snippet store if it exists; 07 will
    # own the table, so tolerate the table being absent today.
    import sqlite3 as _sqlite3
    try:
        row = conn.execute(
            """
            SELECT text FROM event_source_snippets
             WHERE source_path = ? AND source_offset = ? AND seq = ?
            """,
            (source_path, source_offset, seq),
        ).fetchone()
        if row is not None:
            return row[0]
    except _sqlite3.OperationalError:
        # Table doesn't exist yet (07 owns it); fall through.
        pass
    from .events import fetch_vendor_line
    return fetch_vendor_line(source_path, source_offset)


def _print_inspect_human(payload: dict) -> None:
    ev_id = payload["id"]
    label = f"#{ev_id}" if ev_id is not None else "(hook-only)"
    print(f"event {label}  type={payload['type']}  event_key={payload['event_key']}")
    print(f"session    {payload['session_key']} (client={payload['client']})")
    print(f"source     {payload['source']}")
    print(f"confidence {payload['confidence']:<10}          lossiness={payload['lossiness']}")
    event_at = payload["event_at"] or "(none)"
    ingested_at = payload["ingested_at"] or "(n/a)"
    print(f"event_at   {event_at:<24} ingested_at={ingested_at}")
    raw_ref = payload.get("raw_ref")
    if raw_ref is None:
        print("raw_ref    (no vendor base — hook-originated)")
    else:
        print(f"raw_ref    {raw_ref[0]}:{raw_ref[1]}:{raw_ref[2]}")
    print()

    override = payload.get("override")
    if override is None:
        print("override   (none)")
    else:
        print(
            f"override   source={override['source']} confidence={override['confidence']} "
            f"origin={override['origin']}"
        )
    print(f"capability {payload['capability']}")
    print()

    raw_json = payload.get("raw_json")
    if raw_json is not None:
        print("raw_json:")
        _print_indented_text(raw_json)
        print()

    if override is not None:
        print("override payload:")
        _print_indented_text(override["payload_json"])
        print()

    if payload.get("hook_only"):
        print("vendor line:")
        print("  (no vendor base — hook-originated)")
        return

    print("vendor line:")
    vendor_line = payload.get("vendor_line")
    if vendor_line is None:
        print("  (source file no longer on disk)")
    else:
        print(f"  {vendor_line}")


def _print_indented_text(text: str) -> None:
    lines = str(text).splitlines() or [""]
    for line in lines:
        print(f"  {line}")


def _generate_pii_findings(file_path: Path, output_path: Path, provider: str, min_confidence: float = 0.0, backend: str = "auto") -> dict[str, Any]:
    provider = _normalize_pii_provider(provider)
    sessions = load_jsonl_sessions(file_path)
    findings = _collect_pii_findings(sessions, provider, backend=backend)
    filtered = [f for f in findings if float(f.get("confidence", 0.0)) >= min_confidence]
    write_findings(output_path, filtered, meta={
        "provider": provider,
        "session_count": len(sessions),
        "finding_count": len(filtered),
    })
    return {
        "provider": provider,
        "session_count": len(sessions),
        "finding_count": len(filtered),
        "output": str(output_path),
    }


def _apply_pii_findings(file_path: Path, findings_path: Path, output_path: Path, min_confidence: float = 0.0) -> dict[str, Any]:
    sessions = load_jsonl_sessions(file_path)
    findings = load_findings(findings_path)
    total_replacements = 0
    out_sessions = []
    for session in sessions:
        redacted, count = apply_findings_to_session(session, findings, min_confidence=min_confidence)
        out_sessions.append(redacted)
        total_replacements += count
    write_jsonl_sessions(output_path, out_sessions)
    return {
        "session_count": len(out_sessions),
        "replacements": total_replacements,
        "output": str(output_path),
        "findings": str(findings_path),
    }


def _run_export(args) -> None:
    """Run the export flow — discover, anonymize, and export locally."""
    config = load_config()
    source_choice, source_explicit = _resolve_source_choice(args.source, config)
    source_filter = _normalize_source_filter(source_choice)

    if not source_explicit:
        print(json.dumps({
            "error": "Source scope is not confirmed yet.",
            "hint": (
                "Explicitly choose one source scope before exporting: "
                "`claude`, `codex`, `gemini`, or `all`."
            ),
            "required_action": (
                "Ask the user whether to export Claude Code, Codex, Gemini, or all. "
                "Then run `clawjournal config --source <claude|codex|gemini|all>` "
                "or pass `--source <claude|codex|gemini|all>` on the export command."
            ),
            "allowed_sources": sorted(EXPLICIT_SOURCE_CHOICES),
            "blocked_on_step": "Step 2/5",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "clawjournal config --source all",
        }, indent=2))
        sys.exit(1)

    print("=" * 50)
    print("  ClawJournal — Agent Trace Exporter")
    print("=" * 50)

    if not _has_session_sources(source_filter):
        if source_filter == "claude":
            print(f"Error: {CLAUDE_DIR} not found.", file=sys.stderr)
        elif source_filter == "codex":
            print(f"Error: {CODEX_DIR} not found.", file=sys.stderr)
        elif source_filter == "gemini":
            from .parsing.parser import GEMINI_DIR
            print(f"Error: {GEMINI_DIR} not found.", file=sys.stderr)
        else:
            print("Error: none of ~/.claude, ~/.codex, or ~/.gemini/tmp were found.", file=sys.stderr)
        sys.exit(1)

    projects = discover_projects(source_filter=source_filter)
    if not projects:
        print(f"No {_source_label(source_filter)} sessions found.", file=sys.stderr)
        sys.exit(1)

    if not args.all_projects and not config.get("projects_confirmed", False):
        excluded = set(normalize_excluded_project_names(config.get("excluded_projects", [])))
        list_command = f"clawjournal list --source {source_choice}"
        print(json.dumps({
            "error": "Project selection is not confirmed yet.",
            "hint": (
                f"Run `{list_command}`, present the full project list to the user, discuss which projects to exclude, then run "
                "`clawjournal config --exclude \"exact_project_name\"` or `clawjournal config --confirm-projects`."
            ),
            "required_action": (
                "Send the full project/folder list below to the user in a message and get explicit "
                "confirmation on exclusions before exporting."
            ),
            "projects": [
                {
                    "name": p["display_name"],
                    "source": p.get("source", "unknown"),
                    "sessions": p["session_count"],
                    "size": _format_size(p["total_size_bytes"]),
                    "excluded": p["display_name"] in excluded,
                }
                for p in projects
            ],
            "blocked_on_step": "Step 3/5",
            "process_steps": SETUP_TO_PUBLISH_STEPS,
            "next_command": "clawjournal config --confirm-projects",
        }, indent=2))
        sys.exit(1)

    total_sessions = sum(p["session_count"] for p in projects)
    total_size = sum(p["total_size_bytes"] for p in projects)
    print(f"\nFound {total_sessions} sessions across {len(projects)} projects "
          f"({_format_size(total_size)} raw)")
    print(f"Source scope: {source_choice}")

    # Apply exclusions
    excluded = set(normalize_excluded_project_names(config.get("excluded_projects", [])))
    if args.all_projects:
        excluded = set()

    included = [p for p in projects if p["display_name"] not in excluded]
    excluded_projects = [p for p in projects if p["display_name"] in excluded]

    if excluded_projects:
        print(f"\nIncluding {len(included)} projects (excluding {len(excluded_projects)}):")
    else:
        print(f"\nIncluding all {len(included)} projects:")
    for p in included:
        print(f"  + {p['display_name']} ({p['session_count']} sessions)")
    for p in excluded_projects:
        print(f"  - {p['display_name']} (excluded)")

    if not included:
        print("\nNo projects to export. Run: clawjournal config --exclude ''")
        sys.exit(1)

    # Build anonymizer with extra usernames from config
    extra_usernames = config.get("redact_usernames", [])
    anonymizer = Anonymizer(extra_usernames=extra_usernames)

    # Custom strings to redact
    custom_strings = config.get("redact_strings", [])

    if extra_usernames:
        print(f"\nAnonymizing usernames: {', '.join(extra_usernames)}")
    if custom_strings:
        print(f"Redacting custom strings: {len(custom_strings)} configured")

    # Handle markdown export formats
    export_format = getattr(args, "format", "jsonl")
    if export_format in ("md", "md-summary"):
        from .export.markdown import render_session_markdown, render_session_summary

        output_dir = args.output or Path("clawjournal_markdown_export")
        if output_dir.is_file():
            print(f"Error: --output must be a directory for markdown export, but '{output_dir}' is a file.", file=sys.stderr)
            sys.exit(1)
        output_dir.mkdir(parents=True, exist_ok=True)
        renderer = render_session_markdown if export_format == "md" else render_session_summary
        count = 0
        index_lines: list[str] = ["# ClawJournal Export\n"]

        for project in included:
            sessions = parse_project_sessions(
                project["dir_name"],
                anonymizer,
                not args.no_thinking,
                source=project.get("source", "claude"),
                locator=project.get("locator"),
            )
            for session in sessions:
                if not session.get("display_title"):
                    # Generate title from first user message
                    for m in session.get("messages", []):
                        if m.get("role") == "user":
                            c = m.get("content", "")
                            if isinstance(c, str) and c.strip():
                                session["display_title"] = c.strip().split("\n")[0][:120]
                                break
                    else:
                        session["display_title"] = session.get("session_id", "untitled")
                md_content = renderer(session)
                safe_id = session.get("session_id", f"session_{count}")[:64]
                safe_id = re.sub(r"[^\w\-]", "_", safe_id)
                md_path = output_dir / f"{safe_id}.md"
                md_path.write_text(md_content)
                title = session.get("display_title", safe_id)
                index_lines.append(f"- [{title}]({safe_id}.md)")
                count += 1

        (output_dir / "README.md").write_text("\n".join(index_lines) + "\n")
        print(f"\nExported {count} sessions as {'full markdown' if export_format == 'md' else 'summaries'}")
        print(f"Output directory: {output_dir}/")
        return

    # Export
    output_path = args.output or Path("clawjournal_conversations.jsonl")

    print(f"\nExporting to {output_path}...")
    meta = export_to_jsonl(
        included, output_path, anonymizer, not args.no_thinking,
        custom_strings=custom_strings,
    )
    file_size = output_path.stat().st_size
    print(f"\nExported {meta['sessions']} sessions ({_format_size(file_size)})")
    if meta.get("skipped"):
        print(f"Skipped {meta['skipped']} abandoned/error sessions")
    if meta.get("redactions"):
        print(f"Redacted {meta['redactions']} secrets (API keys, tokens, emails, etc.)")
    print(f"Models: {', '.join(f'{m} ({c})' for m, c in sorted(meta['models'].items(), key=lambda x: -x[1]))}")

    _print_pii_guidance(output_path)

    pii_review_summary = None
    pii_apply_summary = None
    if getattr(args, "pii_review", False):
        findings_output = args.pii_findings_output or output_path.with_suffix(output_path.suffix + ".pii_findings.json")
        pii_backend = getattr(args, "pii_backend", "auto") or "auto"
        print(f"\nGenerating structured PII findings ({args.pii_provider}, backend={pii_backend}) -> {findings_output}...")
        pii_review_summary = _generate_pii_findings(output_path, findings_output, args.pii_provider, backend=pii_backend)
        print(f"PII findings: {pii_review_summary['finding_count']} written to {findings_output}")
        if getattr(args, "pii_apply", False):
            sanitized_output = args.pii_sanitized_output or output_path.with_suffix(output_path.suffix + ".sanitized.jsonl")
            print(f"Applying PII findings -> {sanitized_output}...")
            pii_apply_summary = _apply_pii_findings(output_path, findings_output, sanitized_output)
            print(f"Sanitized JSONL: {pii_apply_summary['replacements']} replacements written to {sanitized_output}")

    config["last_export"] = {
        "timestamp": meta["exported_at"],
        "sessions": meta["sessions"],
        "models": meta["models"],
        "source": source_choice,
        "output_file": str(output_path.resolve()),
        "pii_review": pii_review_summary,
        "pii_apply": pii_apply_summary,
    }
    config["stage"] = "review"
    save_config(config)

    print(f"\nDone! JSONL file: {output_path}")
    abs_path = str(output_path.resolve())
    next_steps, next_command = _build_status_next_steps("review", config, None, None)
    json_block = {
        "stage": "review",
        "stage_number": 3,
        "total_stages": 4,
        "sessions": meta["sessions"],
        "source": source_choice,
        "output_file": abs_path,
        "pii_commands": _build_pii_commands(output_path),
        "pii_review": pii_review_summary,
        "pii_apply": pii_apply_summary,
        "next_steps": next_steps,
        "next_command": next_command,
    }
    print("\n---CLAWJOURNAL_JSON---")
    print(json.dumps(json_block, indent=2))


def _build_pii_commands(output_path: Path) -> list[str]:
    """Return grep commands for PII scanning."""
    p = str(output_path.resolve())
    return [
        f"grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {p} | grep -v noreply | head -20",
        f"grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {p} | head -5",
        f"grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {p} | head -5",
        f"grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {p} | sort -u",
    ]


def _print_pii_guidance(output_path: Path) -> None:
    """Print PII review guidance with concrete grep commands."""
    abs_output = output_path.resolve()
    print(f"\n{'=' * 50}")
    print("  IMPORTANT: Review your data before publishing!")
    print(f"{'=' * 50}")
    print("ClawJournal's automatic redaction is NOT foolproof.")
    print("You should scan the exported data for remaining PII.")
    print()
    print("Quick checks (run these and review any matches):")
    print(f"  grep -i 'your_name' {abs_output}")
    print(f"  grep -oE '[a-zA-Z0-9.+-]+@[a-zA-Z0-9.-]+\\.[a-z]{{2,}}' {abs_output} | grep -v noreply | head -20")
    print(f"  grep -oE 'eyJ[A-Za-z0-9_-]{{20,}}' {abs_output} | head -5")
    print(f"  grep -oE '(ghp_|sk-|hf_)[A-Za-z0-9_-]{{10,}}' {abs_output} | head -5")
    print(f"  grep -oE '[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}\\.[0-9]{{1,3}}' {abs_output} | sort -u")
    print()
    print("NEXT: Ask for full name to run an exact-name privacy check, then scan for it:")
    print(f"  grep -i 'THEIR_NAME' {abs_output} | head -10")
    print("  If user declines sharing full name: use clawjournal confirm --skip-full-name-scan with a skip attestation.")
    print()
    print("To add custom redactions, then re-export:")
    print("  clawjournal config --redact-usernames 'github_handle,discord_name'")
    print("  clawjournal config --redact 'secret-domain.com,my-api-key'")
    print(f"  clawjournal export -o {abs_output}")
    print()
    print(f"Found an issue? Help improve ClawJournal: {REPO_URL}/issues")


if __name__ == "__main__":
    main()
