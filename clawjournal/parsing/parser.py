"""Parse Claude Code, Claude Science, Codex, Gemini CLI, OpenCode, and OpenClaw session data into conversations."""

import dataclasses
import hashlib
import json
import logging
import platform
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..redaction.anonymizer import Anonymizer
from ..redaction.secrets import redact_text

logger = logging.getLogger(__name__)

CLAUDE_SOURCE = "claude"
CLAUDE_SCIENCE_SOURCE = "claude-science"
CODEX_SOURCE = "codex"
GEMINI_SOURCE = "gemini"
OPENCODE_SOURCE = "opencode"
OPENCLAW_SOURCE = "openclaw"
KIMI_SOURCE = "kimi"
CURSOR_SOURCE = "cursor"
COPILOT_SOURCE = "copilot"
AIDER_SOURCE = "aider"
CUSTOM_SOURCE = "custom"

CLAUDE_DIR = Path.home() / ".claude"
PROJECTS_DIR = CLAUDE_DIR / "projects"
CLAUDE_SCIENCE_DIR = Path.home() / ".claude-science"
CLAUDE_SCIENCE_DB_NAME = "operon-cli.db"

CODEX_DIR = Path.home() / ".codex"
CODEX_SESSIONS_DIR = CODEX_DIR / "sessions"
CODEX_ARCHIVED_DIR = CODEX_DIR / "archived_sessions"
UNKNOWN_CODEX_CWD = "<unknown-cwd>"

GEMINI_DIR = Path.home() / ".gemini" / "tmp"

OPENCODE_DIR = Path.home() / ".local" / "share" / "opencode"
OPENCODE_DB_PATH = OPENCODE_DIR / "opencode.db"
UNKNOWN_OPENCODE_CWD = "<unknown-cwd>"

OPENCLAW_DIR = Path.home() / ".openclaw"
OPENCLAW_AGENTS_DIR = OPENCLAW_DIR / "agents"
UNKNOWN_OPENCLAW_CWD = "<unknown-cwd>"

KIMI_DIR = Path.home() / ".kimi"
KIMI_SESSIONS_DIR = KIMI_DIR / "sessions"
KIMI_CONFIG_PATH = KIMI_DIR / "kimi.json"
UNKNOWN_KIMI_CWD = "<unknown-cwd>"

CURSOR_DIR = Path.home() / ".cursor"
COPILOT_DIR = Path.home() / ".copilot" / "session-state"
AIDER_HISTORY_FILENAME = ".aider.chat.history.md"

CUSTOM_DIR = Path.home() / ".clawjournal" / "custom"

# Claude Desktop local-agent-mode-sessions (macOS only for now)
LOCAL_AGENT_DIR = (
    Path.home() / "Library" / "Application Support" / "Claude" / "local-agent-mode-sessions"
    if platform.system() == "Darwin"
    else Path.home() / ".claude-desktop" / "local-agent-mode-sessions"
)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE)

# --- Field classification for tool input anonymization ---
# Fields containing filesystem paths -> anonymizer.path()
_PATH_FIELDS = frozenset({
    "file_path", "path", "workdir", "working_dir", "dir_path",
})
# Fields containing shell commands or search patterns -> redact_text() + anonymizer.text()
_COMMAND_FIELDS = frozenset({
    "command", "cmd", "pattern",
})
# Fields containing general text content -> anonymizer.text()
_TEXT_FIELDS = frozenset({
    "content", "prompt", "query", "url", "explanation", "chars",
    "patch", "patchText", "old_string", "new_string", "description",
})
# Fields containing lists of paths -> anonymizer.path() per element
_PATH_LIST_FIELDS = frozenset({
    "paths", "files",
})
# Fields containing lists of text strings -> anonymizer.text() per element
_TEXT_LIST_FIELDS = frozenset({
    "ignore",
})

_CODEX_PROJECT_INDEX: dict[str, list[Path]] = {}
_CLAUDE_SCIENCE_PROJECT_INDEX: dict[str, list[dict[str, Any]]] = {}
_GEMINI_HASH_MAP: dict[str, str] = {}
_OPENCODE_PROJECT_INDEX: dict[str, list[str]] = {}
_OPENCLAW_PROJECT_INDEX: dict[str, list[Path]] = {}
_KIMI_PROJECT_INDEX: dict[str, list[Path]] = {}
_CURSOR_PROJECT_INDEX: dict[str, list[Path]] = {}
_AIDER_PROJECT_INDEX: dict[str, Path] = {}


def _build_gemini_hash_map() -> dict[str, str]:
    """Build a mapping from SHA-256 hash prefix to directory path.

    Gemini CLI names project dirs by hashing the absolute working directory path.
    We scan first-level dirs under $HOME to reverse this mapping.
    """
    result: dict[str, str] = {}
    home = Path.home()
    try:
        for entry in home.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                h = hashlib.sha256(str(entry).encode()).hexdigest()
                result[h] = str(entry)
    except OSError:
        pass
    return result


def _extract_project_path_from_sessions(project_hash: str) -> str | None:
    """Try to extract the project working directory from session tool call file paths."""
    chats_dir = GEMINI_DIR / project_hash / "chats"
    if not chats_dir.exists():
        return None
    for session_file in sorted(chats_dir.glob("session-*.json"), reverse=True):
        try:
            data = json.loads(session_file.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        for msg in data.get("messages", []):
            for tc in msg.get("toolCalls", []):
                fp = tc.get("args", {}).get("file_path") or tc.get("args", {}).get("path", "")
                if fp.startswith("/"):
                    # Extract the shallowest directory and verify its hash matches
                    parts = Path(fp).parts  # e.g. ('/', 'home', 'wd', 'project', ...)
                    for depth in range(3, len(parts)):
                        candidate = str(Path(*parts[:depth + 1]))
                        if hashlib.sha256(candidate.encode()).hexdigest() == project_hash:
                            return candidate
        # Only check the most recent session file with tool calls
        break
    return None


def _resolve_gemini_hash(project_hash: str) -> str:
    """Resolve a Gemini project hash to a readable directory name.

    Strategy:
    1. Check hash map built from first-level dirs under $HOME.
    2. Fallback: extract path from session file tool call args.
    3. Last resort: return first 8 chars of the hash.
    """
    global _GEMINI_HASH_MAP
    if not _GEMINI_HASH_MAP:
        _GEMINI_HASH_MAP = _build_gemini_hash_map()
    full_path = _GEMINI_HASH_MAP.get(project_hash)
    if full_path:
        return Path(full_path).name
    # Fallback: try extracting from session files
    extracted = _extract_project_path_from_sessions(project_hash)
    if extracted:
        _GEMINI_HASH_MAP[project_hash] = extracted  # cache it
        return Path(extracted).name
    return project_hash[:8]


def _iter_jsonl(filepath: Path):
    """Yield parsed JSON objects from a JSONL file, skipping blank/malformed lines."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _path_to_dir_name(absolute_path: str) -> str:
    """Encode an absolute path to a Claude project dir_name (replace / with -)."""
    return absolute_path.replace("/", "-")


def _scan_local_agent_sessions() -> dict[str, list[dict]]:
    """Scan local-agent-mode-sessions and group by workspace key.

    Returns {workspace_key: [session_descriptor, ...]} where workspace_key
    is a dir_name derived from userSelectedFolders, or a synthetic key
    for sessions without folder metadata.
    """
    if not LOCAL_AGENT_DIR.exists():
        return {}

    groups: dict[str, list[dict]] = {}

    try:
        for root_entry in LOCAL_AGENT_DIR.iterdir():
            if not root_entry.is_dir() or not _UUID_RE.match(root_entry.name):
                continue
            for workspace_entry in root_entry.iterdir():
                if not workspace_entry.is_dir() or not _UUID_RE.match(workspace_entry.name):
                    continue
                _scan_workspace_dir(workspace_entry, groups)
    except OSError:
        logger.warning("Failed to scan local-agent-mode-sessions directory")

    return groups


def _scan_workspace_dir(workspace_dir: Path, groups: dict[str, list[dict]]) -> None:
    """Scan a single workspace directory for local-agent session wrappers."""
    try:
        entries = list(workspace_dir.iterdir())
    except OSError:
        return

    for entry in entries:
        if not entry.is_file() or not entry.name.startswith("local_") or not entry.name.endswith(".json"):
            continue

        try:
            wrapper = json.loads(entry.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(wrapper, dict):
            continue

        cli_session_id = wrapper.get("cliSessionId")
        if not cli_session_id:
            continue

        session_id = wrapper.get("sessionId") or cli_session_id
        process_name = wrapper.get("processName", "")
        user_folders = wrapper.get("userSelectedFolders", [])

        # Derive workspace key
        if user_folders and isinstance(user_folders, list) and user_folders[0] and user_folders[0] != "/":
            workspace_key = _path_to_dir_name(user_folders[0].rstrip("/"))
        else:
            workspace_key = f"_cowork_{session_id}"

        # Find the session directory (same name without .json)
        session_dir = entry.with_suffix("")
        if not session_dir.is_dir():
            continue

        # Find nested .claude/projects/ dir
        nested_project_dir = None
        nested_projects_root = session_dir / ".claude" / "projects"
        if nested_projects_root.is_dir():
            # Look for the -sessions-<processName> directory
            # Use replace to sanitize any path separators in processName
            safe_process_name = process_name.replace("/", "-")
            expected_dir_name = f"-sessions-{safe_process_name}"
            candidate = nested_projects_root / expected_dir_name
            if candidate.is_dir():
                nested_project_dir = candidate
            else:
                # Fallback: use first directory found
                for d in nested_projects_root.iterdir():
                    if d.is_dir():
                        nested_project_dir = d
                        break

        # Check for audit.jsonl
        audit_path = session_dir / "audit.jsonl"
        if not audit_path.exists():
            audit_path = None

        descriptor = {
            "wrapper_path": entry,
            "session_dir": session_dir,
            "nested_project_dir": nested_project_dir,
            "audit_path": audit_path,
            "cli_session_id": cli_session_id,
            "outer_session_id": session_id,
            "process_name": process_name,
            "wrapper_meta": {
                "title": wrapper.get("title", ""),
                "model": wrapper.get("model", ""),
                "createdAt": wrapper.get("createdAt"),
                "lastActivityAt": wrapper.get("lastActivityAt"),
            },
        }

        groups.setdefault(workspace_key, []).append(descriptor)


def _get_native_session_ids(project_dir: Path | None) -> set[str]:
    """Get session IDs from a native Claude project directory."""
    if not project_dir or not project_dir.exists():
        return set()
    ids = {f.stem for f in project_dir.glob("*.jsonl")}
    for entry in project_dir.iterdir():
        if entry.is_dir() and entry.name not in ids:
            subagent_dir = entry / "subagents"
            if subagent_dir.is_dir() and any(subagent_dir.glob("agent-*.jsonl")):
                ids.add(entry.name)
    return ids


def _estimate_la_session_size(descriptor: dict) -> int:
    """Estimate the size of a local-agent session's transcript files."""
    total = 0
    nested_dir = descriptor.get("nested_project_dir")
    cli_id = descriptor.get("cli_session_id")
    if nested_dir and cli_id:
        candidate = nested_dir / f"{cli_id}.jsonl"
        try:
            if candidate.exists():
                total += candidate.stat().st_size
        except OSError:
            pass
    if total == 0 and descriptor.get("audit_path"):
        try:
            total += descriptor["audit_path"].stat().st_size
        except OSError:
            pass
    return total


def _build_cowork_project_name(workspace_key: str, la_sessions: list[dict]) -> str:
    """Build a display name for a cowork project without a recoverable host path."""
    if la_sessions:
        def _activity_key(s: dict) -> int | float:
            val = s.get("wrapper_meta", {}).get("lastActivityAt")
            return val if isinstance(val, (int, float)) else 0

        best = max(la_sessions, key=_activity_key)
        title = best.get("wrapper_meta", {}).get("title", "")
        if title:
            short_title = title[:40].strip()
            return f"claude:cowork/{short_title}"
    outer_id = workspace_key.removeprefix("_cowork_")
    return f"claude:cowork/{outer_id[:12]}"


def _claude_cwd_dirs(
    native_dir: Path | None = None,
    local_agent_sessions: list[dict] | None = None,
) -> list[Path]:
    """Return Claude transcript dirs that may contain a recorded cwd."""
    dirs: list[Path] = []
    if native_dir:
        dirs.append(native_dir)
    for la_session in local_agent_sessions or []:
        nested = la_session.get("nested_project_dir")
        if nested:
            dirs.append(nested)
    return dirs


def _build_claude_project_name(
    project_dir_name: str,
    *,
    native_dir: Path | None = None,
    local_agent_sessions: list[dict] | None = None,
) -> str:
    if project_dir_name.startswith("_cowork_"):
        return _build_cowork_project_name(project_dir_name, local_agent_sessions or [])

    cwd = _read_project_cwd(*_claude_cwd_dirs(native_dir, local_agent_sessions))
    if cwd:
        return f"claude:{Path(cwd).name or cwd}"
    return _build_project_name(project_dir_name)


def discover_projects(source_filter: str | None = None) -> list[dict]:
    """Discover supported source projects with optional source filtering."""
    discovery_by_source = {
        CLAUDE_SOURCE: _discover_claude_projects,
        CLAUDE_SCIENCE_SOURCE: _discover_claude_science_projects,
        CODEX_SOURCE: _discover_codex_projects,
        GEMINI_SOURCE: _discover_gemini_projects,
        OPENCODE_SOURCE: _discover_opencode_projects,
        OPENCLAW_SOURCE: _discover_openclaw_projects,
        KIMI_SOURCE: _discover_kimi_projects,
        CURSOR_SOURCE: _discover_cursor_projects,
        COPILOT_SOURCE: _discover_copilot_projects,
        AIDER_SOURCE: _discover_aider_projects,
        CUSTOM_SOURCE: _discover_custom_projects,
    }

    normalized = (source_filter or "").strip().lower()
    if normalized in ("", "auto", "all"):
        discovery_fns = discovery_by_source.values()
    elif normalized == "both":
        discovery_fns = (discovery_by_source[CLAUDE_SOURCE], discovery_by_source[CODEX_SOURCE])
    else:
        discover_fn = discovery_by_source.get(normalized)
        if discover_fn is None:
            return []
        discovery_fns = (discover_fn,)

    projects: list[dict[str, Any]] = []
    for discover_fn in discovery_fns:
        projects.extend(discover_fn())
    return sorted(projects, key=lambda p: (p["display_name"], p["source"]))


def _discover_claude_projects() -> list[dict]:
    # Step 1: Discover native projects
    canonical: dict[str, dict] = {}

    if PROJECTS_DIR.exists():
        for project_dir in sorted(PROJECTS_DIR.iterdir()):
            if not project_dir.is_dir():
                continue
            root_sessions = list(project_dir.glob("*.jsonl"))
            subagent_sessions = _find_subagent_only_sessions(project_dir)
            total_count = len(root_sessions) + len(subagent_sessions)
            if total_count == 0:
                continue
            total_size = sum(f.stat().st_size for f in root_sessions)
            for session_dir in subagent_sessions:
                for sa_file in (session_dir / "subagents").glob("agent-*.jsonl"):
                    total_size += sa_file.stat().st_size
            canonical[project_dir.name] = {
                "dir_name": project_dir.name,
                "display_name": _build_claude_project_name(
                    project_dir.name,
                    native_dir=project_dir,
                ),
                "session_count": total_count,
                "total_size_bytes": total_size,
                "source": CLAUDE_SOURCE,
                "locator": {
                    "native_project_dir": project_dir,
                    "local_agent_sessions": [],
                },
            }

    # Step 2: Scan local-agent sessions and merge
    la_groups = _scan_local_agent_sessions()

    for workspace_key, la_sessions in la_groups.items():
        if workspace_key in canonical:
            # Merge into existing native project
            canonical[workspace_key]["locator"]["local_agent_sessions"].extend(la_sessions)
            canonical[workspace_key]["display_name"] = _build_claude_project_name(
                workspace_key,
                native_dir=canonical[workspace_key]["locator"]["native_project_dir"],
                local_agent_sessions=canonical[workspace_key]["locator"]["local_agent_sessions"],
            )
            native_ids = _get_native_session_ids(canonical[workspace_key]["locator"]["native_project_dir"])
            new_la = [s for s in la_sessions if s["cli_session_id"] not in native_ids and s.get("nested_project_dir")]
            canonical[workspace_key]["session_count"] += len(new_la)
            canonical[workspace_key]["total_size_bytes"] += sum(
                _estimate_la_session_size(s) for s in new_la
            )
        else:
            # New project from local-agent only
            display_name = _build_claude_project_name(
                workspace_key,
                local_agent_sessions=la_sessions,
            )
            parseable = [s for s in la_sessions if s.get("nested_project_dir")]
            canonical[workspace_key] = {
                "dir_name": workspace_key,
                "display_name": display_name,
                "session_count": len(parseable),
                "total_size_bytes": sum(_estimate_la_session_size(s) for s in parseable),
                "source": CLAUDE_SOURCE,
                "locator": {
                    "native_project_dir": None,
                    "local_agent_sessions": la_sessions,
                },
            }

    return [p for p in canonical.values() if p["session_count"] > 0]


def _connect_sqlite_readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _iter_claude_science_dbs() -> list[tuple[str, Path]]:
    orgs_dir = CLAUDE_SCIENCE_DIR / "orgs"
    if not orgs_dir.exists():
        return []
    dbs: list[tuple[str, Path]] = []
    try:
        for org_dir in sorted(orgs_dir.iterdir()):
            if not org_dir.is_dir():
                continue
            db_path = org_dir / CLAUDE_SCIENCE_DB_NAME
            if db_path.exists():
                dbs.append((org_dir.name, db_path))
    except OSError:
        return []
    return dbs


def _claude_science_project_key(org_id: str, project_id: Any) -> str:
    project = project_id if isinstance(project_id, str) and project_id.strip() else "unknown"
    return f"{org_id}:{project}"


def _split_claude_science_project_key(project_key: str) -> tuple[str, str]:
    org_id, sep, project_id = project_key.partition(":")
    return org_id, project_id if sep else "unknown"


def _claude_science_session_id(org_id: str, frame_id: str) -> str:
    return f"{CLAUDE_SCIENCE_SOURCE}:{org_id}:{frame_id}"


def _get_claude_science_project_index(refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
    global _CLAUDE_SCIENCE_PROJECT_INDEX
    if refresh or not _CLAUDE_SCIENCE_PROJECT_INDEX:
        _CLAUDE_SCIENCE_PROJECT_INDEX = _build_claude_science_project_index()
    return _CLAUDE_SCIENCE_PROJECT_INDEX


def _build_claude_science_project_index() -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for org_id, db_path in _iter_claude_science_dbs():
        try:
            db_size = db_path.stat().st_size
        except OSError:
            db_size = 0
        try:
            with _connect_sqlite_readonly(db_path) as conn:
                rows = conn.execute(
                    """
                    SELECT
                        f.id, f.parent_frame_id, f.root_frame_id,
                        f.agent_name, f.status, f.model, f.effort,
                        f.input_tokens, f.output_tokens,
                        f.cache_read_tokens, f.cache_write_tokens,
                        f.aux_input_tokens, f.aux_output_tokens,
                        f.aux_cache_read_tokens, f.aux_cache_write_tokens,
                        f.created_at, f.updated_at, f.completed_at,
                        f.project_id, f.name, f.conversation_type,
                        f.task_summary, f.status_description, f.is_hidden,
                        p.name AS project_name,
                        (SELECT COUNT(*) FROM frame_messages fm WHERE fm.frame_id = f.id) AS message_count,
                        (SELECT COALESCE(SUM(LENGTH(fm.msg_json)), 0) FROM frame_messages fm WHERE fm.frame_id = f.id) AS message_bytes,
                        (SELECT COUNT(*) FROM execution_log el WHERE el.frame_id = f.id) AS execution_count
                      FROM frames f
                      LEFT JOIN projects p ON p.id = f.project_id
                     ORDER BY COALESCE(f.completed_at, f.updated_at, f.created_at) DESC, f.id DESC
                    """
                ).fetchall()
        except sqlite3.Error:
            logger.warning("Failed to scan Claude Science database %s", db_path)
            continue

        for row in rows:
            message_count = _safe_int(row["message_count"])
            execution_count = _safe_int(row["execution_count"])
            if message_count == 0 and execution_count == 0:
                continue
            project_key = _claude_science_project_key(org_id, row["project_id"])
            frame = dict(row)
            frame["org_id"] = org_id
            frame["db_path"] = db_path
            frame["message_count"] = message_count
            frame["execution_count"] = execution_count
            message_bytes = _safe_int(row["message_bytes"])
            frame["size_bytes"] = message_bytes if message_bytes > 0 else db_size // max(len(rows), 1)
            index.setdefault(project_key, []).append(frame)
    return index


def _build_claude_science_project_name(project_key: str, frames: list[dict[str, Any]]) -> str:
    for frame in frames:
        project_name = frame.get("project_name")
        if isinstance(project_name, str) and project_name.strip():
            return f"{CLAUDE_SCIENCE_SOURCE}:{project_name.strip()}"
    _org_id, project_id = _split_claude_science_project_key(project_key)
    label = project_id[:12] if project_id and project_id != "unknown" else "unknown"
    return f"{CLAUDE_SCIENCE_SOURCE}:{label}"


def _claude_science_frame_title(frame: dict[str, Any], anonymizer: Anonymizer) -> str | None:
    for key in ("name", "status_description", "task_summary"):
        value = frame.get(key)
        if not isinstance(value, str):
            continue
        title = re.sub(r"\s+", " ", value).strip()
        if title:
            return anonymizer.text(title)
    return None


def _discover_claude_science_projects() -> list[dict]:
    index = _get_claude_science_project_index(refresh=True)
    projects = []
    for project_key, frames in sorted(index.items()):
        if not frames:
            continue
        projects.append(
            {
                "dir_name": project_key,
                "display_name": _build_claude_science_project_name(project_key, frames),
                "session_count": len(frames),
                "total_size_bytes": sum(_safe_int(frame.get("size_bytes")) for frame in frames),
                "source": CLAUDE_SCIENCE_SOURCE,
            }
        )
    return projects


def _discover_codex_projects() -> list[dict]:
    index = _get_codex_project_index(refresh=True)
    projects = []
    for cwd, session_files in sorted(index.items()):
        if not session_files:
            continue
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_codex_project_name(cwd),
                "session_count": len(session_files),
                "total_size_bytes": sum(f.stat().st_size for f in session_files),
                "source": CODEX_SOURCE,
            }
        )
    return projects


def _discover_gemini_projects() -> list[dict]:
    if not GEMINI_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(GEMINI_DIR.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "bin":
            continue
        chats_dir = project_dir / "chats"
        if not chats_dir.exists():
            continue
        sessions = list(chats_dir.glob("session-*.json"))
        if not sessions:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": _build_gemini_project_name(project_dir.name),
                "session_count": len(sessions),
                "total_size_bytes": sum(f.stat().st_size for f in sessions),
                "source": GEMINI_SOURCE,
            }
        )
    return projects


def _discover_opencode_projects() -> list[dict]:
    index = _get_opencode_project_index(refresh=True)
    total_sessions = sum(len(session_ids) for session_ids in index.values())
    db_size = OPENCODE_DB_PATH.stat().st_size if OPENCODE_DB_PATH.exists() else 0

    projects = []
    for cwd, session_ids in sorted(index.items()):
        if not session_ids:
            continue
        estimated_size = int(db_size * (len(session_ids) / total_sessions)) if total_sessions else 0
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_opencode_project_name(cwd),
                "session_count": len(session_ids),
                "total_size_bytes": estimated_size,
                "source": OPENCODE_SOURCE,
            }
        )
    return projects


def _discover_openclaw_projects() -> list[dict]:
    index = _get_openclaw_project_index(refresh=True)
    projects = []
    for cwd, session_files in sorted(index.items()):
        if not session_files:
            continue
        total_size = sum(f.stat().st_size for f in session_files if f.exists())
        projects.append(
            {
                "dir_name": cwd,
                "display_name": _build_openclaw_project_name(cwd),
                "session_count": len(session_files),
                "total_size_bytes": total_size,
                "source": OPENCLAW_SOURCE,
            }
        )
    return projects


def _load_kimi_work_dirs() -> dict[str, str]:
    """Load Kimi work directory mapping from config file."""
    if not KIMI_CONFIG_PATH.exists():
        return {}
    try:
        data = json.loads(KIMI_CONFIG_PATH.read_text(encoding="utf-8", errors="replace"))
        work_dirs = data.get("work_dirs", [])
        return {
            entry.get("path", ""): entry.get("path", "")
            for entry in work_dirs
            if entry.get("path")
        }
    except (json.JSONDecodeError, OSError):
        return {}


def _get_kimi_project_hash(cwd: str) -> str:
    """Generate Kimi project hash from working directory path (MD5)."""
    return hashlib.md5(cwd.encode()).hexdigest()


def _discover_kimi_projects() -> list[dict]:
    if not KIMI_SESSIONS_DIR.exists():
        return []

    work_dirs = _load_kimi_work_dirs()
    path_to_hash = {path: _get_kimi_project_hash(path) for path in work_dirs}
    hash_to_path = {h: p for p, h in path_to_hash.items()}

    projects = []
    for project_dir in sorted(KIMI_SESSIONS_DIR.iterdir()):
        if not project_dir.is_dir():
            continue

        project_hash = project_dir.name
        session_dirs = [d for d in project_dir.iterdir() if d.is_dir()]
        if not session_dirs:
            continue

        total_sessions = 0
        total_size = 0
        for session_dir in session_dirs:
            context_file = session_dir / "context.jsonl"
            if context_file.exists():
                total_sessions += 1
                total_size += context_file.stat().st_size

        if total_sessions == 0:
            continue

        project_path = hash_to_path.get(project_hash)
        dir_name = project_path if project_path else project_hash

        projects.append(
            {
                "dir_name": dir_name,
                "display_name": _build_kimi_project_name(project_hash, hash_to_path),
                "session_count": total_sessions,
                "total_size_bytes": total_size,
                "source": KIMI_SOURCE,
            }
        )
    return projects


def _build_kimi_project_name(cwd: str, hash_to_path: dict[str, str] | None = None) -> str:
    if hash_to_path is not None:
        project_path = hash_to_path.get(cwd)
        if project_path:
            return f"kimi:{Path(project_path).name}"
        return f"kimi:{cwd[:8]}"
    if cwd == UNKNOWN_KIMI_CWD:
        return "kimi:unknown"
    return f"kimi:{Path(cwd).name or cwd}"


def _discover_custom_projects() -> list[dict]:
    if not CUSTOM_DIR.exists():
        return []

    projects = []
    for project_dir in sorted(CUSTOM_DIR.iterdir()):
        if not project_dir.is_dir():
            continue
        jsonl_files = list(project_dir.glob("*.jsonl"))
        if not jsonl_files:
            continue
        session_count = 0
        total_size = 0
        for f in jsonl_files:
            total_size += f.stat().st_size
            try:
                session_count += sum(
                    1
                    for line in f.open(encoding="utf-8", errors="replace")
                    if line.strip()
                )
            except OSError:
                pass
        if session_count == 0:
            continue
        projects.append(
            {
                "dir_name": project_dir.name,
                "display_name": _build_custom_project_name(project_dir.name),
                "session_count": session_count,
                "total_size_bytes": total_size,
                "source": CUSTOM_SOURCE,
            }
        )
    return projects


def _parse_custom_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
) -> list[dict]:
    project_path = CUSTOM_DIR / project_dir_name
    if not project_path.exists():
        return []

    required_fields = {"session_id", "model", "messages"}
    sessions = []
    for jsonl_file in sorted(project_path.glob("*.jsonl")):
        try:
            for line_num, line in enumerate(
                jsonl_file.open(encoding="utf-8", errors="replace"),
                1,
            ):
                line = line.strip()
                if not line:
                    continue
                try:
                    session = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        "custom:%s: %s line %d: invalid JSON, skipping",
                        project_dir_name, jsonl_file.name, line_num,
                    )
                    continue
                if not isinstance(session, dict):
                    logger.warning(
                        "custom:%s: %s line %d: not a JSON object, skipping",
                        project_dir_name, jsonl_file.name, line_num,
                    )
                    continue
                missing = required_fields - session.keys()
                if missing:
                    logger.warning(
                        "custom:%s: %s line %d: missing required fields %s, skipping",
                        project_dir_name, jsonl_file.name, line_num, sorted(missing),
                    )
                    continue
                session["project"] = f"custom:{project_dir_name}"
                session["source"] = CUSTOM_SOURCE
                # Redact message content through the anonymizer
                for msg in session.get("messages", []):
                    if "content" in msg and isinstance(msg["content"], str):
                        redacted, _, _ = redact_text(msg["content"])
                        msg["content"] = anonymizer.text(redacted)
                sessions.append(session)
        except OSError:
            logger.warning("custom:%s: failed to read %s", project_dir_name, jsonl_file.name)
    return sessions


def _parse_claude_science_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> list[dict]:
    index = _get_claude_science_project_index()
    frames = index.get(project_dir_name, [])
    if not frames:
        return []
    project_name = _build_claude_science_project_name(project_dir_name, frames)
    sessions = []
    for frame in frames:
        parsed = _parse_claude_science_frame(frame, anonymizer, include_thinking)
        if not parsed or not parsed["messages"]:
            continue
        org_id = frame.get("org_id")
        frame_id = frame.get("id")
        if not isinstance(org_id, str) or not isinstance(frame_id, str):
            continue
        parsed["project"] = project_name
        parsed["source"] = CLAUDE_SCIENCE_SOURCE
        parsed["raw_source_path"] = f"{frame.get('db_path')}#frame={frame_id}"
        parsed["client_origin"] = "desktop"
        parsed["runtime_channel"] = "claude-science"
        parent_frame_id = frame.get("parent_frame_id")
        if isinstance(parent_frame_id, str) and parent_frame_id:
            parsed["parent_session_id"] = _claude_science_session_id(org_id, parent_frame_id)
        root_frame_id = frame.get("root_frame_id")
        if isinstance(root_frame_id, str) and root_frame_id and root_frame_id != frame_id:
            parsed["outer_session_id"] = _claude_science_session_id(org_id, root_frame_id)
        sessions.append(parsed)
    return sessions


def _parse_claude_science_frame(
    frame: dict[str, Any],
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict | None:
    db_path = frame.get("db_path")
    frame_id = frame.get("id")
    org_id = frame.get("org_id")
    if not isinstance(db_path, Path) or not isinstance(frame_id, str) or not isinstance(org_id, str):
        return None

    metadata: dict[str, Any] = {
        "session_id": _claude_science_session_id(org_id, frame_id),
        "cwd": None,
        "git_branch": None,
        "model": frame.get("model") or "claude-science",
        "model_effort": frame.get("effort") if isinstance(frame.get("effort"), str) else None,
        "start_time": _normalize_timestamp(frame.get("created_at")),
        "end_time": _normalize_timestamp(frame.get("completed_at") or frame.get("updated_at")),
        "segment_title": _claude_science_frame_title(frame, anonymizer),
    }
    stats = _make_stats()
    stats["input_tokens"] = _safe_int(frame.get("input_tokens")) + _safe_int(frame.get("aux_input_tokens"))
    stats["output_tokens"] = _safe_int(frame.get("output_tokens")) + _safe_int(frame.get("aux_output_tokens"))
    stats["cache_read_tokens"] = (
        _safe_int(frame.get("cache_read_tokens")) + _safe_int(frame.get("aux_cache_read_tokens"))
    )
    stats["cache_creation_tokens"] = (
        _safe_int(frame.get("cache_write_tokens")) + _safe_int(frame.get("aux_cache_write_tokens"))
    )

    messages: list[dict[str, Any]] = []
    try:
        with _connect_sqlite_readonly(db_path) as conn:
            rows = conn.execute(
                "SELECT idx, msg_json FROM frame_messages WHERE frame_id = ? ORDER BY idx",
                (frame_id,),
            ).fetchall()
            entries = _load_claude_science_message_entries(rows)
            tool_result_map = _build_tool_result_map(entries, anonymizer)
            for entry in entries:
                _process_claude_science_entry(
                    entry, messages, metadata, stats, anonymizer, include_thinking, tool_result_map,
                )
            _append_claude_science_execution_messages(conn, frame_id, messages, metadata, stats, anonymizer)
    except sqlite3.Error:
        logger.warning("Failed to parse Claude Science frame %s in %s", frame_id, db_path)
        return None

    result = _make_session_result(metadata, messages, stats)
    if result is None:
        return None
    for key in ("agent_name", "conversation_type", "status"):
        value = frame.get(key)
        if isinstance(value, str) and value:
            result[key] = value
    return result


def _load_claude_science_message_entries(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for row in rows:
        try:
            msg_data = json.loads(row["msg_json"])
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(msg_data, dict):
            continue
        role = msg_data.get("role")
        if role not in ("user", "assistant"):
            continue
        entries.append({"type": role, "message": msg_data, "idx": row["idx"]})
    return entries


def _process_claude_science_entry(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: Anonymizer,
    include_thinking: bool,
    tool_result_map: dict[str, dict],
) -> None:
    role = entry.get("type")
    msg_data = entry.get("message", {})
    if not isinstance(msg_data, dict):
        return

    if role == "user":
        content = _extract_claude_science_user_content(msg_data, anonymizer)
        if content is not None:
            messages.append({"role": "user", "content": content, "timestamp": None})
            stats["user_messages"] += 1
        return

    if role != "assistant":
        return

    if metadata["model"] in (None, "claude-science"):
        model = msg_data.get("model")
        if isinstance(model, str) and model.strip():
            metadata["model"] = model
    if metadata.get("model_effort") is None:
        metadata["model_effort"] = _extract_model_effort(msg_data)

    msg = _extract_assistant_content(
        {"message": msg_data}, anonymizer, include_thinking, tool_result_map,
    )
    if msg:
        msg["timestamp"] = None
        messages.append(msg)
        stats["assistant_messages"] += 1
        stats["tool_uses"] += len(msg.get("tool_uses", []))


def _extract_claude_science_user_content(msg_data: dict[str, Any], anonymizer: Anonymizer) -> str | None:
    content = msg_data.get("content")
    if isinstance(content, str):
        text = content.strip()
        return anonymizer.text(text) if text else None
    if not isinstance(content, list):
        return None

    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text.strip():
                parts.append(anonymizer.text(text.strip()))
        elif block_type in ("image", "document"):
            label = _claude_science_attachment_label(block_type, block, anonymizer)
            if label:
                parts.append(label)
    if not parts:
        return None
    return "\n\n".join(parts)


def _claude_science_attachment_label(
    block_type: str, block: dict[str, Any], anonymizer: Anonymizer,
) -> str | None:
    filename = block.get("_filename")
    if isinstance(filename, str) and filename.strip():
        return f"[{block_type}: {anonymizer.path(filename.strip())}]"
    return f"[{block_type}]"


def _append_claude_science_execution_messages(
    conn: sqlite3.Connection,
    frame_id: str,
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: Anonymizer,
) -> None:
    rows = conn.execute(
        """
        SELECT id, cell_index, conda_env, language, source, stdout, stderr,
               exit_status, created_at, files_written, files_read, error_lineno,
               kernel_kind, origin, detection
          FROM execution_log
         WHERE frame_id = ?
         ORDER BY created_at, cell_index, id
        """,
        (frame_id,),
    ).fetchall()
    for row in rows:
        tool_use = _parse_claude_science_execution_tool(conn, row, anonymizer)
        timestamp = _normalize_timestamp(row["created_at"])
        messages.append({"role": "assistant", "tool_uses": [tool_use], "timestamp": timestamp})
        stats["assistant_messages"] += 1
        stats["tool_uses"] += 1


def _parse_claude_science_execution_tool(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    anonymizer: Anonymizer,
) -> dict[str, Any]:
    language = row["language"] if isinstance(row["language"], str) and row["language"] else "execution"
    tool_name = "bash" if language in ("bash", "shell", "sh", "zsh") else language
    source = row["source"] if isinstance(row["source"], str) else ""
    if tool_name == "bash":
        input_data: dict[str, Any] = {"command": source}
    else:
        input_data = {"code": source, "language": language}
    input_data["cell_index"] = row["cell_index"]
    if isinstance(row["conda_env"], str) and row["conda_env"]:
        input_data["environment"] = row["conda_env"]
    if isinstance(row["kernel_kind"], str) and row["kernel_kind"]:
        input_data["kernel_kind"] = row["kernel_kind"]

    output: dict[str, Any] = {"exit_status": row["exit_status"]}
    if isinstance(row["stdout"], str) and row["stdout"]:
        output["stdout"] = anonymizer.text(row["stdout"])
    if isinstance(row["stderr"], str) and row["stderr"]:
        output["stderr"] = anonymizer.text(row["stderr"])
    for key in ("files_written", "files_read", "detection"):
        parsed = _json_loads_maybe(row[key])
        if parsed:
            output[key] = _anonymize_claude_science_value(parsed, anonymizer, key)
    if row["error_lineno"] is not None:
        output["error_lineno"] = row["error_lineno"]

    host_calls = _load_claude_science_host_calls(conn, row["id"], anonymizer)
    if host_calls:
        output["host_calls"] = host_calls

    status = "success" if row["exit_status"] in ("ok", "success", "0", 0, None) else "error"
    return {
        "tool": tool_name,
        "input": _parse_tool_input(tool_name, input_data, anonymizer),
        "output": output,
        "status": status,
    }


def _load_claude_science_host_calls(
    conn: sqlite3.Connection,
    execution_log_id: str,
    anonymizer: Anonymizer,
) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT seq, method, args_json, derivable, data_ref, error, bytes, created_at
          FROM host_call_log
         WHERE execution_log_id = ?
         ORDER BY seq, id
        """,
        (execution_log_id,),
    ).fetchall()
    calls: list[dict[str, Any]] = []
    for row in rows:
        method = row["method"] if isinstance(row["method"], str) and row["method"] else "host_call"
        args = _json_loads_maybe(row["args_json"])
        call: dict[str, Any] = {
            "seq": row["seq"],
            "method": method,
            "args": _parse_tool_input(method, args if isinstance(args, dict) else {}, anonymizer),
            "bytes": row["bytes"],
            "derivable": bool(row["derivable"]),
        }
        if isinstance(row["data_ref"], str) and row["data_ref"]:
            call["data_ref"] = anonymizer.path(row["data_ref"])
        if isinstance(row["error"], str) and row["error"]:
            call["error"] = anonymizer.text(row["error"])
        timestamp = _normalize_timestamp(row["created_at"])
        if timestamp:
            call["timestamp"] = timestamp
        calls.append(call)
    return calls


def _json_loads_maybe(value: Any) -> Any:
    if value is None:
        return None
    if not isinstance(value, str):
        return value
    if not value.strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return value


def _anonymize_claude_science_value(value: Any, anonymizer: Anonymizer, key: str = "") -> Any:
    if isinstance(value, dict):
        return {
            k: _anonymize_claude_science_value(v, anonymizer, str(k))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_anonymize_claude_science_value(item, anonymizer, key) for item in value]
    if isinstance(value, str):
        key_l = key.lower()
        if "path" in key_l or "file" in key_l or key_l in {"cwd", "working_dir", "data_ref"}:
            return anonymizer.path(value)
        return anonymizer.text(value)
    return value


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    source: str = CLAUDE_SOURCE,
    locator: dict | None = None,
) -> list[dict]:
    """Parse all sessions for a project into structured dicts."""
    if source == CLAUDE_SCIENCE_SOURCE:
        return _parse_claude_science_project_sessions(
            project_dir_name, anonymizer, include_thinking,
        )

    if source == CUSTOM_SOURCE:
        return _parse_custom_sessions(project_dir_name, anonymizer)

    if source == KIMI_SOURCE:
        project_hash = _get_kimi_project_hash(project_dir_name)
        project_path = KIMI_SESSIONS_DIR / project_hash
        if not project_path.exists():
            return []
        sessions = []
        for session_dir in sorted(project_path.iterdir()):
            if not session_dir.is_dir():
                continue
            context_file = session_dir / "context.jsonl"
            if not context_file.exists():
                continue
            parsed = _parse_kimi_session_file(
                context_file,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_kimi_project_name(project_dir_name)
                parsed["source"] = KIMI_SOURCE
                if not parsed.get("model"):
                    parsed["model"] = "kimi-k2"
                sessions.append(parsed)
        return sessions

    if source == OPENCLAW_SOURCE:
        index = _get_openclaw_project_index()
        session_files = index.get(project_dir_name, [])
        sessions = []
        for session_file in session_files:
            parsed = _parse_openclaw_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = _build_openclaw_project_name(project_dir_name)
                parsed["source"] = OPENCLAW_SOURCE
                parsed["raw_source_path"] = str(session_file)
                sessions.append(parsed)
        return sessions

    if source == GEMINI_SOURCE:
        project_path = GEMINI_DIR / project_dir_name / "chats"
        if not project_path.exists():
            return []
        sessions = []
        for session_file in sorted(project_path.glob("session-*.json")):
            parsed = _parse_gemini_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = f"gemini:{_resolve_gemini_hash(project_dir_name)}"
                parsed["source"] = GEMINI_SOURCE
                sessions.append(parsed)
        return sessions

    if source == OPENCODE_SOURCE:
        index = _get_opencode_project_index()
        session_ids = index.get(project_dir_name, [])
        sessions = []
        for session_id in session_ids:
            parsed = _parse_opencode_session(
                session_id,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=project_dir_name,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_opencode_project_name(project_dir_name)
                parsed["source"] = OPENCODE_SOURCE
                sessions.append(parsed)
        return sessions

    if source == CODEX_SOURCE:
        index = _get_codex_project_index()
        session_files = index.get(project_dir_name, [])
        sessions = []
        for session_file in session_files:
            parsed = _parse_codex_session_file(
                session_file,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=project_dir_name,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_codex_project_name(project_dir_name)
                parsed["source"] = CODEX_SOURCE
                parsed["raw_source_path"] = str(session_file)
                # Derive client_origin from originator field
                originator = parsed.pop("originator", None) or ""
                parsed.pop("codex_source", None)
                if "Desktop" in originator:
                    parsed["client_origin"] = "desktop"
                elif originator:
                    parsed["client_origin"] = "cli"
                sessions.append(parsed)
        return sessions

    if source == CURSOR_SOURCE:
        index = _get_cursor_project_index()
        session_files = index.get(project_dir_name, [])
        sessions = []
        for session_file in session_files:
            parsed = _parse_cursor_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = f"cursor:{_build_project_name(project_dir_name).removeprefix('claude:')}"
                parsed["source"] = CURSOR_SOURCE
                sessions.append(parsed)
        return sessions

    if source == COPILOT_SOURCE:
        sessions = []
        session_dir = COPILOT_DIR / project_dir_name
        if session_dir.is_dir():
            events_file = session_dir / "events.jsonl"
            if events_file.exists():
                parsed = _parse_copilot_session_file(events_file, anonymizer, include_thinking)
                if parsed and parsed["messages"]:
                    parsed["project"] = _build_copilot_project_name(project_dir_name)
                    parsed["source"] = COPILOT_SOURCE
                    sessions.append(parsed)
        return sessions

    if source == AIDER_SOURCE:
        index = _get_aider_project_index()
        history_file = index.get(project_dir_name)
        if not history_file or not history_file.exists():
            return []
        sessions = _parse_aider_history_file(history_file, anonymizer, project_dir_name)
        return sessions

    # Claude source — locator-aware parsing with session-level dedupe
    sessions = []
    seen_session_ids: set[str] = set()

    # Priority 1: Native ~/.claude/projects
    native_dir = None
    if locator and locator.get("native_project_dir"):
        native_dir = locator["native_project_dir"]
    elif not locator:
        native_dir = PROJECTS_DIR / project_dir_name

    project_name = _build_claude_project_name(
        project_dir_name,
        native_dir=native_dir,
        local_agent_sessions=locator.get("local_agent_sessions", []) if locator else [],
    )

    if native_dir and native_dir.exists():
        for session_file in sorted(native_dir.glob("*.jsonl")):
            parsed = _parse_claude_session_file(session_file, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = project_name
                parsed["source"] = CLAUDE_SOURCE
                parsed["raw_source_path"] = str(session_file)
                # Derive client_origin from entrypoint field
                ep = parsed.pop("entrypoint", None)
                if ep == "claude-desktop":
                    parsed["client_origin"] = "desktop"
                elif ep == "local-agent":
                    parsed["client_origin"] = "desktop"
                    parsed["runtime_channel"] = "local-agent"
                elif ep:
                    parsed["client_origin"] = ep
                sessions.append(parsed)
                seen_session_ids.add(parsed["session_id"])
                seen_session_ids.add(session_file.stem)

        for session_dir in _find_subagent_only_sessions(native_dir):
            parsed = _parse_subagent_session(session_dir, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed["project"] = project_name
                parsed["source"] = CLAUDE_SOURCE
                parsed["raw_source_path"] = str(session_dir)
                sessions.append(parsed)
                seen_session_ids.add(parsed["session_id"])
                seen_session_ids.add(session_dir.name)

    # Priority 2: Local-agent nested .claude/projects
    if locator:
        for la_session in locator.get("local_agent_sessions", []):
            cli_id = la_session.get("cli_session_id")
            if not cli_id or cli_id in seen_session_ids:
                continue

            nested_dir = la_session.get("nested_project_dir")
            jsonl_path = None
            if nested_dir:
                candidate = nested_dir / f"{cli_id}.jsonl"
                if candidate.exists():
                    jsonl_path = candidate

            if not jsonl_path:
                continue

            parsed = _parse_claude_session_file(jsonl_path, anonymizer, include_thinking)
            if parsed and parsed["messages"]:
                parsed.pop("entrypoint", None)
                parsed["project"] = project_name
                parsed["source"] = CLAUDE_SOURCE
                parsed["raw_source_path"] = str(jsonl_path)
                parsed["client_origin"] = "desktop"
                parsed["runtime_channel"] = "local-agent"
                parsed["outer_session_id"] = la_session.get("outer_session_id")
                # Enrich from wrapper metadata
                meta = la_session.get("wrapper_meta", {})
                if not parsed.get("model") and meta.get("model"):
                    parsed["model"] = meta["model"]
                sessions.append(parsed)
                seen_session_ids.add(parsed["session_id"])
                if cli_id != parsed["session_id"]:
                    seen_session_ids.add(cli_id)

    return sessions


def _parse_opencode_session(
    session_id: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    if not OPENCODE_DB_PATH.exists():
        return None

    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": session_id,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "model_effort": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        with sqlite3.connect(OPENCODE_DB_PATH) as conn:
            conn.row_factory = sqlite3.Row
            session_row = conn.execute(
                "SELECT id, directory, time_created, time_updated FROM session WHERE id = ?",
                (session_id,),
            ).fetchone()
            if session_row is None:
                return None

            raw_cwd = session_row["directory"]
            if isinstance(raw_cwd, str) and raw_cwd.strip():
                if raw_cwd != target_cwd:
                    return None
                metadata["cwd"] = anonymizer.path(raw_cwd)
            elif target_cwd != UNKNOWN_OPENCODE_CWD:
                return None

            metadata["start_time"] = _normalize_timestamp(session_row["time_created"])
            metadata["end_time"] = _normalize_timestamp(session_row["time_updated"])

            message_rows = conn.execute(
                "SELECT id, data, time_created FROM message WHERE session_id = ? ORDER BY time_created ASC, id ASC",
                (session_id,),
            ).fetchall()

            for message_row in message_rows:
                message_data = _load_json_field(message_row["data"])
                role = message_data.get("role")
                timestamp = _normalize_timestamp(message_row["time_created"])

                model = _extract_opencode_model(message_data)
                if metadata["model"] is None and model:
                    metadata["model"] = model
                if metadata.get("model_effort") is None:
                    metadata["model_effort"] = _extract_model_effort(message_data)

                part_rows = conn.execute(
                    "SELECT data FROM part WHERE message_id = ? ORDER BY time_created ASC, id ASC",
                    (message_row["id"],),
                ).fetchall()
                parts = [_load_json_field(part_row["data"]) for part_row in part_rows]

                if role == "user":
                    content = _extract_opencode_user_content(parts, anonymizer)
                    if content is not None:
                        messages.append({"role": "user", "content": content, "timestamp": timestamp})
                        stats["user_messages"] += 1
                        _update_time_bounds(metadata, timestamp)
                elif role == "assistant":
                    msg = _extract_opencode_assistant_content(parts, anonymizer, include_thinking)
                    if msg:
                        msg["timestamp"] = timestamp
                        messages.append(msg)
                        stats["assistant_messages"] += 1
                        stats["tool_uses"] += len(msg.get("tool_uses", []))
                        _update_time_bounds(metadata, timestamp)

                    tokens = message_data.get("tokens", {})
                    if isinstance(tokens, dict):
                        cache = tokens.get("cache", {})
                        cache_read = _safe_int(cache.get("read")) if isinstance(cache, dict) else 0
                        cache_write = _safe_int(cache.get("write")) if isinstance(cache, dict) else 0
                        # OpenCode reports non-cached input separately from cache
                        # read/write. Keep them in the dedicated buckets so cache
                        # reads price at 0.1x (see pricing.estimate_cost), not 1.0x.
                        stats["input_tokens"] += _safe_int(tokens.get("input"))
                        stats["output_tokens"] += _safe_int(tokens.get("output"))
                        stats["cache_read_tokens"] += cache_read
                        stats["cache_creation_tokens"] += cache_write
    except (sqlite3.Error, OSError):
        return None

    if metadata["model"] is None:
        metadata["model"] = "opencode-unknown"

    return _make_session_result(metadata, messages, stats)


def _make_stats() -> dict[str, int]:
    return {
        "user_messages": 0,
        "assistant_messages": 0,
        "tool_uses": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
        "user_interrupts": 0,
    }


_EFFORT_FIELD_ALIASES = (
    "effort",                  # Codex turn_context
    "reasoningEffort",         # OpenAI-flavored SDKs, OpenCode, OpenClaw
    "reasoning_effort",        # Python SDK / Kimi
    "reasoning_effort_level",
)

_EFFORT_NESTED_CONTAINERS = ("options", "providerOptions", "params", "config")


def _extract_model_effort(payload: Any) -> str | None:
    """Return a per-turn model effort ("medium"/"high"/"xhigh"/…) if present.

    Each agent names the knob differently — Codex `turn_context` uses
    ``effort``; OpenAI-style SDKs and OpenCode use ``reasoningEffort``;
    the Python SDK uses ``reasoning_effort``. Some formats nest the field
    under ``options``/``providerOptions``/``params``. When none of the
    aliases are present (Claude Code, Gemini CLI today) the caller keeps
    ``model_effort`` as ``None`` and the column simply won't populate.
    """
    if not isinstance(payload, dict):
        return None
    for key in _EFFORT_FIELD_ALIASES:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    for container_key in _EFFORT_NESTED_CONTAINERS:
        nested = payload.get(container_key)
        if isinstance(nested, dict):
            inner = _extract_model_effort(nested)
            if inner is not None:
                return inner
    return None


def _make_session_result(
    metadata: dict[str, Any], messages: list[dict[str, Any]], stats: dict[str, int],
) -> dict[str, Any] | None:
    if not messages:
        return None
    result = {
        "session_id": metadata["session_id"],
        "model": metadata["model"],
        "git_branch": metadata["git_branch"],
        "start_time": metadata["start_time"],
        "end_time": metadata["end_time"],
        "messages": messages,
        "stats": stats,
    }
    # Pass through optional provenance fields from metadata
    for key in ("entrypoint", "originator", "codex_source", "model_effort", "segment_title"):
        val = metadata.get(key)
        if val is not None:
            result[key] = val
    return result


def _build_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build a map of tool_use_id -> {output, status} from tool_result blocks."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "user":
            continue
        for block in entry.get("message", {}).get("content", []):
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            tid = block.get("tool_use_id")
            if not tid:
                continue
            is_error = bool(block.get("is_error"))
            content = block.get("content", "")
            if isinstance(content, list):
                text = "\n\n".join(
                    part.get("text", "") for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                ).strip()
            else:
                text = str(content).strip() if content else ""
            result[tid] = {
                "output": {"text": anonymizer.text(text)} if text else {},
                "status": "error" if is_error else "success",
            }
    return result


def _parse_claude_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    messages: list[dict[str, Any]] = []
    metadata = {
        "session_id": filepath.stem,
        "cwd": None,
        "git_branch": None,
        "claude_version": None,
        "model": None,
        "model_effort": None,
        "start_time": None,
        "end_time": None,
        "entrypoint": None,
    }
    stats = _make_stats()

    try:
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    tool_result_map = _build_tool_result_map(entries, anonymizer)
    for entry in entries:
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking, tool_result_map)

    return _make_session_result(metadata, messages, stats)


def _parse_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    """Backward-compatible alias for the Claude parser used by tests."""
    return _parse_claude_session_file(filepath, anonymizer, include_thinking)


def _find_subagent_only_sessions(project_dir: Path) -> list[Path]:
    """Find session directories that have subagent data but no root-level JSONL.

    Some Claude Code sessions (especially those run entirely via the Task tool)
    store conversation data only in ``<uuid>/subagents/agent-*.jsonl`` without
    writing a root-level ``<uuid>.jsonl`` file.  This function identifies those
    directories so they can be parsed separately.
    """
    root_stems = {f.stem for f in project_dir.glob("*.jsonl")}
    sessions = []
    for entry in sorted(project_dir.iterdir()):
        if not entry.is_dir() or entry.name in root_stems:
            continue
        subagent_dir = entry / "subagents"
        if subagent_dir.is_dir() and any(subagent_dir.glob("agent-*.jsonl")):
            sessions.append(entry)
    return sessions


def _parse_subagent_session(
    session_dir: Path, anonymizer: Anonymizer, include_thinking: bool = True,
) -> dict | None:
    """Merge subagent JSONL files into a single session and parse it.

    Reads all ``agent-*.jsonl`` files from the session's ``subagents/``
    directory, sorts entries by timestamp, and feeds them through the
    standard Claude entry processor.
    """
    subagent_dir = session_dir / "subagents"
    if not subagent_dir.is_dir():
        return None

    # Collect all entries with their timestamps for sorting.
    timed_entries: list[tuple[str, dict[str, Any]]] = []
    for sa_file in sorted(subagent_dir.glob("agent-*.jsonl")):
        for entry in _iter_jsonl(sa_file):
            ts = entry.get("timestamp", "")
            timed_entries.append((ts if isinstance(ts, str) else "", entry))

    if not timed_entries:
        return None

    timed_entries.sort(key=lambda pair: pair[0])

    messages: list[dict[str, Any]] = []
    metadata = {
        "session_id": session_dir.name,
        "cwd": None,
        "git_branch": None,
        "claude_version": None,
        "model": None,
        "model_effort": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    entries = [entry for _ts, entry in timed_entries]
    tool_result_map = _build_tool_result_map(entries, anonymizer)
    for entry in entries:
        _process_entry(entry, messages, metadata, stats, anonymizer, include_thinking, tool_result_map)

    return _make_session_result(metadata, messages, stats)


def _parse_gemini_tool_call(tc: dict, anonymizer: Anonymizer) -> dict:
    """Parse a Gemini tool call into a structured dict with input/output/status."""
    name = tc.get("name")
    args = tc.get("args", {})
    status = tc.get("status", "unknown")
    result_list = tc.get("result") or []

    # --- Extract output text from functionResponse ---
    output_text: str | None = None
    extra_texts: list[str] = []
    for item in result_list:
        if not isinstance(item, dict):
            continue
        if "functionResponse" in item:
            resp = item["functionResponse"].get("response", {})
            output_text = resp.get("output")
        elif "text" in item:
            extra_texts.append(item["text"])

    # --- Build structured input (reuses generic field classification) ---
    inp = _parse_tool_input(name, args, anonymizer)

    # --- Build structured output ---
    if name == "read_many_files":
        # Parse "--- /path/to/file ---\n<content>" blocks from extra text parts
        files: list[dict] = []
        for raw in extra_texts:
            lines = raw.split("\n")
            current_path: str | None = None
            content_lines: list[str] = []
            for line in lines:
                if line.startswith("--- ") and line.endswith(" ---"):
                    if current_path is not None:
                        files.append({
                            "path": anonymizer.path(current_path),
                            "content": anonymizer.text("\n".join(content_lines).strip()),
                        })
                    current_path = line[4:-4].strip()
                    content_lines = []
                else:
                    content_lines.append(line)
            if current_path is not None:
                files.append({
                    "path": anonymizer.path(current_path),
                    "content": anonymizer.text("\n".join(content_lines).strip()),
                })
        out: dict = {"files": files}
    elif name == "run_shell_command" and output_text:
        # Parse "Command: ...\nDirectory: ...\nOutput: ...\nExit Code: ..." format
        parsed: dict = {}
        current_key: str | None = None
        current_val: list[str] = []
        for line in output_text.splitlines():
            for key, prefix in (("command", "Command: "), ("directory", "Directory: "),
                                 ("output", "Output: "), ("exit_code", "Exit Code: ")):
                if line.startswith(prefix):
                    if current_key:
                        parsed[current_key] = "\n".join(current_val).strip()
                    current_key = key
                    current_val = [line[len(prefix):]]
                    break
            else:
                if current_key:
                    current_val.append(line)
        if current_key:
            parsed[current_key] = "\n".join(current_val).strip()
        if "exit_code" in parsed:
            try:
                parsed["exit_code"] = int(parsed["exit_code"])
            except ValueError:
                pass
        if "command" in parsed:
            parsed["command"] = anonymizer.text(parsed["command"])
        if "directory" in parsed:
            parsed["directory"] = anonymizer.path(parsed["directory"])
        if "output" in parsed:
            parsed["output"] = anonymizer.text(parsed["output"])
        out = parsed
    elif output_text is not None:
        out = {"text": anonymizer.text(output_text)}
    else:
        out = {}

    result: dict = {"tool": name, "input": inp, "output": out, "status": status}
    return result


def _parse_gemini_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    try:
        with open(filepath, encoding="utf-8", errors="replace") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

    messages = []
    metadata = {
        "session_id": data.get("sessionId", filepath.stem),
        "cwd": None,
        "git_branch": None,
        "model": None,
        "model_effort": None,
        "start_time": data.get("startTime"),
        "end_time": data.get("lastUpdated"),
    }
    stats = _make_stats()

    for msg_data in data.get("messages", []):
        msg_type = msg_data.get("type")
        timestamp = msg_data.get("timestamp")

        if msg_type == "user":
            content = msg_data.get("content")
            if isinstance(content, list):
                text_parts = [part.get("text", "") for part in content if isinstance(part, dict) and "text" in part]
                text = "\n".join(text_parts)
            elif isinstance(content, str):
                text = content
            else:
                continue
            if not text.strip():
                continue
            messages.append({
                "role": "user",
                "content": anonymizer.text(text.strip()),
                "timestamp": timestamp,
            })
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

        elif msg_type == "gemini":
            if metadata["model"] is None:
                metadata["model"] = msg_data.get("model")
            if metadata.get("model_effort") is None:
                metadata["model_effort"] = _extract_model_effort(msg_data)

            tokens = msg_data.get("tokens", {})
            if tokens:
                # Gemini's `input` (promptTokenCount) already INCLUDES `cached`
                # (cachedContentTokenCount) as a subset, so subtract to recover the
                # non-cached input and price the cached portion at 0.1x as a cache
                # read (mirrors the Codex/OpenAI subset handling below).
                raw_input = _safe_int(tokens.get("input"))
                cached = _safe_int(tokens.get("cached"))
                stats["input_tokens"] += max(0, raw_input - cached)
                stats["output_tokens"] += _safe_int(tokens.get("output"))
                stats["cache_read_tokens"] += cached

            msg = {"role": "assistant"}
            if timestamp:
                msg["timestamp"] = timestamp

            content = msg_data.get("content")
            if isinstance(content, str) and content.strip():
                msg["content"] = anonymizer.text(content.strip())

            if include_thinking:
                thoughts = msg_data.get("thoughts", [])
                if thoughts:
                    thought_texts = []
                    for t in thoughts:
                        if "description" in t and isinstance(t["description"], str):
                            thought_texts.append(t["description"].strip())
                    if thought_texts:
                        msg["thinking"] = anonymizer.text("\n\n".join(thought_texts))

            tool_uses = []
            for tc in msg_data.get("toolCalls", []):
                tool_uses.append(_parse_gemini_tool_call(tc, anonymizer))

            if tool_uses:
                msg["tool_uses"] = tool_uses
                stats["tool_uses"] += len(tool_uses)

            if "content" in msg or "thinking" in msg or "tool_uses" in msg:
                messages.append(msg)
                stats["assistant_messages"] += 1
                _update_time_bounds(metadata, timestamp)

    return _make_session_result(metadata, messages, stats)


def _parse_openclaw_session_file(
    filepath: Path, anonymizer: Anonymizer, include_thinking: bool = True
) -> dict | None:
    """Parse an OpenClaw session JSONL file into a structured conversation."""
    try:
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    if not entries:
        return None

    # First entry is the session header
    header = entries[0]
    if header.get("type") != "session":
        return None

    metadata: dict[str, Any] = {
        "session_id": header.get("id", filepath.stem),
        "cwd": None,
        "git_branch": None,
        "model": None,
        "model_effort": None,
        "start_time": header.get("timestamp"),
        "end_time": None,
    }
    cwd = header.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        metadata["cwd"] = anonymizer.path(cwd)

    messages: list[dict[str, Any]] = []
    stats = _make_stats()

    # Pre-pass: build tool result map from toolResult messages
    tool_result_map: dict[str, dict] = {}
    for entry in entries[1:]:
        if entry.get("type") != "message":
            continue
        msg_data = entry.get("message", {})
        if msg_data.get("role") != "toolResult":
            continue
        tool_call_id = msg_data.get("toolCallId")
        if not tool_call_id:
            continue
        is_error = bool(msg_data.get("isError"))
        content = msg_data.get("content", [])
        if isinstance(content, list):
            text_parts = [
                b.get("text", "") for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            output_text = "\n".join(text_parts).strip()
        elif isinstance(content, str):
            output_text = content.strip()
        else:
            output_text = ""
        tool_result_map[tool_call_id] = {
            "output": {"text": anonymizer.text(output_text)} if output_text else {},
            "status": "error" if is_error else "success",
        }

    # Main pass: process message entries
    for entry in entries[1:]:
        entry_type = entry.get("type")
        timestamp = entry.get("timestamp")

        if entry_type == "model_change":
            provider = entry.get("provider", "")
            model_id = entry.get("modelId", "")
            if model_id:
                metadata["model"] = f"{provider}/{model_id}" if provider else model_id
            effort = _extract_model_effort(entry)
            if effort is not None:
                metadata["model_effort"] = effort

        if entry_type == "compaction":
            comp_ts = timestamp
            messages.append({
                "role": "system",
                "content": "[compaction]",
                "timestamp": comp_ts,
                "_compaction": True,
                "_compaction_summary": entry.get("summary", ""),
            })
            _update_time_bounds(metadata, comp_ts)
            continue

        if entry_type != "message":
            continue

        msg_data = entry.get("message", {})
        role = msg_data.get("role")
        msg_ts = msg_data.get("timestamp")
        # Message-level timestamp is epoch ms; entry-level is ISO string
        if isinstance(msg_ts, (int, float)):
            msg_ts = _normalize_timestamp(msg_ts)
        effective_ts = msg_ts or timestamp

        if role == "user":
            content = msg_data.get("content")
            if isinstance(content, list):
                text_parts = [
                    b.get("text", "") for b in content
                    if isinstance(b, dict) and b.get("type") == "text"
                ]
                text = "\n".join(text_parts)
            elif isinstance(content, str):
                text = content
            else:
                continue
            if not text.strip():
                continue
            messages.append({
                "role": "user",
                "content": anonymizer.text(text.strip()),
                "timestamp": effective_ts,
            })
            stats["user_messages"] += 1
            _update_time_bounds(metadata, effective_ts)

        elif role == "assistant":
            model = msg_data.get("model")
            if model and metadata["model"] is None:
                provider = msg_data.get("provider", "")
                metadata["model"] = f"{provider}/{model}" if provider else model
            if metadata.get("model_effort") is None:
                metadata["model_effort"] = _extract_model_effort(msg_data)

            usage = msg_data.get("usage", {})
            if isinstance(usage, dict):
                # OpenClaw uses the Claude wire shape: `input` is non-cached and
                # cache reads/writes are reported separately. Route them to the
                # dedicated buckets so cache reads price at 0.1x, not 1.0x.
                stats["input_tokens"] += _safe_int(usage.get("input"))
                stats["output_tokens"] += _safe_int(usage.get("output"))
                stats["cache_read_tokens"] += _safe_int(usage.get("cacheRead"))
                stats["cache_creation_tokens"] += (
                    _safe_int(usage.get("cacheCreation")) + _safe_int(usage.get("cacheWrite"))
                )

            content = msg_data.get("content", [])
            if not isinstance(content, list):
                continue

            text_parts: list[str] = []
            thinking_parts: list[str] = []
            tool_uses: list[dict[str, Any]] = []

            for block in content:
                if not isinstance(block, dict):
                    continue
                block_type = block.get("type")

                if block_type == "text":
                    text = block.get("text", "")
                    if isinstance(text, str) and text.strip():
                        text_parts.append(anonymizer.text(text.strip()))

                elif block_type == "thinking" and include_thinking:
                    thinking = block.get("thinking", "")
                    if isinstance(thinking, str) and thinking.strip():
                        thinking_parts.append(anonymizer.text(thinking.strip()))

                elif block_type == "toolCall":
                    tool_name = block.get("name")
                    args = block.get("arguments", {})
                    tool_entry: dict[str, Any] = {
                        "tool": tool_name,
                        "input": _parse_tool_input(tool_name, args, anonymizer),
                    }
                    tool_call_id = block.get("id")
                    if tool_call_id and tool_call_id in tool_result_map:
                        result = tool_result_map[tool_call_id]
                        if result.get("output"):
                            tool_entry["output"] = result["output"]
                        if result.get("status"):
                            tool_entry["status"] = result["status"]
                    tool_uses.append(tool_entry)

            if not text_parts and not thinking_parts and not tool_uses:
                continue

            msg: dict[str, Any] = {"role": "assistant"}
            if effective_ts:
                msg["timestamp"] = effective_ts
            if text_parts:
                msg["content"] = "\n\n".join(text_parts)
            if thinking_parts:
                msg["thinking"] = "\n\n".join(thinking_parts)
            if tool_uses:
                msg["tool_uses"] = tool_uses
                stats["tool_uses"] += len(tool_uses)

            messages.append(msg)
            stats["assistant_messages"] += 1
            _update_time_bounds(metadata, effective_ts)

        elif role == "bashExecution":
            # Standalone bash execution (interactive shell, not via toolCall/toolResult)
            command = msg_data.get("command", "")
            output = msg_data.get("output", "")
            exit_code = msg_data.get("exitCode")
            is_error = exit_code is not None and exit_code != 0
            tool_entry: dict[str, Any] = {
                "tool": "bash",
                "input": {"command": anonymizer.text(command)} if command else {},
            }
            out_dict: dict[str, Any] = {}
            if output:
                out_dict["text"] = anonymizer.text(output.strip())
            if exit_code is not None:
                out_dict["exit_code"] = exit_code
            if out_dict:
                tool_entry["output"] = out_dict
            tool_entry["status"] = "error" if is_error else "success"
            messages.append({
                "role": "assistant",
                "tool_uses": [tool_entry],
                "timestamp": effective_ts,
            })
            stats["assistant_messages"] += 1
            stats["tool_uses"] += 1
            _update_time_bounds(metadata, effective_ts)

    if metadata["model"] is None:
        metadata["model"] = "openclaw-unknown"

    return _make_session_result(metadata, messages, stats)


@dataclasses.dataclass
class _CodexParseState:
    messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    metadata: dict[str, Any] = dataclasses.field(default_factory=dict)
    stats: dict[str, int] = dataclasses.field(default_factory=_make_stats)
    pending_tool_uses: list[dict[str, str | None]] = dataclasses.field(default_factory=list)
    pending_thinking: list[str] = dataclasses.field(default_factory=list)
    _pending_thinking_seen: set[str] = dataclasses.field(default_factory=set)
    raw_cwd: str = UNKNOWN_CODEX_CWD
    max_input_tokens: int = 0
    max_output_tokens: int = 0
    max_cached_tokens: int = 0
    tool_result_map: dict[str, dict] = dataclasses.field(default_factory=dict)


def _coalesce_codex_output(raw: Any) -> str:
    """Normalize a Codex tool `output` field to a string.

    Codex may deliver output as a plain string or as a list of OpenAI-style
    content blocks (``input_text``, ``input_image``). Only text is preserved;
    non-text blocks are dropped.
    """
    if isinstance(raw, str):
        return raw
    if isinstance(raw, list):
        parts: list[str] = []
        for block in raw:
            if isinstance(block, dict) and block.get("type") == "input_text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return ""


def _build_codex_tool_result_map(entries: list[dict[str, Any]], anonymizer: Anonymizer) -> dict[str, dict]:
    """Pre-pass: build call_id -> {output, status} from function_call_output and custom_tool_call_output."""
    result: dict[str, dict] = {}
    for entry in entries:
        if entry.get("type") != "response_item":
            continue
        p = entry.get("payload", {})
        pt = p.get("type")
        call_id = p.get("call_id")
        if not call_id:
            continue

        if pt == "function_call_output":
            raw_output = p.get("output", "")
            out: dict = {}
            if isinstance(raw_output, str):
                # Parse "Exit code: N\nWall time: ...\nOutput:\n..." format
                output_lines: list[str] = []
                in_output = False
                for line in raw_output.splitlines():
                    if line.startswith("Exit code: "):
                        try:
                            out["exit_code"] = int(line[len("Exit code: "):].strip())
                        except ValueError:
                            out["exit_code"] = line[len("Exit code: "):].strip()
                    elif line.startswith("Wall time: "):
                        out["wall_time"] = line[len("Wall time: "):].strip()
                    elif line == "Output:":
                        in_output = True
                    elif in_output:
                        output_lines.append(line)
                if output_lines:
                    out["output"] = anonymizer.text("\n".join(output_lines).strip())
            else:
                text = _coalesce_codex_output(raw_output)
                if text:
                    out["output"] = anonymizer.text(text)
            result[call_id] = {"output": out, "status": "success"}

        elif pt == "custom_tool_call_output":
            raw_output = p.get("output", "")
            out = {}
            if isinstance(raw_output, str):
                try:
                    parsed = json.loads(raw_output)
                    text = parsed.get("output", "")
                    if text:
                        out["output"] = anonymizer.text(str(text))
                    meta = parsed.get("metadata", {})
                    if "exit_code" in meta:
                        out["exit_code"] = meta["exit_code"]
                    if "duration_seconds" in meta:
                        out["duration_seconds"] = meta["duration_seconds"]
                except (json.JSONDecodeError, AttributeError):
                    if raw_output:
                        out["output"] = anonymizer.text(raw_output)
            else:
                text = _coalesce_codex_output(raw_output)
                if text:
                    out["output"] = anonymizer.text(text)
            result[call_id] = {"output": out, "status": "success"}

    return result


def _parse_codex_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
) -> dict | None:
    state = _CodexParseState(
        metadata={
            "session_id": filepath.stem,
            "cwd": None,
            "git_branch": None,
            "model": None,
            "start_time": None,
            "end_time": None,
            "model_provider": None,
            "originator": None,
            "codex_source": None,
            # Codex logs `effort` (e.g. "medium"/"high"/"xhigh") in every
            # `turn_context` event. Keep the first non-empty value so we
            # attribute the session to the mode it was launched in.
            "model_effort": None,
        },
    )

    try:
        entries = list(_iter_jsonl(filepath))
    except OSError:
        return None

    state.tool_result_map = _build_codex_tool_result_map(entries, anonymizer)

    for entry in entries:
        timestamp = _normalize_timestamp(entry.get("timestamp"))
        entry_type = entry.get("type")

        if entry_type == "session_meta":
            _handle_codex_session_meta(state, entry, filepath, anonymizer)
        elif entry_type == "turn_context":
            _handle_codex_turn_context(state, entry, anonymizer)
        elif entry_type == "response_item":
            _handle_codex_response_item(state, entry, anonymizer, include_thinking)
        elif entry_type == "event_msg":
            payload = entry.get("payload", {})
            event_type = payload.get("type")
            if event_type == "token_count":
                _handle_codex_token_count(state, payload)
            elif event_type == "agent_reasoning" and include_thinking:
                thinking = payload.get("text")
                if isinstance(thinking, str) and thinking.strip():
                    cleaned = anonymizer.text(thinking.strip())
                    if cleaned not in state._pending_thinking_seen:
                        state._pending_thinking_seen.add(cleaned)
                        state.pending_thinking.append(cleaned)
            elif event_type == "user_message":
                _handle_codex_user_message(state, payload, timestamp, anonymizer)
            elif event_type == "agent_message":
                _handle_codex_agent_message(state, payload, timestamp, anonymizer, include_thinking)

    state.stats["input_tokens"] = state.max_input_tokens
    state.stats["output_tokens"] = state.max_output_tokens
    state.stats["cache_read_tokens"] = state.max_cached_tokens

    if state.raw_cwd != target_cwd:
        return None

    _flush_codex_pending(state, timestamp=state.metadata["end_time"])

    if state.metadata["model"] is None:
        model_provider = state.metadata.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            state.metadata["model"] = f"{model_provider}-codex"
        else:
            state.metadata["model"] = "codex-unknown"

    return _make_session_result(state.metadata, state.messages, state.stats)


def _handle_codex_session_meta(
    state: _CodexParseState, entry: dict[str, Any], filepath: Path,
    anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        state.raw_cwd = session_cwd
        if state.metadata["cwd"] is None:
            state.metadata["cwd"] = anonymizer.path(session_cwd)
    if state.metadata["session_id"] == filepath.stem:
        state.metadata["session_id"] = payload.get("id", state.metadata["session_id"])
    if state.metadata["model_provider"] is None:
        state.metadata["model_provider"] = payload.get("model_provider")
    git_info = payload.get("git", {})
    if isinstance(git_info, dict) and state.metadata["git_branch"] is None:
        state.metadata["git_branch"] = git_info.get("branch")
    if state.metadata.get("originator") is None:
        originator = payload.get("originator")
        if isinstance(originator, str):
            state.metadata["originator"] = originator
    if state.metadata.get("codex_source") is None:
        codex_src = payload.get("source")
        if isinstance(codex_src, str):
            state.metadata["codex_source"] = codex_src


def _handle_codex_turn_context(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        state.raw_cwd = session_cwd
        if state.metadata["cwd"] is None:
            state.metadata["cwd"] = anonymizer.path(session_cwd)
    if state.metadata["model"] is None:
        model_name = payload.get("model")
        if isinstance(model_name, str) and model_name.strip():
            state.metadata["model"] = model_name
    if state.metadata.get("model_effort") is None:
        state.metadata["model_effort"] = _extract_model_effort(payload)


def _handle_codex_response_item(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
    include_thinking: bool,
) -> None:
    payload = entry.get("payload", {})
    item_type = payload.get("type")
    if item_type == "function_call":
        tool_name = payload.get("name")
        args_data = _parse_codex_tool_arguments(payload.get("arguments"))
        state.pending_tool_uses.append(
            {
                "tool": tool_name,
                "input": _parse_tool_input(tool_name, args_data, anonymizer),
                "_call_id": payload.get("call_id"),
            }
        )
    elif item_type == "custom_tool_call":
        tool_name = payload.get("name")
        raw_input = payload.get("input", "")
        inp = {"patch": anonymizer.text(raw_input)} if isinstance(raw_input, str) else _parse_tool_input(tool_name, raw_input, anonymizer)
        state.pending_tool_uses.append(
            {
                "tool": tool_name,
                "input": inp,
                "_call_id": payload.get("call_id"),
            }
        )
    elif item_type == "reasoning" and include_thinking:
        for summary in payload.get("summary", []):
            if not isinstance(summary, dict):
                continue
            text = summary.get("text")
            if isinstance(text, str) and text.strip():
                cleaned = anonymizer.text(text.strip())
                if cleaned not in state._pending_thinking_seen:
                    state._pending_thinking_seen.add(cleaned)
                    state.pending_thinking.append(cleaned)


def _handle_codex_token_count(state: _CodexParseState, payload: dict[str, Any]) -> None:
    info = payload.get("info", {})
    if isinstance(info, dict):
        total_usage = info.get("total_token_usage", {})
        if isinstance(total_usage, dict):
            input_tokens_total = _safe_int(total_usage.get("input_tokens"))
            cached_tokens = _safe_int(total_usage.get("cached_input_tokens"))
            output_tokens = _safe_int(total_usage.get("output_tokens"))
            # OpenAI's input_tokens includes cached_input_tokens as a subset,
            # so subtract to get non-cached input only
            non_cached_input = max(0, input_tokens_total - cached_tokens)
            state.max_input_tokens = max(state.max_input_tokens, non_cached_input)
            state.max_output_tokens = max(state.max_output_tokens, output_tokens)
            state.max_cached_tokens = max(state.max_cached_tokens, cached_tokens)


def _handle_codex_user_message(
    state: _CodexParseState, payload: dict[str, Any],
    timestamp: str | None, anonymizer: Anonymizer,
) -> None:
    _flush_codex_pending(state, timestamp)
    content = payload.get("message")
    if isinstance(content, str) and content.strip():
        state.messages.append(
            {
                "role": "user",
                "content": anonymizer.text(content.strip()),
                "timestamp": timestamp,
            }
        )
        state.stats["user_messages"] += 1
        _update_time_bounds(state.metadata, timestamp)


def _resolve_codex_tool_uses(state: _CodexParseState) -> list[dict]:
    """Attach outputs from tool_result_map and strip internal _call_id field."""
    resolved = []
    for tu in state.pending_tool_uses:
        call_id = tu.pop("_call_id", None)
        if call_id and call_id in state.tool_result_map:
            r = state.tool_result_map[call_id]
            tu["output"] = r["output"]
            tu["status"] = r["status"]
        resolved.append(tu)
    return resolved


def _handle_codex_agent_message(
    state: _CodexParseState, payload: dict[str, Any],
    timestamp: str | None, anonymizer: Anonymizer, include_thinking: bool,
) -> None:
    content = payload.get("message")
    msg: dict[str, Any] = {"role": "assistant"}
    if isinstance(content, str) and content.strip():
        msg["content"] = anonymizer.text(content.strip())
    if state.pending_thinking and include_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = _resolve_codex_tool_uses(state)

    if len(msg) > 1:
        msg["timestamp"] = timestamp
        state.messages.append(msg)
        state.stats["assistant_messages"] += 1
        state.stats["tool_uses"] += len(msg.get("tool_uses", []))
        _update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def _flush_codex_pending(state: _CodexParseState, timestamp: str | None) -> None:
    if not state.pending_tool_uses and not state.pending_thinking:
        return

    msg: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
    if state.pending_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = _resolve_codex_tool_uses(state)

    state.messages.append(msg)
    state.stats["assistant_messages"] += 1
    state.stats["tool_uses"] += len(msg.get("tool_uses", []))
    _update_time_bounds(state.metadata, timestamp)

    state.pending_tool_uses.clear()
    state.pending_thinking.clear()
    state._pending_thinking_seen.clear()


def _parse_codex_tool_arguments(arguments: Any) -> Any:
    if isinstance(arguments, dict):
        return arguments
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return arguments
        return parsed
    return arguments


def _update_time_bounds(metadata: dict[str, Any], timestamp: str | None) -> None:
    if timestamp is None:
        return
    if metadata["start_time"] is None:
        metadata["start_time"] = timestamp
    metadata["end_time"] = timestamp


def _safe_int(value: Any) -> int:
    if isinstance(value, (int, float)):
        return int(value)
    return 0


def _load_json_field(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _extract_opencode_model(message_data: dict[str, Any]) -> str | None:
    model = message_data.get("model")
    if not isinstance(model, dict):
        return None
    provider_id = model.get("providerID")
    model_id = model.get("modelID")
    if isinstance(provider_id, str) and provider_id.strip() and isinstance(model_id, str) and model_id.strip():
        return f"{provider_id}/{model_id}"
    if isinstance(model_id, str) and model_id.strip():
        return model_id
    return None


def _extract_opencode_user_content(parts: list[dict[str, Any]], anonymizer: Anonymizer) -> str | None:
    text_parts: list[str] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        if part.get("type") != "text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text.strip():
            text_parts.append(anonymizer.text(text.strip()))

    if not text_parts:
        return None
    return "\n\n".join(text_parts)


def _extract_opencode_assistant_content(
    parts: list[dict[str, Any]], anonymizer: Anonymizer, include_thinking: bool,
) -> dict[str, Any] | None:
    text_parts: list[str] = []
    thinking_parts: list[str] = []
    tool_uses: list[dict[str, str | None]] = []

    for part in parts:
        if not isinstance(part, dict):
            continue
        part_type = part.get("type")

        if part_type == "text":
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                text_parts.append(anonymizer.text(text.strip()))
        elif part_type == "reasoning" and include_thinking:
            text = part.get("text")
            if isinstance(text, str) and text.strip():
                thinking_parts.append(anonymizer.text(text.strip()))
        elif part_type == "tool":
            tool_name = part.get("tool")
            state = part.get("state", {})
            tool_input = state.get("input", {}) if isinstance(state, dict) else {}
            tu: dict[str, Any] = {
                "tool": tool_name,
                "input": _parse_tool_input(tool_name, tool_input, anonymizer),
            }
            if isinstance(state, dict):
                status = state.get("status")
                if isinstance(status, str):
                    tu["status"] = "success" if status == "completed" else status
                output = state.get("output")
                if isinstance(output, str) and output:
                    tu["output"] = {"text": anonymizer.text(output)}
                elif output is not None:
                    tu["output"] = {}
            tool_uses.append(tu)

    if not text_parts and not thinking_parts and not tool_uses:
        return None

    msg: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    if thinking_parts:
        msg["thinking"] = "\n\n".join(thinking_parts)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg


def _get_codex_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _CODEX_PROJECT_INDEX
    if refresh or not _CODEX_PROJECT_INDEX:
        _CODEX_PROJECT_INDEX = _build_codex_project_index()
    return _CODEX_PROJECT_INDEX


def _build_codex_project_index() -> dict[str, list[Path]]:
    index: dict[str, list[Path]] = {}
    for session_file in _iter_codex_session_files():
        cwd = _extract_codex_cwd(session_file) or UNKNOWN_CODEX_CWD
        index.setdefault(cwd, []).append(session_file)
    return index


def _iter_codex_session_files() -> list[Path]:
    files: list[Path] = []
    if CODEX_SESSIONS_DIR.exists():
        files.extend(sorted(CODEX_SESSIONS_DIR.rglob("*.jsonl")))
    if CODEX_ARCHIVED_DIR.exists():
        files.extend(sorted(CODEX_ARCHIVED_DIR.glob("*.jsonl")))
    return files


def _extract_codex_cwd(session_file: Path) -> str | None:
    try:
        for entry in _iter_jsonl(session_file):
            if entry.get("type") in ("session_meta", "turn_context"):
                cwd = entry.get("payload", {}).get("cwd")
                if isinstance(cwd, str) and cwd.strip():
                    return cwd
    except OSError:
        return None
    return None


def _first_cwd_in_session_file(session_file: Path) -> str | None:
    """Return the first absolute ``cwd`` recorded in a session JSONL, or None."""
    try:
        with open(session_file, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = obj.get("cwd") if isinstance(obj, dict) else None
                if isinstance(cwd, str) and cwd:
                    return cwd
    except OSError:
        return None
    return None


def _read_project_cwd(*project_dirs: Path) -> str | None:
    """Recover the project's real working directory from session content.

    Claude encodes the project dir name as the cwd with ``/`` replaced by
    ``-``, which is lossy when a folder name itself contains ``-`` (e.g.
    ``llm-gateway-infra`` is indistinguishable from ``llm/gateway/infra``).
    Each Claude session JSONL records the absolute ``cwd``, so reading it
    recovers the true basename. Searches the given directories — root sessions
    first, then nested subagent / local-agent transcripts — and returns the
    first ``cwd`` found, or None."""
    for project_dir in project_dirs:
        if not project_dir:
            continue
        try:
            roots = sorted(project_dir.glob("*.jsonl"))
        except OSError:
            roots = []
        for session_file in roots:
            cwd = _first_cwd_in_session_file(session_file)
            if cwd:
                return cwd
        # Nested fallback: subagent-only projects have no root *.jsonl; subagent
        # and local-agent transcripts live in subdirectories. Materialize inside
        # the try — rglob is lazy and raises OSError during iteration (e.g. an
        # unreadable subdir), not at creation — and sort for deterministic order.
        try:
            nested = sorted(project_dir.rglob("*.jsonl"))
        except OSError:
            nested = []
        for session_file in nested:
            cwd = _first_cwd_in_session_file(session_file)
            if cwd:
                return cwd
    return None


def _build_codex_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_CODEX_CWD:
        return "codex:unknown"
    return f"codex:{Path(cwd).name or cwd}"


def _build_opencode_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_OPENCODE_CWD:
        return "opencode:unknown"
    return f"opencode:{Path(cwd).name or cwd}"


def _build_openclaw_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_OPENCLAW_CWD:
        return "openclaw:unknown"
    return f"openclaw:{Path(cwd).name or cwd}"


def _build_gemini_project_name(project_hash: str) -> str:
    return f"gemini:{_resolve_gemini_hash(project_hash)}"


def _build_custom_project_name(dir_name: str) -> str:
    return f"custom:{dir_name}"


def _get_opencode_project_index(refresh: bool = False) -> dict[str, list[str]]:
    global _OPENCODE_PROJECT_INDEX
    if refresh or not _OPENCODE_PROJECT_INDEX:
        _OPENCODE_PROJECT_INDEX = _build_opencode_project_index()
    return _OPENCODE_PROJECT_INDEX


def _parse_kimi_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Parse a Kimi CLI context.jsonl file into structured session data."""
    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": filepath.parent.name,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "model_effort": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        for entry in _iter_jsonl(filepath):
            role = entry.get("role")
            if metadata.get("model_effort") is None:
                metadata["model_effort"] = _extract_model_effort(entry)

            if role == "user":
                content = entry.get("content")
                if isinstance(content, str) and content.strip():
                    messages.append({
                        "role": "user",
                        "content": anonymizer.text(content.strip()),
                        "timestamp": None,
                    })
                    stats["user_messages"] += 1

            elif role == "assistant":
                msg: dict[str, Any] = {"role": "assistant"}

                content = entry.get("content")
                text_parts = []
                thinking_parts = []

                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text":
                            text = block.get("text", "").strip()
                            if text:
                                text_parts.append(anonymizer.text(text))
                        elif block_type == "think" and include_thinking:
                            think = block.get("think", "").strip()
                            if think:
                                thinking_parts.append(anonymizer.text(think))

                if text_parts:
                    msg["content"] = "\n\n".join(text_parts)
                if thinking_parts:
                    msg["thinking"] = "\n\n".join(thinking_parts)

                tool_calls = entry.get("tool_calls", [])
                tool_uses = []
                if isinstance(tool_calls, list):
                    for tc in tool_calls:
                        if not isinstance(tc, dict):
                            continue
                        func = tc.get("function", {})
                        if isinstance(func, dict):
                            tool_name = func.get("name")
                            args_str = func.get("arguments", "")
                            try:
                                args = json.loads(args_str) if isinstance(args_str, str) else args_str
                            except json.JSONDecodeError:
                                args = args_str
                            tool_uses.append({
                                "tool": tool_name,
                                "input": _parse_tool_input(tool_name, args, anonymizer),
                            })

                if tool_uses:
                    msg["tool_uses"] = tool_uses
                    stats["tool_uses"] += len(tool_uses)

                if text_parts or thinking_parts or tool_uses:
                    messages.append(msg)
                    stats["assistant_messages"] += 1

            elif role == "_usage":
                token_count = entry.get("token_count")
                if isinstance(token_count, int):
                    stats["output_tokens"] = max(stats["output_tokens"], token_count)

    except OSError:
        return None

    return _make_session_result(metadata, messages, stats)


def _build_opencode_project_index() -> dict[str, list[str]]:
    if not OPENCODE_DB_PATH.exists():
        return {}

    index: dict[str, list[str]] = {}
    try:
        with sqlite3.connect(OPENCODE_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT id, directory FROM session ORDER BY time_updated DESC, id DESC"
            ).fetchall()
    except sqlite3.Error:
        return {}

    for session_id, cwd in rows:
        normalized_cwd = cwd if isinstance(cwd, str) and cwd.strip() else UNKNOWN_OPENCODE_CWD
        if not isinstance(session_id, str) or not session_id:
            continue
        index.setdefault(normalized_cwd, []).append(session_id)
    return index


def _get_openclaw_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _OPENCLAW_PROJECT_INDEX
    if refresh or not _OPENCLAW_PROJECT_INDEX:
        _OPENCLAW_PROJECT_INDEX = _build_openclaw_project_index()
    return _OPENCLAW_PROJECT_INDEX


def _build_openclaw_project_index() -> dict[str, list[Path]]:
    """Scan ~/.openclaw/agents/*/sessions/*.jsonl, read each session header to get cwd."""
    if not OPENCLAW_AGENTS_DIR.exists():
        return {}

    index: dict[str, list[Path]] = {}
    try:
        for agent_dir in sorted(OPENCLAW_AGENTS_DIR.iterdir()):
            sessions_dir = agent_dir / "sessions"
            if not sessions_dir.is_dir():
                continue
            for session_file in sorted(sessions_dir.glob("*.jsonl")):
                cwd = _extract_openclaw_cwd(session_file) or UNKNOWN_OPENCLAW_CWD
                index.setdefault(cwd, []).append(session_file)
    except OSError:
        pass
    return index


def _extract_openclaw_cwd(session_file: Path) -> str | None:
    """Read the first line (session header) of an OpenClaw JSONL file to extract cwd."""
    try:
        with open(session_file, encoding="utf-8", errors="replace") as f:
            first_line = f.readline().strip()
            if not first_line:
                return None
            header = json.loads(first_line)
            if header.get("type") != "session":
                return None
            cwd = header.get("cwd")
            if isinstance(cwd, str) and cwd.strip():
                return cwd
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _process_entry(
    entry: dict[str, Any],
    messages: list[dict[str, Any]],
    metadata: dict[str, Any],
    stats: dict[str, int],
    anonymizer: Anonymizer,
    include_thinking: bool,
    tool_result_map: dict[str, dict] | None = None,
) -> None:
    entry_type = entry.get("type")

    if metadata["cwd"] is None and entry.get("cwd"):
        metadata["cwd"] = anonymizer.path(entry["cwd"])
        metadata["git_branch"] = entry.get("gitBranch")
        metadata["claude_version"] = entry.get("version")
        metadata["session_id"] = entry.get("sessionId", metadata["session_id"])
        if metadata.get("entrypoint") is None:
            metadata["entrypoint"] = entry.get("entrypoint")

    timestamp = _normalize_timestamp(entry.get("timestamp"))

    if entry_type == "user":
        content = _extract_user_content(entry, anonymizer)
        if content is not None:
            messages.append({"role": "user", "content": content, "timestamp": timestamp})
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)
        # Detect user interrupts (Escape key)
        raw_msg = entry.get("message", {})
        raw_content = raw_msg.get("content") if isinstance(raw_msg, dict) else None
        if isinstance(raw_content, list):
            for block in raw_content:
                if isinstance(block, dict) and "[Request interrupted by user" in (block.get("text") or ""):
                    stats["user_interrupts"] += 1
        elif isinstance(raw_content, str) and "[Request interrupted by user" in raw_content:
            stats["user_interrupts"] += 1

    elif entry_type == "assistant":
        msg = _extract_assistant_content(entry, anonymizer, include_thinking, tool_result_map)
        if msg:
            message_payload = entry.get("message", {})
            if metadata["model"] is None:
                metadata["model"] = message_payload.get("model")
            if metadata.get("model_effort") is None:
                metadata["model_effort"] = _extract_model_effort(message_payload)
            usage = entry.get("message", {}).get("usage", {})
            stats["input_tokens"] += usage.get("input_tokens", 0)
            stats["output_tokens"] += usage.get("output_tokens", 0)
            stats["cache_read_tokens"] += usage.get("cache_read_input_tokens", 0)
            stats["cache_creation_tokens"] += usage.get("cache_creation_input_tokens", 0)
            stats["tool_uses"] += len(msg.get("tool_uses", []))
            msg["timestamp"] = timestamp
            messages.append(msg)
            stats["assistant_messages"] += 1
            _update_time_bounds(metadata, timestamp)


def _extract_user_content(entry: dict[str, Any], anonymizer: Anonymizer) -> str | None:
    msg_data = entry.get("message", {})
    content = msg_data.get("content", "")
    if isinstance(content, list):
        text_parts = [b.get("text", "") for b in content if b.get("type") == "text"]
        content = "\n".join(text_parts)
    if not content or not content.strip():
        return None
    return anonymizer.text(content)


def _extract_assistant_content(
    entry: dict[str, Any], anonymizer: Anonymizer, include_thinking: bool,
    tool_result_map: dict[str, dict] | None = None,
) -> dict[str, Any] | None:
    msg_data = entry.get("message", {})
    content_blocks = msg_data.get("content", [])
    if not isinstance(content_blocks, list):
        return None

    text_parts = []
    thinking_parts = []
    tool_uses = []

    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text", "").strip()
            if text:
                text_parts.append(anonymizer.text(text))
        elif block_type == "thinking" and include_thinking:
            thinking = block.get("thinking", "").strip()
            if thinking:
                thinking_parts.append(anonymizer.text(thinking))
        elif block_type == "tool_use":
            tu: dict[str, Any] = {
                "tool": block.get("name"),
                "id": block.get("id"),
                "input": _parse_tool_input(block.get("name"), block.get("input", {}), anonymizer),
            }
            if tool_result_map is not None:
                result = tool_result_map.get(block.get("id", ""))
                if result:
                    tu["output"] = result["output"]
                    tu["status"] = result["status"]
            tool_uses.append(tu)

    if not text_parts and not tool_uses and not thinking_parts:
        return None

    msg: dict[str, Any] = {"role": "assistant"}
    if text_parts:
        msg["content"] = "\n\n".join(text_parts)
    if thinking_parts:
        msg["thinking"] = "\n\n".join(thinking_parts)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    return msg


def _parse_tool_input(tool_name: str | None, input_data: Any, anonymizer: Anonymizer) -> dict:
    """Return the full tool input dict with paths/content anonymized.

    Preserves ALL fields from the original input. Each field is classified
    by name and anonymized appropriately (path, command, text, or pass-through).
    """
    if not isinstance(input_data, dict):
        return {"raw": anonymizer.text(str(input_data))}

    result: dict[str, Any] = {}
    for key, value in input_data.items():
        if key in _PATH_FIELDS:
            result[key] = anonymizer.path(value) if isinstance(value, str) else value
        elif key in _COMMAND_FIELDS:
            if isinstance(value, str):
                redacted, _, _ = redact_text(value)
                result[key] = anonymizer.text(redacted)
            else:
                result[key] = value
        elif key in _TEXT_FIELDS:
            result[key] = anonymizer.text(str(value)) if isinstance(value, str) else value
        elif key in _PATH_LIST_FIELDS:
            if isinstance(value, list):
                result[key] = [anonymizer.path(p) if isinstance(p, str) else p for p in value]
            else:
                result[key] = value
        elif key in _TEXT_LIST_FIELDS:
            if isinstance(value, list):
                result[key] = [anonymizer.text(str(p)) if isinstance(p, str) else p for p in value]
            else:
                result[key] = value
        elif key == "plan" and isinstance(value, list):
            result[key] = [anonymizer.text(str(p)) if isinstance(p, str) else p for p in value]
        elif isinstance(value, str):
            result[key] = anonymizer.text(value)
        else:
            result[key] = value

    return result

def _normalize_timestamp(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat()
    return None


def _build_project_name(dir_name: str) -> str:
    """Convert a hyphen-encoded project dir name to a human-readable name.

    Examples: '-Users-alice-Documents-myapp' -> 'claude:myapp'
              '-home-bob-project' -> 'claude:project'
              'standalone' -> 'claude:standalone'
    """
    path = dir_name.replace("-", "/")
    path = path.lstrip("/")
    parts = path.split("/")
    common_dirs = {"Documents", "Downloads", "Desktop"}

    if len(parts) >= 2 and parts[0] == "Users":
        if len(parts) >= 4 and parts[2] in common_dirs:
            meaningful = parts[3:]
        elif len(parts) >= 3 and parts[2] not in common_dirs:
            meaningful = parts[2:]
        else:
            meaningful = []
    elif len(parts) >= 2 and parts[0] == "home":
        meaningful = parts[2:] if len(parts) > 2 else []
    else:
        meaningful = parts

    if meaningful:
        segments = dir_name.lstrip("-").split("-")
        prefix_parts = len(parts) - len(meaningful)
        name = "-".join(segments[prefix_parts:]) or dir_name
    else:
        if len(parts) >= 2 and parts[0] in ("Users", "home"):
            if len(parts) == 2:
                name = "~home"
            elif len(parts) == 3 and parts[2] in common_dirs:
                name = f"~{parts[2]}"
            else:
                name = dir_name.strip("-") or "unknown"
        else:
            name = dir_name.strip("-") or "unknown"
    return f"claude:{name}"


# ---------------------------------------------------------------------------
# Cursor CLI parser
# ---------------------------------------------------------------------------

def _get_cursor_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _CURSOR_PROJECT_INDEX
    if refresh or not _CURSOR_PROJECT_INDEX:
        _CURSOR_PROJECT_INDEX = _build_cursor_project_index()
    return _CURSOR_PROJECT_INDEX


def _build_cursor_project_index() -> dict[str, list[Path]]:
    """Scan ~/.cursor/ for JSONL session files and group by project directory."""
    index: dict[str, list[Path]] = {}
    projects_dir = CURSOR_DIR / "projects"
    if not projects_dir.exists():
        return index
    try:
        for project_dir in sorted(projects_dir.iterdir()):
            if not project_dir.is_dir():
                continue
            session_files = list(project_dir.glob("*.jsonl"))
            if session_files:
                index[project_dir.name] = sorted(session_files)
    except OSError:
        pass
    return index


def _discover_cursor_projects() -> list[dict]:
    index = _get_cursor_project_index(refresh=True)
    projects = []
    for dir_name, session_files in sorted(index.items()):
        if not session_files:
            continue
        projects.append({
            "dir_name": dir_name,
            "display_name": f"cursor:{_build_project_name(dir_name).removeprefix('claude:')}",
            "session_count": len(session_files),
            "total_size_bytes": sum(f.stat().st_size for f in session_files if f.exists()),
            "source": CURSOR_SOURCE,
        })
    return projects


def _parse_cursor_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Parse a Cursor CLI JSONL session file.

    Cursor CLI uses a similar format to Claude Code: JSONL with event entries
    containing type, message role/content, and model information.
    """
    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": filepath.stem,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        for entry in _iter_jsonl(filepath):
            entry_type = entry.get("type")
            timestamp = _normalize_timestamp(entry.get("timestamp"))

            if entry_type == "model_change":
                model = entry.get("modelId") or entry.get("model")
                if model and metadata["model"] is None:
                    metadata["model"] = model

            if entry_type in ("user", "human"):
                msg_data = entry.get("message", entry)
                content = msg_data.get("content")
                text = ""
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            text = block.get("text", "").strip()
                            break
                        elif isinstance(block, str):
                            text = block.strip()
                            break
                if text:
                    messages.append({
                        "role": "user",
                        "content": anonymizer.text(text),
                        "timestamp": timestamp,
                    })
                    stats["user_messages"] += 1
                    if metadata["start_time"] is None:
                        metadata["start_time"] = timestamp

            elif entry_type in ("assistant", "ai"):
                msg_data = entry.get("message", entry)
                msg: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
                content = msg_data.get("content")
                text_parts: list[str] = []
                thinking_parts: list[str] = []
                tool_uses_in_msg: list[dict[str, Any]] = []

                if isinstance(content, str) and content.strip():
                    text_parts.append(anonymizer.text(content.strip()))
                elif isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        btype = block.get("type")
                        if btype == "text":
                            t = block.get("text", "").strip()
                            if t:
                                text_parts.append(anonymizer.text(t))
                        elif btype == "thinking" and include_thinking:
                            t = block.get("thinking", block.get("text", "")).strip()
                            if t:
                                thinking_parts.append(anonymizer.text(t))
                        elif btype == "tool_use":
                            tool_name = block.get("name", "")
                            tool_input = block.get("input", {})
                            tool_uses_in_msg.append({
                                "tool": tool_name,
                                "input": _parse_tool_input(tool_name, tool_input, anonymizer),
                            })
                            stats["tool_uses"] += 1

                if text_parts:
                    msg["content"] = "\n\n".join(text_parts)
                if thinking_parts:
                    msg["thinking"] = "\n\n".join(thinking_parts)
                if tool_uses_in_msg:
                    msg["tool_uses"] = tool_uses_in_msg
                if text_parts or thinking_parts or tool_uses_in_msg:
                    messages.append(msg)
                    stats["assistant_messages"] += 1
                # Also emit separate tool entries for downstream compat
                for tu in tool_uses_in_msg:
                    messages.append({"role": "assistant", **tu})

                # Update model from message-level data
                model = msg_data.get("model")
                if model and metadata["model"] is None:
                    metadata["model"] = model

            # Track timestamps for duration
            if timestamp:
                metadata["end_time"] = timestamp

            # Token usage
            usage = entry.get("usage", {})
            if isinstance(usage, dict):
                stats["input_tokens"] += _safe_int(usage.get("input_tokens", 0))
                stats["output_tokens"] += _safe_int(usage.get("output_tokens", 0))

    except OSError:
        return None

    return _make_session_result(metadata, messages, stats)


# ---------------------------------------------------------------------------
# GitHub Copilot CLI parser
# ---------------------------------------------------------------------------

def _discover_copilot_projects() -> list[dict]:
    """Discover Copilot CLI sessions from ~/.copilot/session-state/."""
    if not COPILOT_DIR.exists():
        return []
    projects = []
    try:
        for session_dir in sorted(COPILOT_DIR.iterdir()):
            if not session_dir.is_dir():
                continue
            events_file = session_dir / "events.jsonl"
            if not events_file.exists():
                continue
            projects.append({
                "dir_name": session_dir.name,
                "display_name": f"copilot:{session_dir.name[:12]}",
                "session_count": 1,
                "total_size_bytes": events_file.stat().st_size,
                "source": COPILOT_SOURCE,
            })
    except OSError:
        pass
    return projects


def _build_copilot_project_name(session_dir_name: str) -> str:
    return f"copilot:{session_dir_name[:12]}"


def _parse_copilot_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> dict | None:
    """Parse a Copilot CLI events.jsonl file.

    Event types: sessionStart, userPromptSubmitted, preToolUse, postToolUse, SessionEnd.
    Each event has: type, data, id (UUID), timestamp (ISO-8601), parentId.
    """
    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": filepath.parent.name,
        "cwd": None,
        "git_branch": None,
        "model": None,
        "start_time": None,
        "end_time": None,
    }
    stats = _make_stats()

    try:
        for entry in _iter_jsonl(filepath):
            event_type = entry.get("type", "")
            data = entry.get("data", {})
            timestamp = _normalize_timestamp(entry.get("timestamp"))

            if not isinstance(data, dict):
                data = {}

            if event_type == "sessionStart":
                metadata["start_time"] = timestamp
                metadata["model"] = data.get("model")
                metadata["cwd"] = data.get("workingDirectory")

            elif event_type == "userPromptSubmitted":
                prompt = data.get("prompt", "")
                if isinstance(prompt, str) and prompt.strip():
                    messages.append({
                        "role": "user",
                        "content": anonymizer.text(prompt.strip()),
                        "timestamp": timestamp,
                    })
                    stats["user_messages"] += 1

            elif event_type == "assistantResponse":
                content = data.get("content", data.get("text", ""))
                if isinstance(content, str) and content.strip():
                    messages.append({
                        "role": "assistant",
                        "content": anonymizer.text(content.strip()),
                        "timestamp": timestamp,
                    })
                    stats["assistant_messages"] += 1
                # Model from response
                model = data.get("model")
                if model and metadata["model"] is None:
                    metadata["model"] = model

            elif event_type == "preToolUse":
                tool_name = data.get("toolName", data.get("name", "unknown"))
                tool_input = data.get("input", data.get("parameters", {}))
                if isinstance(tool_input, str):
                    try:
                        tool_input = json.loads(tool_input)
                    except json.JSONDecodeError:
                        tool_input = {"command": tool_input}
                messages.append({
                    "role": "assistant",
                    "tool": tool_name,
                    "input": _parse_tool_input(tool_name, tool_input, anonymizer) if isinstance(tool_input, dict) else {},
                })
                stats["tool_uses"] += 1

            elif event_type == "postToolUse":
                output = data.get("output", data.get("result", ""))
                if isinstance(output, str) and output.strip() and messages:
                    last = messages[-1]
                    if last.get("tool"):
                        last["output"] = anonymizer.text(output.strip()[:5000])
                        last["status"] = "error" if data.get("isError") else "success"

            elif event_type in ("SessionEnd", "sessionEnd"):
                metadata["end_time"] = timestamp
                usage = data.get("metrics", data.get("usage", {}))
                if isinstance(usage, dict):
                    stats["input_tokens"] += _safe_int(usage.get("input_tokens", usage.get("inputTokens", 0)))
                    stats["output_tokens"] += _safe_int(usage.get("output_tokens", usage.get("outputTokens", 0)))

            # Track end_time continuously
            if timestamp:
                metadata["end_time"] = timestamp

    except OSError:
        return None

    return _make_session_result(metadata, messages, stats)


# ---------------------------------------------------------------------------
# Aider parser
# ---------------------------------------------------------------------------

def _get_aider_project_index(refresh: bool = False) -> dict[str, Path]:
    global _AIDER_PROJECT_INDEX
    if refresh or not _AIDER_PROJECT_INDEX:
        _AIDER_PROJECT_INDEX = _build_aider_project_index()
    return _AIDER_PROJECT_INDEX


def _build_aider_project_index() -> dict[str, Path]:
    """Find .aider.chat.history.md files across known project directories.

    Scans home directory for common code directories (up to 2 levels deep)
    and checks for the Aider history file.
    """
    index: dict[str, Path] = {}
    home = Path.home()
    scan_dirs = [home]
    # Add common code directories
    for subdir in ("projects", "repos", "code", "src", "work", "dev", "llm", "git"):
        candidate = home / subdir
        if candidate.is_dir():
            scan_dirs.append(candidate)

    for parent in scan_dirs:
        try:
            for entry in parent.iterdir():
                if not entry.is_dir() or entry.name.startswith("."):
                    continue
                history = entry / AIDER_HISTORY_FILENAME
                if history.exists():
                    index[str(entry)] = history
                # One level deeper
                try:
                    for subentry in entry.iterdir():
                        if not subentry.is_dir() or subentry.name.startswith("."):
                            continue
                        history = subentry / AIDER_HISTORY_FILENAME
                        if history.exists():
                            index[str(subentry)] = history
                except OSError:
                    continue
        except OSError:
            continue
    return index


def _discover_aider_projects() -> list[dict]:
    index = _get_aider_project_index(refresh=True)
    projects = []
    for cwd, history_file in sorted(index.items()):
        try:
            size = history_file.stat().st_size
        except OSError:
            continue
        # Estimate session count from markdown headers
        try:
            content = history_file.read_text(encoding="utf-8", errors="replace")
            session_count = max(
                1,
                len(re.findall(r"^# aider chat started at .+$", content, flags=re.MULTILINE)),
            )
        except OSError:
            session_count = 1
        name = Path(cwd).name
        projects.append({
            "dir_name": cwd,
            "display_name": f"aider:{name}",
            "session_count": session_count,
            "total_size_bytes": size,
            "source": AIDER_SOURCE,
        })
    return projects


def _parse_aider_history_file(
    filepath: Path,
    anonymizer: Anonymizer,
    project_dir: str,
) -> list[dict]:
    """Parse .aider.chat.history.md into session dicts.

    Aider writes conversations in markdown with headers like:
    # aider chat started at 2026-04-10 14:32:00

    Each header starts a new session. Within a session, user messages
    are prefixed with '>' (blockquote) or '####', and assistant responses
    are plain text.
    """
    try:
        content = filepath.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    sessions: list[dict] = []
    # Split by session headers
    parts = re.split(r"^# aider chat started at (.+)$", content, flags=re.MULTILINE)
    # parts[0] is text before first header (usually empty)
    # parts[1] is timestamp, parts[2] is content, parts[3] is timestamp, etc.

    for i in range(1, len(parts) - 1, 2):
        timestamp_str = parts[i].strip()
        session_content = parts[i + 1] if i + 1 < len(parts) else ""

        try:
            start_time = datetime.strptime(timestamp_str, "%Y-%m-%d %H:%M:%S")
            start_time = start_time.replace(tzinfo=timezone.utc)
            start_iso = start_time.isoformat()
        except ValueError:
            start_iso = timestamp_str

        session_id = hashlib.sha256(
            f"aider:{project_dir}:{timestamp_str}".encode()
        ).hexdigest()[:16]

        messages: list[dict[str, Any]] = []
        stats = _make_stats()
        current_role = None
        current_text: list[str] = []

        def flush_message():
            nonlocal current_role, current_text
            if current_role and current_text:
                text = "\n".join(current_text).strip()
                if text:
                    messages.append({
                        "role": current_role,
                        "content": anonymizer.text(text),
                    })
                    if current_role == "user":
                        stats["user_messages"] += 1
                    else:
                        stats["assistant_messages"] += 1
            current_role = None
            current_text = []

        for line in session_content.split("\n"):
            stripped = line.strip()
            # User messages: blockquote or #### header
            if stripped == ">":
                # Empty blockquote continuation — blank line within user block
                if current_role == "user" and current_text:
                    current_text.append("")
            elif stripped.startswith("> ") or stripped.startswith("#### "):
                if current_role != "user":
                    flush_message()
                    current_role = "user"
                if stripped.startswith("#### "):
                    text = stripped.removeprefix("#### ").strip()
                else:
                    text = stripped.removeprefix("> ").strip()
                if text:
                    current_text.append(text)
            elif stripped and not stripped.startswith("#"):
                # Assistant response (plain text)
                if current_role != "assistant":
                    flush_message()
                    current_role = "assistant"
                current_text.append(stripped)
            elif not stripped:
                # Blank line — continue current block
                if current_text:
                    current_text.append("")

        flush_message()

        if messages:
            # Estimate tokens from content length (rough: 1 token ≈ 4 chars)
            total_chars = sum(len(m.get("content", "")) for m in messages)
            est_tokens = total_chars // 4
            stats["input_tokens"] = est_tokens // 3
            stats["output_tokens"] = est_tokens * 2 // 3

            metadata: dict[str, Any] = {
                "session_id": session_id,
                "cwd": project_dir,
                "git_branch": None,
                "model": None,  # Aider history doesn't reliably include model
                "start_time": start_iso,
                "end_time": None,  # Aider history only has start time
            }
            result = _make_session_result(metadata, messages, stats)
            if result:
                result["project"] = f"aider:{Path(project_dir).name}"
                result["source"] = AIDER_SOURCE
                sessions.append(result)

    return sessions
