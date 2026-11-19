"""Parse Claude Code, Claude Science, Codex, Gemini CLI, Antigravity CLI, OpenCode, OpenClaw, and Kiro session data into conversations."""

import base64
import dataclasses
import difflib
import hashlib
import io
import json
import logging
import os
import platform
import re
import sqlite3
import zipfile
from collections.abc import Iterable
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.parse import unquote, urlsplit

from ..config import CONFIG_DIR
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
WORKBUDDY_SOURCE = "workbuddy"
KIRO_SOURCE = "kiro"
ANTIGRAVITY_SOURCE = "antigravity"
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
WORKBUDDY_DIR = Path.home() / "WorkBuddy"
WORKBUDDY_AI_PROJECTS_DIR = Path.home() / ".workbuddy-ai" / "projects"
WORKBUDDY_IMPORT_DIR = CONFIG_DIR / "workbuddy"
WORKBUDDY_MAX_FILE_BYTES = 100 * 1024 * 1024
WORKBUDDY_MAX_ZIP_MEMBER_BYTES = 25 * 1024 * 1024
WORKBUDDY_TRACE_SUFFIXES = {".json", ".jsonl", ".ndjson", ".log"}
WORKBUDDY_ARCHIVE_SUFFIXES = {".zip"}

# Kiro (https://kiro.dev) keeps sessions in three places: the ~/.kiro/sessions
# tree shared by the current CLI and IDE, the Kiro CLI's SQLite database, and
# the Kiro IDE's VS Code-style extension storage.
KIRO_DIR = Path.home() / ".kiro"
KIRO_SESSIONS_DIR = KIRO_DIR / "sessions"
if platform.system() == "Darwin":
    KIRO_CLI_DIR = Path.home() / "Library" / "Application Support" / "kiro-cli"
    _KIRO_IDE_USER_DATA_DIR = Path.home() / "Library" / "Application Support" / "Kiro"
elif platform.system() == "Windows":
    _KIRO_APPDATA = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    KIRO_CLI_DIR = _KIRO_APPDATA / "kiro-cli"
    _KIRO_IDE_USER_DATA_DIR = _KIRO_APPDATA / "Kiro"
else:
    KIRO_CLI_DIR = Path.home() / ".local" / "share" / "kiro-cli"
    _KIRO_IDE_USER_DATA_DIR = Path.home() / ".config" / "Kiro"
KIRO_CLI_DB_PATH = KIRO_CLI_DIR / "data.sqlite3"
KIRO_IDE_DIR = _KIRO_IDE_USER_DATA_DIR / "User" / "globalStorage" / "kiro.kiroagent"
UNKNOWN_KIRO_CWD = "<unknown-cwd>"

# Antigravity CLI (`agy`, https://antigravity.google/product/antigravity-cli)
# keeps its data beside Gemini CLI's.
ANTIGRAVITY_DIR = Path.home() / ".gemini" / "antigravity-cli"
UNKNOWN_ANTIGRAVITY_CWD = "<unknown-cwd>"

CUSTOM_DIR = CONFIG_DIR / "custom"

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
_WORKBUDDY_PROJECT_INDEX: dict[str, list[Path]] = {}
_KIRO_PROJECT_INDEX: dict[str, list[dict[str, Any]]] = {}
_ANTIGRAVITY_PROJECT_INDEX: dict[str, list[dict[str, Any]]] = {}


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


def _iter_jsonl_bytes(data: bytes, *, filepath: Path):
    """Yield every nonblank JSONL object from one exact byte snapshot."""

    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"invalid UTF-8 JSONL input: {filepath.name}") from exc
    # Split on newlines exactly like the non-strict `for line in f` path
    # (universal newlines: \n, \r\n, \r). str.splitlines() must NOT be used
    # here: it also breaks on U+2028/U+2029/U+0085, which JSON.stringify (e.g.
    # Claude Code) emits unescaped inside string values, so a single valid
    # JSONL record would be split into fragments and fail strict parsing.
    for line_number, line in enumerate(io.StringIO(text, newline=None), start=1):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"malformed JSONL input at line {line_number}: {filepath.name}"
            ) from exc


def _iter_binary_jsonl_records(handle):
    """Yield records and delimiters without buffering the whole file."""

    offset = 0
    for raw_line in handle:
        line_start = 0
        for delimiter in re.finditer(br"\r\n|\r|\n", raw_line):
            yield (
                raw_line[line_start:delimiter.start()],
                raw_line[delimiter.start():delimiter.end()],
                offset + line_start,
                offset + delimiter.end(),
            )
            line_start = delimiter.end()
        if line_start < len(raw_line):
            yield (
                raw_line[line_start:],
                b"",
                offset + line_start,
                offset + len(raw_line),
            )
        offset += len(raw_line)


def _iter_jsonl(
    filepath: Path,
    *,
    strict: bool = False,
    snapshot_out: dict[str, Any] | None = None,
):
    """Yield JSONL objects with bounded memory and exact strict snapshots."""

    if snapshot_out is not None:
        snapshot_out.clear()
    with filepath.open("rb") as handle:
        before = os.fstat(handle.fileno())
        digest = hashlib.sha256()
        line_number = 0
        for (
            record_bytes,
            delimiter_bytes,
            record_start,
            record_end,
        ) in _iter_binary_jsonl_records(handle):
            digest.update(record_bytes)
            digest.update(delimiter_bytes)
            line_number += 1
            if snapshot_out is not None:
                snapshot_out["record_start"] = record_start
                snapshot_out["record_end"] = record_end
            try:
                line = record_bytes.decode(
                    "utf-8", errors="strict" if strict else "replace"
                ).strip()
            except UnicodeDecodeError as exc:
                raise ValueError(
                    f"invalid UTF-8 JSONL input at line {line_number}: {filepath.name}"
                ) from exc
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as exc:
                if strict:
                    raise ValueError(
                        f"malformed JSONL input at line {line_number}: {filepath.name}"
                    ) from exc

        after = os.fstat(handle.fileno())

    if strict and (
        before.st_dev != after.st_dev
        or before.st_ino != after.st_ino
        or before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
    ):
        from ..raw_sources import RawSourceChanged

        raise RawSourceChanged("raw source changed while it was parsed")
    if snapshot_out is not None:
        snapshot_out["file_size"] = int(after.st_size)
        snapshot_out["fingerprint"] = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
            digest.hexdigest(),
        )


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
        WORKBUDDY_SOURCE: _discover_workbuddy_projects,
        KIRO_SOURCE: _discover_kiro_projects,
        ANTIGRAVITY_SOURCE: _discover_antigravity_projects,
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


def _workbuddy_env_dir(name: str, fallback: Path) -> Path:
    value = os.environ.get(name)
    return Path(value) if value else fallback


def _iter_existing_workbuddy_roots() -> list[tuple[Path, str]]:
    """Return WorkBuddy locations we can scan without product internals.

    Official docs expose logs through the in-app Help menu, so the manual
    import directory is first. The other roots are known user/workspace/cache
    locations; nonexistent paths are ignored.
    """
    roots: list[tuple[Path, str]] = [
        (WORKBUDDY_IMPORT_DIR, "manual"),
        (WORKBUDDY_DIR, "workspace"),
        (WORKBUDDY_AI_PROJECTS_DIR, "project-tree"),
    ]
    if platform.system() == "Darwin":
        roots.extend(
            [
                (Path.home() / "Library" / "Application Support" / "WorkBuddy", "support"),
                (Path.home() / "Library" / "Caches" / "WorkBuddy", "logs"),
                (Path.home() / "Library" / "Caches" / "com.tencent.WorkBuddy", "logs"),
                (Path.home() / "Library" / "Logs" / "WorkBuddy", "logs"),
            ]
        )
    else:
        appdata = _workbuddy_env_dir(
            "APPDATA", Path.home() / "AppData" / "Roaming"
        )
        localappdata = _workbuddy_env_dir(
            "LOCALAPPDATA", Path.home() / "AppData" / "Local"
        )
        roots.extend(
            [
                (appdata / "Tencent" / "WorkBuddy", "support"),
                (appdata / "Tencent" / "WorkBuddy" / "Logs", "logs"),
                (appdata / "WorkBuddy", "support"),
                (localappdata / "Tencent" / "WorkBuddy", "support"),
                (localappdata / "Tencent" / "WorkBuddy" / "Logs", "logs"),
                (localappdata / "WorkBuddy", "support"),
            ]
        )

    seen: set[Path] = set()
    existing: list[tuple[Path, str]] = []
    for root, kind in roots:
        try:
            resolved = root.expanduser().resolve()
        except OSError:
            resolved = root.expanduser()
        if resolved in seen or not root.exists():
            continue
        seen.add(resolved)
        existing.append((root, kind))
    return existing


def _iter_workbuddy_candidate_files(root: Path, kind: str) -> list[Path]:
    suffixes = WORKBUDDY_TRACE_SUFFIXES | WORKBUDDY_ARCHIVE_SUFFIXES
    candidates: list[Path] = []
    try:
        if kind == "workspace":
            workspace_dirs = [d for d in root.iterdir() if d.is_dir()]
            for workspace in sorted(workspace_dirs):
                wb_dir = workspace / ".workbuddy"
                scan_roots = [wb_dir] if wb_dir.is_dir() else []
                for scan_root in scan_roots:
                    candidates.extend(
                        p for p in scan_root.rglob("*")
                        if p.is_file() and p.suffix.lower() in suffixes
                    )
            candidates.extend(
                p for p in root.glob("*.zip") if p.is_file()
            )
        elif kind == "project-tree":
            candidates.extend(
                p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in suffixes
            )
        else:
            candidates.extend(
                p for p in root.rglob("*")
                if p.is_file() and p.suffix.lower() in suffixes
            )
    except OSError:
        return []
    return [p for p in sorted(candidates) if _workbuddy_file_size_ok(p)]


def _workbuddy_file_size_ok(path: Path) -> bool:
    try:
        return path.stat().st_size <= WORKBUDDY_MAX_FILE_BYTES
    except OSError:
        return False


def _workbuddy_project_key(path: Path, root: Path, kind: str) -> str:
    try:
        rel = path.relative_to(root)
    except ValueError:
        rel = Path(path.name)
    if kind == "manual":
        return rel.parts[0] if len(rel.parts) > 1 else "manual"
    if kind == "project-tree":
        return rel.parts[0] if len(rel.parts) > 1 else "workbuddy-ai"
    if kind == "workspace":
        return rel.parts[0] if rel.parts else "workspace"
    return kind


def _get_workbuddy_project_index(refresh: bool = False) -> dict[str, list[Path]]:
    global _WORKBUDDY_PROJECT_INDEX
    if refresh or not _WORKBUDDY_PROJECT_INDEX:
        index: dict[str, list[Path]] = {}
        for root, kind in _iter_existing_workbuddy_roots():
            for path in _iter_workbuddy_candidate_files(root, kind):
                key = _workbuddy_project_key(path, root, kind)
                index.setdefault(key, []).append(path)
        _WORKBUDDY_PROJECT_INDEX = {
            key: sorted(paths) for key, paths in sorted(index.items())
        }
    return _WORKBUDDY_PROJECT_INDEX


def _discover_workbuddy_projects() -> list[dict]:
    index = _get_workbuddy_project_index(refresh=True)
    projects: list[dict[str, Any]] = []
    anonymizer = Anonymizer(enabled=False)
    for key, files in sorted(index.items()):
        sessions = []
        total_size = 0
        for path in files:
            try:
                total_size += path.stat().st_size
            except OSError:
                pass
            sessions.extend(_parse_workbuddy_trace_file(path, anonymizer))
        if not sessions:
            continue
        projects.append(
            {
                "dir_name": key,
                "display_name": _build_workbuddy_project_name(key),
                "session_count": len(sessions),
                "total_size_bytes": total_size,
                "source": WORKBUDDY_SOURCE,
            }
        )
    return projects


def _parse_workbuddy_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> list[dict]:
    index = _get_workbuddy_project_index()
    sessions: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for path in index.get(project_dir_name, []):
        for session in _parse_workbuddy_trace_file(path, anonymizer, include_thinking):
            if not session.get("messages"):
                continue
            session["project"] = _build_workbuddy_project_name(project_dir_name)
            session["source"] = WORKBUDDY_SOURCE
            sid = str(session.get("session_id") or "")
            if sid in seen_ids:
                session["session_id"] = hashlib.sha256(
                    f"{sid}:{session.get('raw_source_path', path)}".encode()
                ).hexdigest()[:16]
            seen_ids.add(str(session["session_id"]))
            sessions.append(session)
    return sessions


def _parse_workbuddy_trace_file(
    path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
) -> list[dict]:
    suffix = path.suffix.lower()
    if suffix in WORKBUDDY_ARCHIVE_SUFFIXES:
        return _parse_workbuddy_zip(path, anonymizer, include_thinking)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return _parse_workbuddy_text(
        text,
        anonymizer=anonymizer,
        include_thinking=include_thinking,
        raw_source_path=str(path),
    )


def _parse_workbuddy_zip(
    path: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> list[dict]:
    sessions: list[dict[str, Any]] = []
    try:
        with zipfile.ZipFile(path) as archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if info.is_dir() or info.file_size > WORKBUDDY_MAX_ZIP_MEMBER_BYTES:
                    continue
                suffix = Path(info.filename).suffix.lower()
                if suffix not in WORKBUDDY_TRACE_SUFFIXES:
                    continue
                try:
                    data = archive.read(info)
                except (KeyError, RuntimeError, zipfile.BadZipFile):
                    continue
                text = data.decode("utf-8", errors="replace")
                sessions.extend(
                    _parse_workbuddy_text(
                        text,
                        anonymizer=anonymizer,
                        include_thinking=include_thinking,
                        raw_source_path=f"{path}#{info.filename}",
                    )
                )
    except (OSError, zipfile.BadZipFile):
        return []
    return sessions


def _parse_workbuddy_text(
    text: str,
    *,
    anonymizer: Anonymizer,
    include_thinking: bool,
    raw_source_path: str,
) -> list[dict]:
    values = _load_workbuddy_json_values(text)
    if not values:
        return []

    sessions: list[dict[str, Any]] = []
    message_groups: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        extracted = _extract_workbuddy_sessions(
            value,
            anonymizer=anonymizer,
            include_thinking=include_thinking,
            raw_source_path=raw_source_path,
        )
        if extracted:
            sessions.extend(extracted)
            continue
        if isinstance(value, dict) and _is_workbuddy_message_like(value):
            group_key = _workbuddy_group_id(value, raw_source_path)
            message_groups.setdefault(group_key, []).append(value)

    for group_id, rows in message_groups.items():
        parsed = _build_workbuddy_session_from_messages(
            rows,
            anonymizer=anonymizer,
            include_thinking=include_thinking,
            raw_source_path=raw_source_path,
            fallback_session_id=group_id,
            metadata={},
        )
        if parsed:
            sessions.append(parsed)

    return _dedupe_workbuddy_sessions(sessions, raw_source_path)


def _load_workbuddy_json_values(text: str) -> list[Any]:
    stripped = text.strip()
    if not stripped:
        return []
    try:
        return [json.loads(stripped)]
    except json.JSONDecodeError:
        pass

    values: list[Any] = []
    for line in text.splitlines():
        row = line.strip()
        if not row:
            continue
        parsed = _parse_workbuddy_json_fragment(row)
        if parsed is not None:
            values.append(parsed)
    return values


def _parse_workbuddy_json_fragment(row: str) -> Any | None:
    candidates = [row]
    if not row.startswith(("{", "[")):
        start = min(
            (idx for idx in (row.find("{"), row.find("[")) if idx >= 0),
            default=-1,
        )
        if start >= 0:
            candidates.append(row[start:])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return None


_WORKBUDDY_MESSAGE_KEYS = (
    "messages", "messageList", "chatMessages", "conversation", "history",
    "records", "dialogue", "turns",
)
_WORKBUDDY_SESSION_LIST_KEYS = (
    "sessions", "conversations", "tasks", "items", "data", "list",
)


def _extract_workbuddy_sessions(
    value: Any,
    *,
    anonymizer: Anonymizer,
    include_thinking: bool,
    raw_source_path: str,
) -> list[dict]:
    if isinstance(value, list):
        if value and all(isinstance(item, dict) and _is_workbuddy_message_like(item) for item in value):
            parsed = _build_workbuddy_session_from_messages(
                value,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                raw_source_path=raw_source_path,
                fallback_session_id=_workbuddy_hash_id(raw_source_path, "list"),
                metadata={},
            )
            return [parsed] if parsed else []
        sessions: list[dict[str, Any]] = []
        for item in value:
            sessions.extend(
                _extract_workbuddy_sessions(
                    item,
                    anonymizer=anonymizer,
                    include_thinking=include_thinking,
                    raw_source_path=raw_source_path,
                )
            )
        return sessions

    if not isinstance(value, dict):
        return []

    for key in _WORKBUDDY_MESSAGE_KEYS:
        raw_messages = value.get(key)
        if isinstance(raw_messages, list) and raw_messages:
            parsed = _build_workbuddy_session_from_messages(
                raw_messages,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                raw_source_path=raw_source_path,
                fallback_session_id=_workbuddy_session_id(value, raw_source_path),
                metadata=value,
            )
            return [parsed] if parsed else []

    for key in _WORKBUDDY_SESSION_LIST_KEYS:
        nested = value.get(key)
        if isinstance(nested, list) and nested:
            sessions: list[dict[str, Any]] = []
            for item in nested:
                sessions.extend(
                    _extract_workbuddy_sessions(
                        item,
                        anonymizer=anonymizer,
                        include_thinking=include_thinking,
                        raw_source_path=raw_source_path,
                    )
                )
            if sessions:
                return sessions

    return []


def _build_workbuddy_session_from_messages(
    raw_messages: list[Any],
    *,
    anonymizer: Anonymizer,
    include_thinking: bool,
    raw_source_path: str,
    fallback_session_id: str,
    metadata: dict[str, Any],
) -> dict | None:
    messages: list[dict[str, Any]] = []
    stats = _make_stats()
    session_meta = {
        "session_id": _workbuddy_session_id(metadata, raw_source_path, fallback_session_id),
        "cwd": _first_str(metadata, ("cwd", "workspace", "workspacePath", "workdir")),
        "git_branch": _first_str(metadata, ("git_branch", "gitBranch", "branch")),
        "model": _first_str(metadata, ("model", "modelName", "model_id", "modelId")) or "workbuddy-unknown",
        "model_effort": _extract_model_effort(metadata),
        "start_time": _normalize_timestamp(
            _first_present(metadata, ("start_time", "startTime", "createdAt", "created_at", "timeCreated"))
        ),
        "end_time": _normalize_timestamp(
            _first_present(metadata, ("end_time", "endTime", "updatedAt", "finishedAt", "lastActivityAt"))
        ),
    }

    for raw in raw_messages:
        if not isinstance(raw, dict):
            continue
        msg = _workbuddy_message(raw, anonymizer, include_thinking)
        if not msg:
            continue
        timestamp = msg.get("timestamp")
        if session_meta["start_time"] is None and timestamp:
            session_meta["start_time"] = timestamp
        if timestamp:
            session_meta["end_time"] = timestamp
        model = _first_str(raw, ("model", "modelName", "model_id", "modelId"))
        if model and session_meta["model"] == "workbuddy-unknown":
            session_meta["model"] = model
        effort = _extract_model_effort(raw)
        if effort and session_meta["model_effort"] is None:
            session_meta["model_effort"] = effort
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
        stats["input_tokens"] += _safe_int(
            _first_present(usage, ("input_tokens", "inputTokens", "prompt_tokens", "promptTokens"))
        )
        stats["output_tokens"] += _safe_int(
            _first_present(usage, ("output_tokens", "outputTokens", "completion_tokens", "completionTokens"))
        )
        messages.append(msg)
        if msg.get("role") == "user":
            stats["user_messages"] += 1
        elif msg.get("role") == "assistant":
            stats["assistant_messages"] += 1
        stats["tool_uses"] += len(msg.get("tool_uses", []))

    result = _make_session_result(session_meta, messages, stats)
    if result:
        result["raw_source_path"] = raw_source_path
    return result


def _workbuddy_message(
    raw: dict[str, Any],
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> dict[str, Any] | None:
    role = _workbuddy_role(raw)
    text = _workbuddy_text(raw)
    tool_uses = _workbuddy_tool_uses(raw, anonymizer)
    if role is None and tool_uses:
        role = "assistant"
    if role is None:
        return None

    msg: dict[str, Any] = {"role": role}
    timestamp = _normalize_timestamp(
        _first_present(raw, ("timestamp", "createdAt", "created_at", "time", "timeCreated"))
    )
    if timestamp:
        msg["timestamp"] = timestamp
    if text:
        redacted, _, _ = redact_text(text)
        msg["content"] = anonymizer.text(redacted)
    thinking = _first_str(raw, ("thinking", "reasoning", "thought", "plan"))
    if include_thinking and thinking:
        redacted, _, _ = redact_text(thinking)
        msg["thinking"] = anonymizer.text(redacted)
    if tool_uses:
        msg["tool_uses"] = tool_uses
    if not msg.get("content") and not msg.get("thinking") and not tool_uses:
        return None
    return msg


def _workbuddy_tool_uses(raw: dict[str, Any], anonymizer: Anonymizer) -> list[dict[str, Any]]:
    entries = []
    for key in ("tool_uses", "toolUses", "tool_calls", "toolCalls", "tools"):
        value = raw.get(key)
        if isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
    if not entries and (
        any(k in raw for k in ("tool", "toolName", "functionName"))
        or ("name" in raw and any(k in raw for k in ("input", "arguments", "args", "parameters")))
    ):
        entries.append(raw)

    tool_uses: list[dict[str, Any]] = []
    for item in entries:
        name = _first_str(item, ("tool", "toolName", "name", "functionName")) or "tool"
        input_data = (
            item.get("input")
            if "input" in item
            else item.get("arguments", item.get("args", item.get("parameters", {})))
        )
        tool_uses.append(
            {
                "tool": name,
                "input": _parse_tool_input(name, input_data, anonymizer),
            }
        )
    return tool_uses


def _is_workbuddy_message_like(value: dict[str, Any]) -> bool:
    return _workbuddy_role(value) is not None or any(
        key in value for key in ("content", "text", "message", "prompt", "answer", "response")
    )


def _workbuddy_role(value: dict[str, Any]) -> str | None:
    raw = _first_str(value, ("role", "author", "sender", "type", "event"))
    if raw is None:
        return None
    normalized = raw.strip().lower()
    if normalized in {"user", "human", "customer", "client", "request", "user_message"}:
        return "user"
    if normalized in {
        "assistant", "ai", "agent", "bot", "workbuddy", "answer",
        "assistant_message", "model",
    }:
        return "assistant"
    if "user" in normalized:
        return "user"
    if any(token in normalized for token in ("assistant", "agent", "bot", "reply", "answer")):
        return "assistant"
    return None


def _workbuddy_text(value: dict[str, Any]) -> str | None:
    for key in ("content", "text", "message", "prompt", "answer", "response", "output", "result"):
        if key not in value:
            continue
        text = _stringify_workbuddy_content(value.get(key))
        if text:
            return text
    return None


def _stringify_workbuddy_content(value: Any) -> str | None:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    if isinstance(value, dict):
        for key in ("text", "content", "markdown", "value", "body"):
            text = _stringify_workbuddy_content(value.get(key))
            if text:
                return text
        return None
    if isinstance(value, list):
        parts = [
            text for item in value
            if (text := _stringify_workbuddy_content(item))
        ]
        return "\n\n".join(parts).strip() or None
    return None


def _workbuddy_group_id(value: dict[str, Any], raw_source_path: str) -> str:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "task_id", "taskId", "threadId"):
        val = value.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()
    return _workbuddy_hash_id(raw_source_path, "events")


def _workbuddy_session_id(
    metadata: dict[str, Any],
    raw_source_path: str,
    fallback: str | None = None,
) -> str:
    for key in ("session_id", "sessionId", "conversation_id", "conversationId", "task_id", "taskId", "id", "uuid"):
        val = metadata.get(key)
        if isinstance(val, (str, int)) and str(val).strip():
            return str(val).strip()
    return fallback or _workbuddy_hash_id(raw_source_path, "session")


def _workbuddy_hash_id(raw_source_path: str, salt: str) -> str:
    return hashlib.sha256(f"workbuddy:{raw_source_path}:{salt}".encode()).hexdigest()[:16]


def _dedupe_workbuddy_sessions(sessions: list[dict], raw_source_path: str) -> list[dict]:
    seen: set[str] = set()
    deduped: list[dict] = []
    for index, session in enumerate(sessions):
        sid = str(session.get("session_id") or "")
        if sid in seen:
            sid = _workbuddy_hash_id(raw_source_path, f"{sid}:{index}")
            session["session_id"] = sid
        seen.add(sid)
        deduped.append(session)
    return deduped


def _first_present(data: Any, keys: tuple[str, ...]) -> Any:
    if not isinstance(data, dict):
        return None
    for key in keys:
        if key in data and data[key] not in (None, ""):
            return data[key]
    return None


def _first_str(data: Any, keys: tuple[str, ...]) -> str | None:
    value = _first_present(data, keys)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, int):
        return str(value)
    return None


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


def _finalize_append_only_segments(
    session: dict[str, Any],
    raw_path: Path,
    *,
    strict_jsonl: bool,
) -> list[dict[str, Any]]:
    from .segmenter import segment_append_only_session

    logical_session_id = str(session.get("session_id") or "")
    existing_logical_id = session.get("logical_session_id")
    if logical_session_id and (
        not isinstance(existing_logical_id, str) or not existing_logical_id
    ):
        session["logical_session_id"] = logical_session_id

    full_fingerprint = session.get("_raw_source_fingerprint")
    offsets = session.pop("_raw_message_end_offsets", None)
    start_offsets = session.pop("_raw_message_start_offsets", None)
    raw_source_size = session.pop("_raw_source_size", None)
    token_snapshots = session.pop("_raw_message_token_snapshots", None)
    # Multi-fragment Codex rollouts: per-message raw file, per-file sizes and
    # whole-file fingerprints (absent for single-file sessions).
    message_raw_paths = session.pop("_raw_message_paths", None)
    sizes_by_path = session.pop("_raw_source_sizes_by_path", None)
    fingerprints_by_path = session.pop("_raw_source_fingerprints_by_path", None)
    if not isinstance(offsets, list):
        return [session]
    segments = segment_append_only_session(
        session,
        offsets,
        message_start_offsets=start_offsets,
        raw_source_size=raw_source_size,
        message_token_snapshots=token_snapshots,
        message_raw_paths=message_raw_paths,
        raw_source_sizes=sizes_by_path if isinstance(sizes_by_path, dict) else None,
    )
    if len(segments) <= 1:
        return [session]
    if strict_jsonl:
        from ..raw_sources import fingerprint_raw_source_range

        for segment in segments:
            segment_path = Path(segment.get("raw_source_path") or raw_path)
            if segment.get("segment_sealed"):
                segment["_raw_source_fingerprint"] = fingerprint_raw_source_range(
                    segment_path,
                    int(segment["raw_source_start_offset"]),
                    int(segment["raw_source_end_offset"]),
                )
            else:
                fragment_fingerprint = (
                    fingerprints_by_path.get(str(segment_path))
                    if isinstance(fingerprints_by_path, dict)
                    else None
                )
                if (
                    isinstance(fragment_fingerprint, tuple)
                    and len(fragment_fingerprint) == 5
                ):
                    segment["_raw_source_fingerprint"] = fragment_fingerprint
                elif isinstance(full_fingerprint, tuple) and len(full_fingerprint) == 5:
                    segment["_raw_source_fingerprint"] = full_fingerprint
    return segments


def parse_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    source: str = CLAUDE_SOURCE,
    locator: dict | None = None,
    strict_jsonl: bool = False,
) -> list[dict]:
    """Parse all sessions for a project into structured dicts."""
    if source == CLAUDE_SCIENCE_SOURCE:
        return _parse_claude_science_project_sessions(
            project_dir_name, anonymizer, include_thinking,
        )

    if source == CUSTOM_SOURCE:
        return _parse_custom_sessions(project_dir_name, anonymizer)

    if source == WORKBUDDY_SOURCE:
        return _parse_workbuddy_project_sessions(
            project_dir_name, anonymizer, include_thinking,
        )

    if source == KIRO_SOURCE:
        return _parse_kiro_project_sessions(
            project_dir_name, anonymizer, include_thinking,
        )

    if source == ANTIGRAVITY_SOURCE:
        return _parse_antigravity_project_sessions(
            project_dir_name, anonymizer, include_thinking,
        )

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
        for fragment_group in _group_codex_rollout_fragments(session_files):
            parsed = _parse_codex_session_fragments(
                fragment_group,
                anonymizer=anonymizer,
                include_thinking=include_thinking,
                target_cwd=project_dir_name,
                strict_jsonl=strict_jsonl,
                capture_raw_offsets=True,
            )
            if parsed and parsed["messages"]:
                parsed["project"] = _build_codex_project_name(project_dir_name)
                parsed["source"] = CODEX_SOURCE
                # The newest fragment holds the active tail; sealed
                # checkpoints override this per segment in finalize.
                parsed["raw_source_path"] = str(fragment_group[-1])
                # Derive client_origin from originator field
                originator = parsed.pop("originator", None) or ""
                parsed.pop("codex_source", None)
                if "Desktop" in originator:
                    parsed["client_origin"] = "desktop"
                elif originator:
                    parsed["client_origin"] = "cli"
                sessions.extend(
                    _finalize_append_only_segments(
                        parsed, fragment_group[-1], strict_jsonl=strict_jsonl
                    )
                )
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
            parsed = _parse_claude_session_file(
                session_file,
                anonymizer,
                include_thinking,
                strict_jsonl=strict_jsonl,
                capture_raw_offsets=True,
            )
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
                sessions.extend(
                    _finalize_append_only_segments(
                        parsed, session_file, strict_jsonl=strict_jsonl
                    )
                )
                seen_session_ids.add(parsed["session_id"])
                seen_session_ids.add(session_file.stem)

        for session_dir in _find_subagent_only_sessions(native_dir):
            parsed = _parse_subagent_session(
                session_dir,
                anonymizer,
                include_thinking,
                strict_jsonl=strict_jsonl,
            )
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

            parsed = _parse_claude_session_file(
                jsonl_path,
                anonymizer,
                include_thinking,
                strict_jsonl=strict_jsonl,
                capture_raw_offsets=True,
            )
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
                sessions.extend(
                    _finalize_append_only_segments(
                        parsed, jsonl_path, strict_jsonl=strict_jsonl
                    )
                )
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
    for key in (
        "entrypoint", "originator", "codex_source", "model_effort",
        "segment_title", "fork_of", "fork_source", "fork_nickname",
    ):
        val = metadata.get(key)
        if val is not None:
            result[key] = val
    return result


def _build_tool_result_map(
    entries: Iterable[dict[str, Any]], anonymizer: Anonymizer
) -> dict[str, dict]:
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
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    *,
    strict_jsonl: bool = False,
    capture_raw_offsets: bool = False,
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

    tool_snapshot: dict[str, Any] = {}
    try:
        tool_result_map = _build_tool_result_map(
            _iter_jsonl(
                filepath,
                strict=strict_jsonl,
                snapshot_out=tool_snapshot,
            ),
            anonymizer,
        )
    except OSError:
        if strict_jsonl:
            raise
        return None

    parse_snapshot: dict[str, Any] = {}
    raw_message_start_offsets: list[int] = []
    raw_message_end_offsets: list[int] = []
    raw_message_token_snapshots: list[tuple[int, int, int, int]] = []
    try:
        for entry in _iter_jsonl(
            filepath,
            strict=strict_jsonl,
            snapshot_out=parse_snapshot,
        ):
            before_messages = len(messages)
            _process_entry(
                entry,
                messages,
                metadata,
                stats,
                anonymizer,
                include_thinking,
                tool_result_map,
            )
            if capture_raw_offsets and len(messages) > before_messages:
                added_messages = len(messages) - before_messages
                raw_message_start_offsets.extend(
                    [int(parse_snapshot["record_start"])] * added_messages
                )
                raw_message_end_offsets.extend(
                    [int(parse_snapshot["record_end"])] * added_messages
                )
                # Cumulative totals as of each message. Claude bills usage on
                # the same entry that carries the assistant message, so an
                # entry that adds no message never moves these counters.
                raw_message_token_snapshots.extend([(
                    stats["input_tokens"],
                    stats["output_tokens"],
                    stats["cache_read_tokens"],
                    stats["cache_creation_tokens"],
                )] * added_messages)
    except OSError:
        if strict_jsonl:
            raise
        return None

    if strict_jsonl and tool_snapshot.get("fingerprint") != parse_snapshot.get("fingerprint"):
        from ..raw_sources import RawSourceChanged

        raise RawSourceChanged("raw source changed between parser passes")

    result = _make_session_result(metadata, messages, stats)
    if result is not None and strict_jsonl:
        result["_raw_source_fingerprint"] = parse_snapshot.get("fingerprint")
    if result is not None and capture_raw_offsets:
        result["_raw_message_start_offsets"] = raw_message_start_offsets
        result["_raw_message_end_offsets"] = raw_message_end_offsets
        result["_raw_message_token_snapshots"] = raw_message_token_snapshots
        result["_raw_source_size"] = parse_snapshot.get("file_size")
    return result


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
    session_dir: Path,
    anonymizer: Anonymizer,
    include_thinking: bool = True,
    *,
    strict_jsonl: bool = False,
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
    snapshot_fingerprint = None
    if strict_jsonl:
        from ..raw_sources import read_raw_source_snapshot

        source_snapshots, snapshot_fingerprint = read_raw_source_snapshot(session_dir)
    else:
        source_snapshots = [
            (sa_file, None)
            for sa_file in sorted(subagent_dir.glob("agent-*.jsonl"))
        ]
    for sa_file, snapshot_bytes in source_snapshots:
        entries = (
            _iter_jsonl_bytes(snapshot_bytes, filepath=sa_file)
            if snapshot_bytes is not None
            else _iter_jsonl(sa_file)
        )
        for entry in entries:
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

    result = _make_session_result(metadata, messages, stats)
    if result is not None and strict_jsonl:
        result["_raw_source_fingerprint"] = snapshot_fingerprint
    return result


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
    retry_candidate_index: int | None = None
    retry_candidate_replaced: bool = False
    last_user_input_hash: str | None = None
    retry_candidate_input_hash: str | None = None
    # Which record format produced messages[-1] ("event_msg" or
    # "response_item"). Rollouts that carry both formats duplicate each
    # message once per format; the duplicate arrives from the *other*
    # format, so a same-format repeat is a genuine resend and is kept.
    last_message_origin: str | None = None
    # Every rollout entry is timestamped, not only messages. Kept so a
    # session whose messages never parsed still leaves with a clock.
    first_entry_timestamp: str | None = None
    last_entry_timestamp: str | None = None


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


def _build_codex_tool_result_map(
    entries: Iterable[dict[str, Any]], anonymizer: Anonymizer
) -> dict[str, dict]:
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


def _codex_token_snapshot(state: _CodexParseState) -> tuple[int, int, int, int]:
    """Cumulative (input, output, cache_read, cache_creation) totals so far.

    Codex rollouts report no cache-creation counter, so that slot stays zero.
    """
    return (
        state.max_input_tokens,
        state.max_output_tokens,
        state.max_cached_tokens,
        0,
    )


def _parse_codex_session_file(
    filepath: Path,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
    strict_jsonl: bool = False,
    capture_raw_offsets: bool = False,
) -> dict | None:
    return _parse_codex_session_fragments(
        [filepath],
        anonymizer=anonymizer,
        include_thinking=include_thinking,
        target_cwd=target_cwd,
        strict_jsonl=strict_jsonl,
        capture_raw_offsets=capture_raw_offsets,
    )


def _parse_codex_session_fragments(
    filepaths: list[Path],
    *,
    anonymizer: Anonymizer,
    include_thinking: bool,
    target_cwd: str,
    strict_jsonl: bool = False,
    capture_raw_offsets: bool = False,
) -> dict | None:
    """Parse one Codex conversation stored across ordered rollout fragments.

    Codex Desktop's cancel+edit flow continues the same conversation in a
    new rollout file that repeats the original ``session_meta.payload.id``
    (issue #214). All fragments flow through one ``_CodexParseState`` so the
    retry-candidate and cross-format-duplicate logic survive the file
    boundary; per-message raw paths keep byte ranges bound to the file they
    actually came from.
    """
    state = _CodexParseState(
        metadata={
            "session_id": filepaths[0].stem,
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

    # Tool outputs may land in a later fragment than their call (a turn cut
    # by the continuation); merge every fragment's call_id map up front.
    tool_snapshots: dict[str, dict[str, Any]] = {}
    for filepath in filepaths:
        tool_snapshot: dict[str, Any] = {}
        try:
            state.tool_result_map.update(
                _build_codex_tool_result_map(
                    _iter_jsonl(
                        filepath,
                        strict=strict_jsonl,
                        snapshot_out=tool_snapshot,
                    ),
                    anonymizer,
                )
            )
        except OSError:
            if strict_jsonl:
                raise
            return None
        tool_snapshots[str(filepath)] = tool_snapshot

    parse_snapshot: dict[str, Any] = {}
    raw_message_start_offsets: list[int] = []
    raw_message_end_offsets: list[int] = []
    raw_message_token_snapshots: list[tuple[int, int, int, int]] = []
    raw_message_paths: list[str] = []
    fragment_sizes: dict[str, int] = {}
    fragment_fingerprints: dict[str, Any] = {}

    def _iter_fragment_entries() -> Iterable[tuple[Path, dict[str, Any]]]:
        """Yield (fragment_path, entry) across fragments, in order.

        Runs each fragment's end-of-file bookkeeping (two-pass fingerprint
        check, size/fingerprint capture) before moving on, and leaves
        ``parse_snapshot`` pointing at the fragment currently being read.
        """
        nonlocal parse_snapshot
        for fragment_path in filepaths:
            fragment_snapshot: dict[str, Any] = {}
            parse_snapshot = fragment_snapshot
            for entry in _iter_jsonl(
                fragment_path,
                strict=strict_jsonl,
                snapshot_out=fragment_snapshot,
            ):
                yield fragment_path, entry
            fragment_key = str(fragment_path)
            if strict_jsonl and (
                tool_snapshots.get(fragment_key, {}).get("fingerprint")
                != fragment_snapshot.get("fingerprint")
            ):
                from ..raw_sources import RawSourceChanged

                raise RawSourceChanged("raw source changed between parser passes")
            size = fragment_snapshot.get("file_size")
            if isinstance(size, int):
                fragment_sizes[fragment_key] = size
            fragment_fingerprints[fragment_key] = fragment_snapshot.get("fingerprint")

    current_fragment: str | None = None
    try:
        for filepath, entry in _iter_fragment_entries():
            path_str = str(filepath)
            if path_str != current_fragment:
                if (
                    current_fragment is not None
                    and state.retry_candidate_index is not None
                ):
                    # Codex Desktop creates a same-id continuation rollout
                    # specifically for cancel+edit, so the new fragment is
                    # itself the edit-and-resend signal: its first real user
                    # message replaces the output-free canceled prompt that
                    # ended the previous fragment, even with edited text
                    # (mirrors the in-file `thread_rolled_back` rule).
                    state.retry_candidate_replaced = True
                current_fragment = path_str
            before_messages = len(state.messages)
            replaced_retry_user = False
            user_message_record = False
            timestamp = _normalize_timestamp(entry.get("timestamp"))
            if timestamp is not None:
                if state.first_entry_timestamp is None:
                    state.first_entry_timestamp = timestamp
                state.last_entry_timestamp = timestamp
            entry_type = entry.get("type")

            if entry_type == "session_meta":
                _handle_codex_session_meta(state, entry, filepath, anonymizer)
            elif entry_type == "turn_context":
                _handle_codex_turn_context(state, entry, anonymizer)
            elif entry_type == "response_item":
                response_payload = entry.get("payload", {})
                response_type = response_payload.get("type")
                if response_type in ("function_call", "custom_tool_call") or (
                    response_type == "reasoning" and include_thinking
                ):
                    _clear_codex_retry_candidate(state)
                user_message_record = (
                    response_type == "message"
                    and response_payload.get("role") == "user"
                )
                replaced_retry_user = _handle_codex_response_item(
                    state, entry, anonymizer, include_thinking, timestamp
                )
            elif entry_type == "event_msg":
                payload = entry.get("payload", {})
                event_type = payload.get("type")
                if event_type == "token_count":
                    _handle_codex_token_count(state, payload)
                elif event_type == "agent_reasoning" and include_thinking:
                    _clear_codex_retry_candidate(state)
                    thinking = payload.get("text")
                    if isinstance(thinking, str) and thinking.strip():
                        cleaned = anonymizer.text(thinking.strip())
                        if cleaned not in state._pending_thinking_seen:
                            state._pending_thinking_seen.add(cleaned)
                            state.pending_thinking.append(cleaned)
                elif event_type == "user_message":
                    user_message_record = True
                    message_parts = _codex_user_message_parts(payload, anonymizer)
                    if message_parts is not None:
                        replaced_retry_user = _ingest_codex_user_message(
                            state, message_parts, timestamp, "event_msg"
                        )
                    else:
                        _flush_codex_pending(state, timestamp)
                elif event_type == "agent_message":
                    _clear_codex_retry_candidate(state)
                    _handle_codex_agent_message(
                        state, payload.get("message"), timestamp, anonymizer,
                        include_thinking, "event_msg",
                    )
                elif event_type in ("turn_aborted", "task_complete"):
                    # Do not delete the prompt at the terminal event: the log
                    # may end here, or the next prompt may be an unrelated new
                    # turn.  A confirmed retry replaces this slot later so its
                    # original raw start remains an append-only boundary.
                    _mark_codex_retry_candidate(state, event_type, payload)
                elif event_type == "thread_rolled_back":
                    # A rollback is an explicit edit-and-resend signal.  The
                    # next user message replaces the output-free candidate even
                    # when its text was edited.
                    if (
                        state.retry_candidate_index is not None
                        and _safe_int(payload.get("num_turns")) == 1
                    ):
                        state.retry_candidate_replaced = True
            if (
                capture_raw_offsets
                and replaced_retry_user
                and raw_message_end_offsets
            ):
                if not raw_message_paths or raw_message_paths[-1] == path_str:
                    raw_message_end_offsets[-1] = int(parse_snapshot["record_end"])
                else:
                    # The edited resend arrived in a later rollout fragment;
                    # the abandoned prompt's range cannot reach into another
                    # file, so bind it through the end of its own fragment
                    # (covering the aborted-turn record) instead.
                    prior_size = fragment_sizes.get(raw_message_paths[-1])
                    if isinstance(prior_size, int):
                        raw_message_end_offsets[-1] = max(
                            raw_message_end_offsets[-1], prior_size
                        )
            if capture_raw_offsets and len(state.messages) > before_messages:
                added_messages = len(state.messages) - before_messages
                raw_message_start_offsets.extend(
                    [int(parse_snapshot["record_start"])] * added_messages
                )
                raw_message_paths.extend([path_str] * added_messages)
                if user_message_record and added_messages == 2:
                    raw_message_end_offsets.extend([
                        int(parse_snapshot["record_start"]),
                        int(parse_snapshot["record_end"]),
                    ])
                else:
                    raw_message_end_offsets.extend(
                        [int(parse_snapshot["record_end"])] * added_messages
                    )
            if capture_raw_offsets:
                # Cumulative totals as of each message. A token_count arriving
                # between messages belongs to the response it concludes, so it
                # updates the latest message's snapshot in place.
                token_snapshot = _codex_token_snapshot(state)
                if len(state.messages) > before_messages:
                    raw_message_token_snapshots.extend(
                        [token_snapshot] * (len(state.messages) - before_messages)
                    )
                elif raw_message_token_snapshots:
                    raw_message_token_snapshots[-1] = token_snapshot
    except OSError:
        # RawSourceChanged (an OSError) from the per-fragment two-pass
        # fingerprint check re-raises here under strict parsing.
        if strict_jsonl:
            raise
        return None

    state.stats["input_tokens"] = state.max_input_tokens
    state.stats["output_tokens"] = state.max_output_tokens
    state.stats["cache_read_tokens"] = state.max_cached_tokens

    if state.raw_cwd != target_cwd:
        return None

    # With no timestamped message (a rollout of tool calls only, or one whose
    # messages use a record shape this parser does not read), the trailing
    # tool calls used to flush with timestamp None and the session left with
    # no start or end at all, so the hosted reports could not place it in any
    # month. Fall back to the span of the rollout's own entries.
    entry_clock_only = state.metadata["start_time"] is None
    if entry_clock_only:
        state.metadata["start_time"] = state.first_entry_timestamp
    before_flush = len(state.messages)
    _flush_codex_pending(
        state, timestamp=state.metadata["end_time"] or state.last_entry_timestamp
    )
    if entry_clock_only:
        state.metadata["end_time"] = state.last_entry_timestamp
    if capture_raw_offsets and len(state.messages) > before_flush:
        raw_message_start_offsets.extend(
            [int(parse_snapshot.get("record_start") or 0)]
            * (len(state.messages) - before_flush)
        )
        raw_message_end_offsets.extend(
            [int(parse_snapshot.get("file_size") or 0)]
            * (len(state.messages) - before_flush)
        )
        raw_message_token_snapshots.extend(
            [_codex_token_snapshot(state)] * (len(state.messages) - before_flush)
        )
        raw_message_paths.extend(
            [str(filepaths[-1])] * (len(state.messages) - before_flush)
        )

    if state.metadata["model"] is None:
        model_provider = state.metadata.get("model_provider")
        if isinstance(model_provider, str) and model_provider.strip():
            state.metadata["model"] = f"{model_provider}-codex"
        else:
            state.metadata["model"] = "codex-unknown"

    result = _make_session_result(state.metadata, state.messages, state.stats)
    if result is not None and strict_jsonl:
        # Session-level snapshot stays the active tail's whole-file
        # fingerprint (matches the session-level raw_source_path); sealed
        # segments get exact per-file range fingerprints in finalize.
        result["_raw_source_fingerprint"] = parse_snapshot.get("fingerprint")
    if result is not None and capture_raw_offsets:
        result["_raw_message_start_offsets"] = raw_message_start_offsets
        result["_raw_message_end_offsets"] = raw_message_end_offsets
        result["_raw_message_token_snapshots"] = raw_message_token_snapshots
        result["_raw_source_size"] = parse_snapshot.get("file_size")
        if len(filepaths) > 1:
            result["_raw_message_paths"] = raw_message_paths
            result["_raw_source_sizes_by_path"] = fragment_sizes
            result["_raw_source_fingerprints_by_path"] = fragment_fingerprints
    return result


def _handle_codex_session_meta(
    state: _CodexParseState, entry: dict[str, Any], filepath: Path,
    anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        if state.raw_cwd == UNKNOWN_CODEX_CWD:
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
    # Codex Desktop fork rollouts (retry branches, subagent threads) carry
    # their lineage in the first session_meta; later metas are replayed
    # parent records, so first-wins.
    if state.metadata.get("fork_of") is None:
        forked_from = payload.get("forked_from_id")
        if isinstance(forked_from, str) and forked_from.strip():
            state.metadata["fork_of"] = forked_from.strip()
            thread_source = payload.get("thread_source")
            if isinstance(thread_source, str) and thread_source.strip():
                state.metadata["fork_source"] = thread_source.strip()
            nickname = payload.get("agent_nickname")
            if isinstance(nickname, str) and nickname.strip():
                cleaned_nickname = anonymizer.text(nickname.strip())
                cleaned_nickname, _, _ = redact_text(cleaned_nickname)
                state.metadata["fork_nickname"] = cleaned_nickname


def _handle_codex_turn_context(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
) -> None:
    payload = entry.get("payload", {})
    session_cwd = payload.get("cwd")
    if isinstance(session_cwd, str) and session_cwd.strip():
        if state.raw_cwd == UNKNOWN_CODEX_CWD:
            state.raw_cwd = session_cwd
        if state.metadata["cwd"] is None:
            state.metadata["cwd"] = anonymizer.path(session_cwd)
    if state.metadata["model"] is None:
        model_name = payload.get("model")
        if isinstance(model_name, str) and model_name.strip():
            state.metadata["model"] = model_name
    if state.metadata.get("model_effort") is None:
        state.metadata["model_effort"] = _extract_model_effort(payload)


# Synthetic context fragments Codex injects into model input as user-role
# `response_item` messages (mirrors codex-rs `is_contextual_user_fragment`):
# a text block is contextual when its trimmed text starts with the open
# marker and ends with the close marker, ASCII case-insensitive. Entries
# with a None close marker match on the prefix alone.
_CODEX_CONTEXTUAL_USER_MARKERS: tuple[tuple[str, str | None], ...] = (
    ("<user_instructions>", "</user_instructions>"),
    ("# agents.md instructions", "</instructions>"),
    ("<environment_context>", "</environment_context>"),
    ("<external_", ">"),
    ("<skill>", "</skill>"),
    ("<user_shell_command>", "</user_shell_command>"),
    ("<turn_aborted>", "</turn_aborted>"),
    ("<subagent_notification>", "</subagent_notification>"),
    ("<codex_internal_context", "</codex_internal_context>"),
    ("<goal_context>", "</goal_context>"),
    ("<recommended_plugins>", "</recommended_plugins>"),
    ("<hook_prompt", "</hook_prompt>"),
    ("warning: apply_patch was requested via ", None),
    ("warning: your account was flagged for potentially high-risk cyber activity", None),
    ("warning: the maximum number of unified exec processes you can keep open is", None),
)

# Prefix Codex prepends to a prompt when IDE context / attachments are
# serialized into the raw user message; the real request follows the marker.
_CODEX_USER_REQUEST_MARKER = "## My request for Codex:"


# Longest open/close marker lengths, so the contextual check only has to
# lowercase a bounded head and tail instead of copying the whole block.
_CODEX_MARKER_HEAD_LEN = max(
    len(open_marker) for open_marker, _ in _CODEX_CONTEXTUAL_USER_MARKERS
)
_CODEX_MARKER_TAIL_LEN = max(
    len(close_marker)
    for _, close_marker in _CODEX_CONTEXTUAL_USER_MARKERS
    if close_marker
)


def _is_codex_contextual_user_text(text: str) -> bool:
    start, end = 0, len(text)
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    head = text[start:start + _CODEX_MARKER_HEAD_LEN].lower()
    tail = text[max(start, end - _CODEX_MARKER_TAIL_LEN):end].lower()
    for open_marker, close_marker in _CODEX_CONTEXTUAL_USER_MARKERS:
        if not head.startswith(open_marker):
            continue
        if close_marker is None or tail.endswith(close_marker):
            return True
    return False


def _strip_codex_contextual_close_tag(text: str) -> str:
    lowered = text.lower()
    for _, close_marker in _CODEX_CONTEXTUAL_USER_MARKERS:
        if (
            close_marker
            and close_marker.startswith("</")
            and lowered.endswith(close_marker)
        ):
            return text[: -len(close_marker)].strip()
    return text


def _codex_request_dedup_key(role: str, content: str) -> str:
    """Identity used to match a message across the two rollout formats.

    For user messages, strips any attachment/IDE-context wrapper (and a
    trailing contextual close tag, mirroring the response_item extractor)
    so the two formats compare equal even if one side persists the wrapped
    text and the other the bare request. Assistant text is compared as-is:
    the wrapper marker is a user-input construct there."""
    if role != "user":
        return content.strip()
    idx = content.rfind(_CODEX_USER_REQUEST_MARKER)
    if idx != -1:
        content = content[idx + len(_CODEX_USER_REQUEST_MARKER):]
    return _strip_codex_contextual_close_tag(content.strip())


def _is_codex_cross_format_duplicate(
    state: _CodexParseState, role: str, content: str, origin: str,
) -> bool:
    if state.last_message_origin is None or state.last_message_origin == origin:
        return False
    if not state.messages:
        return False
    last = state.messages[-1]
    if last.get("role") != role:
        return False
    previous = last.get("content")
    if not isinstance(previous, str):
        return False
    return _codex_request_dedup_key(role, previous) == _codex_request_dedup_key(
        role, content
    )


def _codex_user_identity_hash(message: str, media: list[tuple[str, Any]]) -> str:
    """Hash identifying a user submission for retry detection.

    ``media`` holds (kind, value) pairs; empty values are dropped so a
    text-only message hashes identically from either rollout format."""
    entries = [[kind, value] for kind, value in media if value]
    raw_identity = json.dumps(
        {"message": message, "media": entries},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw_identity.encode("utf-8")).hexdigest()


def _is_codex_media_label_text(text: str) -> bool:
    if text in ("<image>", "</image>", "<audio>", "</audio>"):
        return True
    return (
        (text.startswith("<image name=") or text.startswith("<audio name="))
        and text.endswith(">")
    )


def _codex_response_user_message_parts(
    payload: dict[str, Any], anonymizer: Anonymizer,
) -> tuple[str, str] | None:
    """Extract (anonymized text, identity hash) from a user `response_item`.

    Returns None for synthetic context records (AGENTS.md instructions,
    environment context, hook prompts, ...) and for messages with no text.
    A wrapper that embeds the real request after ``## My request for
    Codex:`` is preserved rather than dropped."""
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None
    texts: list[str] = []
    contextual_texts: list[str] = []
    images: list[dict[str, Any]] = []
    audios: list[dict[str, Any]] = []
    has_media = any(
        isinstance(block, dict) and block.get("type") in ("input_image", "input_audio")
        for block in blocks
    )
    for block in blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "input_text":
            text = block.get("text")
            if not isinstance(text, str) or not text.strip():
                continue
            if has_media and _is_codex_media_label_text(text.strip()):
                continue
            if _is_codex_contextual_user_text(text):
                contextual_texts.append(text)
            else:
                texts.append(text)
        elif block_type == "input_image":
            images.append(
                {
                    "image_url": block.get("image_url"),
                    "detail": block.get("detail"),
                }
            )
        elif block_type == "input_audio":
            audios.append({"audio_url": block.get("audio_url")})
    raw_content = "\n\n".join(part.strip() for part in texts).strip()
    if not raw_content:
        # A fully-contextual bundle may still embed the user's request
        # after the wrapper marker — keep the request, drop the context.
        for text in contextual_texts:
            idx = text.rfind(_CODEX_USER_REQUEST_MARKER)
            if idx == -1:
                continue
            raw_content = _strip_codex_contextual_close_tag(
                text[idx + len(_CODEX_USER_REQUEST_MARKER):].strip()
            )
            break
    if not raw_content:
        return None
    input_hash = _codex_user_identity_hash(
        raw_content,
        [("input_images", images), ("input_audio", audios)],
    )
    return anonymizer.text(raw_content), input_hash


def _codex_response_agent_message_text(payload: dict[str, Any]) -> str | None:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        return None
    texts = [
        block.get("text")
        for block in blocks
        if isinstance(block, dict)
        and block.get("type") in ("output_text", "input_text")
        and isinstance(block.get("text"), str)
    ]
    joined = "\n\n".join(part.strip() for part in texts if part.strip()).strip()
    return joined or None


def _handle_codex_response_item(
    state: _CodexParseState, entry: dict[str, Any], anonymizer: Anonymizer,
    include_thinking: bool, timestamp: str | None,
) -> bool:
    """Handle one `response_item` record. Returns True when the record
    replaced a retry-candidate user message in place."""
    payload = entry.get("payload", {})
    item_type = payload.get("type")
    if item_type == "message":
        role = payload.get("role")
        if role == "user":
            message_parts = _codex_response_user_message_parts(payload, anonymizer)
            if message_parts is None:
                return False
            return _ingest_codex_user_message(
                state, message_parts, timestamp, "response_item"
            )
        elif role == "assistant":
            _clear_codex_retry_candidate(state)
            _handle_codex_agent_message(
                state, _codex_response_agent_message_text(payload), timestamp,
                anonymizer, include_thinking, "response_item",
            )
        # developer / system records are instructions, not conversation.
        return False
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
    return False


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


def _codex_user_message_parts(
    payload: dict[str, Any], anonymizer: Anonymizer,
) -> tuple[str, str] | None:
    content = payload.get("message")
    if not isinstance(content, str) or not content.strip():
        return None
    raw_content = content.strip()
    input_hash = _codex_user_identity_hash(
        raw_content,
        [
            ("images", payload.get("images") or []),
            ("local_images", payload.get("local_images") or []),
            ("text_elements", payload.get("text_elements") or []),
        ],
    )
    return anonymizer.text(raw_content), input_hash


def _clear_codex_retry_candidate(state: _CodexParseState) -> None:
    state.retry_candidate_index = None
    state.retry_candidate_replaced = False
    state.retry_candidate_input_hash = None


def _mark_codex_retry_candidate(
    state: _CodexParseState, terminal: str, payload: dict[str, Any],
) -> None:
    """Remember an output-free turn without deleting its prompt yet."""
    if (
        not state.messages
        or state.messages[-1].get("role") != "user"
        or state.pending_tool_uses
        or state.pending_thinking
    ):
        _clear_codex_retry_candidate(state)
        return
    last_agent_message = payload.get("last_agent_message")
    if isinstance(last_agent_message, str) and last_agent_message.strip():
        _clear_codex_retry_candidate(state)
        return

    candidate_index = len(state.messages) - 1
    if state.retry_candidate_index != candidate_index:
        state.retry_candidate_index = candidate_index
        state.retry_candidate_replaced = False
        state.retry_candidate_input_hash = state.last_user_input_hash
    if terminal == "turn_aborted" and payload.get("reason") == "replaced":
        state.retry_candidate_replaced = True


def _replace_codex_retry_user_message(
    state: _CodexParseState,
    replacement_parts: tuple[str, str],
    timestamp: str | None,
    origin: str,
) -> bool:
    """Replace a confirmed retry in place, preserving its raw start boundary."""
    candidate_index = state.retry_candidate_index
    if candidate_index is None:
        return False
    replacement, replacement_hash = replacement_parts
    if (
        candidate_index != len(state.messages) - 1
        or state.messages[candidate_index].get("role") != "user"
    ):
        _clear_codex_retry_candidate(state)
        return False

    should_replace = (
        state.retry_candidate_replaced
        or state.retry_candidate_input_hash == replacement_hash
    )
    _clear_codex_retry_candidate(state)
    if not should_replace:
        return False

    # Mutating the existing slot keeps its first-attempt raw start offset.  A
    # sealed segment immediately before this turn therefore keeps the exact
    # same byte boundary as the rollout grows from user -> abort -> retry.
    previous_timestamp = state.messages[candidate_index].get("timestamp")
    state.messages[candidate_index]["content"] = replacement
    if timestamp is not None:
        state.messages[candidate_index]["timestamp"] = timestamp
        if state.metadata.get("start_time") == previous_timestamp:
            timestamps = [
                message.get("timestamp")
                for message in state.messages
                if isinstance(message.get("timestamp"), str)
            ]
            state.metadata["start_time"] = min(timestamps, default=timestamp)
    state.last_user_input_hash = replacement_hash
    state.last_message_origin = origin
    _update_time_bounds(state.metadata, timestamp)
    return True


def _ingest_codex_user_message(
    state: _CodexParseState, message_parts: tuple[str, str],
    timestamp: str | None, origin: str,
) -> bool:
    """Route one user message (either rollout format) into the transcript.

    Returns True when it replaced a retry-candidate message in place. The
    cross-format duplicate check runs first and leaves any armed retry
    candidate untouched: the twin record must not clear the candidate
    before the other format's copy gets its chance to replace it."""
    content, _ = message_parts
    if (
        not state.pending_tool_uses
        and not state.pending_thinking
        and _is_codex_cross_format_duplicate(state, "user", content, origin)
    ):
        # The same message already arrived via the other rollout format;
        # consume the pair so a genuine identical resend is kept.
        state.last_message_origin = None
        return False
    if _replace_codex_retry_user_message(state, message_parts, timestamp, origin):
        return True
    _handle_codex_user_message(state, message_parts, timestamp, origin)
    return False


def _handle_codex_user_message(
    state: _CodexParseState, message_parts: tuple[str, str],
    timestamp: str | None, origin: str,
) -> None:
    content, input_hash = message_parts
    _flush_codex_pending(state, timestamp)
    state.messages.append(
        {
            "role": "user",
            "content": content,
            "timestamp": timestamp,
        }
    )
    state.last_user_input_hash = input_hash
    state.last_message_origin = origin
    state.stats["user_messages"] += 1
    _update_time_bounds(state.metadata, timestamp)


def _resolve_codex_tool_uses(state: _CodexParseState) -> list[dict]:
    """Attach outputs from tool_result_map and strip internal _call_id field."""
    resolved = []
    for tu in state.pending_tool_uses:
        call_id = tu.pop("_call_id", None)
        if call_id and call_id in state.tool_result_map:
            r = state.tool_result_map.pop(call_id)
            tu["output"] = r["output"]
            tu["status"] = r["status"]
        resolved.append(tu)
    return resolved


def _handle_codex_agent_message(
    state: _CodexParseState, content: Any,
    timestamp: str | None, anonymizer: Anonymizer, include_thinking: bool,
    origin: str,
) -> None:
    msg: dict[str, Any] = {"role": "assistant"}
    if isinstance(content, str) and content.strip():
        cleaned = anonymizer.text(content.strip())
        if (
            not state.pending_tool_uses
            and not state.pending_thinking
            and _is_codex_cross_format_duplicate(state, "assistant", cleaned, origin)
        ):
            state.last_message_origin = None
            return
        msg["content"] = cleaned
    if state.pending_thinking and include_thinking:
        msg["thinking"] = "\n\n".join(state.pending_thinking)
    if state.pending_tool_uses:
        msg["tool_uses"] = _resolve_codex_tool_uses(state)

    if len(msg) > 1:
        msg["timestamp"] = timestamp
        state.messages.append(msg)
        state.last_message_origin = origin
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
    state.last_message_origin = None
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


def _extract_codex_session_meta_id(session_file: Path) -> str | None:
    """Return the first embedded ``session_meta.payload.id``, or None.

    First-wins mirrors ``_handle_codex_session_meta``: a fork rollout's own
    meta precedes any replayed parent records, so its own id is returned.
    Bounded to the leading records — session_meta is normally the first.
    """
    try:
        for index, entry in enumerate(_iter_jsonl(session_file)):
            if entry.get("type") == "session_meta":
                meta_id = entry.get("payload", {}).get("id")
                if isinstance(meta_id, str) and meta_id.strip():
                    return meta_id.strip()
            if index >= 24:
                break
    except OSError:
        return None
    return None


def _group_codex_rollout_fragments(session_files: list[Path]) -> list[list[Path]]:
    """Group rollout files that carry the same embedded session id.

    Codex Desktop's cancel+edit flow can continue one conversation in a new
    ``<stem>_<continuation>.jsonl`` rollout that repeats the original
    ``session_meta.payload.id`` (issue #214). Fragments must parse through
    one shared state, so they are grouped here, ordered by filename (the
    name embeds the creation timestamp, and a same-timestamp continuation
    suffix sorts after its base). A fork rollout declares its own id plus
    ``forked_from_id``, so it naturally stays a separate group; a file
    without a readable meta id stays alone.
    """
    groups: dict[str, list[Path]] = {}
    ordered_keys: list[str] = []
    for session_file in session_files:
        meta_id = _extract_codex_session_meta_id(session_file)
        key = meta_id if meta_id else f"__no_meta__:{session_file}"
        if key not in groups:
            groups[key] = []
            ordered_keys.append(key)
        groups[key].append(session_file)
    return [
        sorted(groups[key], key=lambda path: path.name) for key in ordered_keys
    ]


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


def _build_workbuddy_project_name(dir_name: str) -> str:
    return f"workbuddy:{Path(dir_name).name or dir_name}"


def _build_kiro_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_KIRO_CWD:
        return "kiro:unknown"
    return f"kiro:{Path(cwd).name or cwd}"


def _build_antigravity_project_name(cwd: str) -> str:
    if cwd == UNKNOWN_ANTIGRAVITY_CWD:
        return "antigravity:unknown"
    return f"antigravity:{Path(cwd).name or cwd}"


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
                result = tool_result_map.pop(block.get("id", ""), None)
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


# ---------------------------------------------------------------------------
# Kiro parser
# ---------------------------------------------------------------------------
#
# Kiro has changed its on-disk format several times, and one install can hold
# sessions in more than one of them:
#
#   event log     ~/.kiro/sessions/[<workspace-hash>/]sess_<id>/messages.jsonl
#                 with a session.json beside it (current Kiro CLI and IDE)
#   CLI database  <Kiro CLI data dir>/data.sqlite3, tables conversations_v2
#                 and conversations (Kiro CLI 1.x and 2.x)
#   CLI JSONL     ~/.kiro/sessions/cli/<id>.jsonl with <id>.json beside it
#                 (Kiro CLI 2.x)
#   IDE session   <IDE storage>/workspace-sessions/<base64url(cwd)>/<id>.json
#                 (earlier Kiro IDE)
#
# Projects are keyed by working directory, as for Codex and OpenCode.

_KIRO_EXECUTION_ID_RE = re.compile(rb'"executionId"\s*:\s*"([^"]+)"')


def _kiro_str(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _load_kiro_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return None


def _kiro_timestamp(value: Any) -> str | None:
    """Normalize Kiro's timestamps to ISO-8601 UTC.

    Depending on the store they are ISO strings (sometimes with a local
    offset), epoch seconds, epoch milliseconds, or milliseconds as a string.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        seconds = value / 1000 if abs(value) >= 1e11 else value
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    text = _kiro_str(value)
    if text is None:
        return None
    if text.isdigit():
        return _kiro_timestamp(int(text))
    try:
        # Python 3.10 rejects a "Z" suffix and more than six fractional digits.
        parsed = datetime.fromisoformat(
            re.sub(r"(\.\d{6})\d+", r"\1", text.replace("Z", "+00:00"))
        )
    except ValueError:
        return text
    if parsed.tzinfo is None:
        return text
    return parsed.astimezone(timezone.utc).isoformat()


def _kiro_text(value: Any, *, structured: bool = False) -> str:
    """Flatten Kiro content, a string or a list of typed blocks, to text.

    A text block is {"Text": ...} in the CLI database, {"kind": "text",
    "data": ...} in CLI JSONL and {"type": "text", "text": ...} in the IDE.
    ``structured`` also keeps JSON payloads, which is how tool results carry
    command output; message text drops them so attachments are not inlined.
    """
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        parts = (_kiro_text(item, structured=structured) for item in value)
        return "\n\n".join(part for part in parts if part)
    if isinstance(value, dict):
        if isinstance(value.get("Text"), str):
            return value["Text"].strip()
        if value.get("kind") == "text" and isinstance(value.get("data"), str):
            return value["data"].strip()
        if value.get("type") == "text" and isinstance(value.get("text"), str):
            return value["text"].strip()
        if structured:
            if "Json" in value:
                value = value["Json"]
            elif value.get("kind") == "json" and "data" in value:
                value = value["data"]
            return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return ""


def _kiro_title(value: Any, anonymizer: Anonymizer) -> str | None:
    title = re.sub(r"\s+", " ", value).strip() if isinstance(value, str) else ""
    return anonymizer.text(title) if title else None


def _kiro_model(value: Any) -> str | None:
    model = _kiro_str(value)
    # "auto" is Kiro's model router, not a model another agent could mean.
    return "kiro-auto" if model and model.lower() == "auto" else model


def _kiro_tool_input(tool_name: str, args: Any, anonymizer: Anonymizer) -> dict:
    args = _json_loads_maybe(args)
    parsed = _parse_tool_input(tool_name, {} if args is None else args, anonymizer)
    # _parse_tool_input leaves nested containers alone, but Kiro tools such as
    # fs_read put their paths inside a list of operations.
    return {
        key: _anonymize_claude_science_value(value, anonymizer, key)
        if isinstance(value, (dict, list))
        else value
        for key, value in parsed.items()
    }


class _KiroTranscript:
    """Builds the shared message shape from any of Kiro's storage formats.

    Kiro stores an assistant turn as separate text, reasoning, tool-call and
    tool-result records. This folds them back into one assistant message per
    model response, with each tool result attached to the call it answers.
    """

    def __init__(self, session_id: str, anonymizer: Anonymizer) -> None:
        self.metadata: dict[str, Any] = {
            "session_id": session_id,
            "cwd": None,
            "git_branch": None,
            "model": None,
            "model_effort": None,
            "start_time": None,
            "end_time": None,
        }
        self.anonymizer = anonymizer
        self.messages: list[dict[str, Any]] = []
        self.stats = _make_stats()
        self._open: dict[str, Any] | None = None
        self._calls: dict[str, dict[str, Any]] = {}

    def close(self) -> None:
        """End the current assistant message; the next part starts a new one."""
        self._open = None

    def user(self, text: Any, timestamp: str | None) -> None:
        text = _kiro_str(text)
        if text is None:
            return
        self._open = None
        self.messages.append(
            {"role": "user", "content": self.anonymizer.text(text), "timestamp": timestamp}
        )
        self.stats["user_messages"] += 1
        _update_time_bounds(self.metadata, timestamp)

    def _assistant(self, timestamp: str | None) -> dict[str, Any]:
        if self._open is None:
            self._open = {"role": "assistant", "timestamp": timestamp}
            self.messages.append(self._open)
            self.stats["assistant_messages"] += 1
        _update_time_bounds(self.metadata, timestamp)
        return self._open

    def assistant_text(
        self, text: Any, timestamp: str | None, *, thinking: bool = False
    ) -> bool:
        text = _kiro_str(text)
        if text is None:
            return False
        # Reasoning after a reply, or a reply after tool calls, is a new response.
        if self._open is not None and (
            "tool_uses" in self._open or (thinking and "content" in self._open)
        ):
            self._open = None
        message = self._assistant(timestamp)
        key = "thinking" if thinking else "content"
        text = self.anonymizer.text(text)
        message[key] = f"{message[key]}\n\n{text}" if key in message else text
        return True

    def tool_call(self, call_id: Any, name: Any, args: Any, timestamp: str | None) -> bool:
        name = _kiro_str(name)
        if name is None:
            return False
        tool_use = {"tool": name, "input": _kiro_tool_input(name, args, self.anonymizer)}
        self._assistant(timestamp).setdefault("tool_uses", []).append(tool_use)
        self.stats["tool_uses"] += 1
        if isinstance(call_id, str) and call_id:
            self._calls[call_id] = tool_use
        return True

    def tool_result(self, call_id: Any, content: Any, status: Any = None) -> None:
        # Whatever the model says next is a response to this result.
        self._open = None
        tool_use = self._calls.pop(call_id, None) if isinstance(call_id, str) else None
        if tool_use is None:
            return
        text = _kiro_text(content, structured=True)
        tool_use["output"] = {"text": self.anonymizer.text(text)} if text else {}
        if isinstance(status, str) and status:
            tool_use["status"] = "error" if status.lower() == "error" else "success"

    def result(self) -> dict[str, Any] | None:
        if not self.metadata["model"]:
            self.metadata["model"] = "kiro-unknown"
        return _make_session_result(self.metadata, self.messages, self.stats)


def _kiro_ref(
    kind: str, session_id: str, cwd: Any, path: Path, size: int, **extra: Any
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": session_id,
        "cwd": _kiro_str(cwd),
        "path": path,
        "size": size,
        **extra,
    }


def _kiro_file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _list_kiro_event_log_refs() -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    try:
        entries = sorted(p for p in KIRO_SESSIONS_DIR.iterdir() if p.is_dir())
    except OSError:
        return refs
    for entry in entries:
        if entry.name.startswith("sess_"):
            session_dirs = [entry]
        else:
            try:
                session_dirs = sorted(
                    p for p in entry.iterdir() if p.is_dir() and p.name.startswith("sess_")
                )
            except OSError:
                continue
        for session_dir in session_dirs:
            transcript = session_dir / "messages.jsonl"
            size = _kiro_file_size(transcript)
            if size is None:
                continue
            meta = _load_kiro_json(session_dir / "session.json")
            paths = meta.get("workspacePaths") if isinstance(meta, dict) else None
            cwd = paths[0] if isinstance(paths, list) and paths else None
            refs.append(_kiro_ref("event-log", session_dir.name, cwd, transcript, size))
    return refs


def _list_kiro_cli_db_refs() -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    if not KIRO_CLI_DB_PATH.is_file():
        return refs
    try:
        with closing(_connect_sqlite_readonly(KIRO_CLI_DB_PATH)) as conn:
            tables = {
                row[0]
                for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
            }
            if "conversations_v2" in tables:
                # A conversation can be stored under several directories; the
                # most recently updated row comes first and wins in the index.
                for row in conn.execute(
                    "SELECT key, conversation_id, length(value) AS size "
                    "FROM conversations_v2 ORDER BY updated_at DESC, created_at DESC"
                ):
                    session_id = _kiro_str(row["conversation_id"])
                    if session_id:
                        refs.append(_kiro_ref(
                            "cli-db", session_id, row["key"], KIRO_CLI_DB_PATH,
                            _safe_int(row["size"]), table="conversations_v2", key=row["key"],
                        ))
            if "conversations" in tables:
                # Kiro CLI 1.x kept one conversation per directory.
                for row in conn.execute("SELECT key, value FROM conversations"):
                    value = row["value"]
                    session_id = _kiro_str(_load_json_field(value).get("conversation_id"))
                    if session_id:
                        refs.append(_kiro_ref(
                            "cli-db", session_id, row["key"], KIRO_CLI_DB_PATH,
                            len(value), table="conversations", key=row["key"],
                        ))
    except sqlite3.Error:
        logger.warning("Failed to read Kiro CLI database %s", KIRO_CLI_DB_PATH)
    return refs


def _list_kiro_cli_jsonl_refs() -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for directory in (KIRO_SESSIONS_DIR / "cli", KIRO_SESSIONS_DIR):
        try:
            transcripts = sorted(directory.glob("*.jsonl"))
        except OSError:
            continue
        for transcript in transcripts:
            size = _kiro_file_size(transcript)
            if size is None:
                continue
            meta = _load_kiro_json(transcript.with_suffix(".json"))
            meta = meta if isinstance(meta, dict) else {}
            session_id = _kiro_str(meta.get("session_id")) or transcript.stem
            refs.append(_kiro_ref("cli-jsonl", session_id, meta.get("cwd"), transcript, size))
    return refs


def _decode_kiro_workspace_dir(name: str) -> str | None:
    """The IDE names each workspace-sessions folder base64url(workspace path)."""
    try:
        decoded = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    if PurePosixPath(decoded).is_absolute() or PureWindowsPath(decoded).is_absolute():
        return decoded
    return None


def _list_kiro_ide_session_refs() -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    try:
        workspace_dirs = sorted(
            p for p in (KIRO_IDE_DIR / "workspace-sessions").iterdir() if p.is_dir()
        )
    except OSError:
        return refs
    for workspace_dir in workspace_dirs:
        listing = _load_kiro_json(workspace_dir / "sessions.json")
        listed = {
            entry["sessionId"]: entry
            for entry in (listing if isinstance(listing, list) else [])
            if isinstance(entry, dict) and isinstance(entry.get("sessionId"), str)
        }
        try:
            session_files = sorted(workspace_dir.glob("*.json"))
        except OSError:
            continue
        for session_file in session_files:
            size = _kiro_file_size(session_file)
            if size is None or session_file.name == "sessions.json":
                continue
            entry = listed.get(session_file.stem, {})
            cwd = _kiro_str(entry.get("workspaceDirectory")) or _decode_kiro_workspace_dir(
                workspace_dir.name
            )
            if cwd is None:
                # Neither the listing nor the folder name gave a path, so read
                # it from the session itself.
                session = _load_kiro_json(session_file)
                cwd = session.get("workspaceDirectory") if isinstance(session, dict) else None
            refs.append(_kiro_ref(
                "ide-session", session_file.stem, cwd, session_file, size,
                title=entry.get("title"), created=entry.get("dateCreated"),
            ))
    return refs


def _get_kiro_project_index(refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
    global _KIRO_PROJECT_INDEX
    if refresh or not _KIRO_PROJECT_INDEX:
        _KIRO_PROJECT_INDEX = _build_kiro_project_index()
    return _KIRO_PROJECT_INDEX


def _build_kiro_project_index() -> dict[str, list[dict[str, Any]]]:
    """Group every Kiro session by working directory.

    A conversation can sit in more than one store, so stores are read newest
    format first and a session ID that was already seen is skipped.
    """
    index: dict[str, list[dict[str, Any]]] = {}
    seen: set[str] = set()
    for list_refs in (
        _list_kiro_event_log_refs,
        _list_kiro_cli_db_refs,
        _list_kiro_cli_jsonl_refs,
        _list_kiro_ide_session_refs,
    ):
        for ref in list_refs():
            if ref["id"] in seen:
                continue
            seen.add(ref["id"])
            index.setdefault(ref["cwd"] or UNKNOWN_KIRO_CWD, []).append(ref)
    return index


def _discover_kiro_projects() -> list[dict]:
    index = _get_kiro_project_index(refresh=True)
    return [
        {
            "dir_name": cwd,
            "display_name": _build_kiro_project_name(cwd),
            "session_count": len(refs),
            "total_size_bytes": sum(ref["size"] for ref in refs),
            "source": KIRO_SOURCE,
        }
        for cwd, refs in sorted(index.items())
    ]


def _parse_kiro_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> list[dict]:
    sessions = []
    executions: dict[str, Path] | None = None
    for ref in _get_kiro_project_index().get(project_dir_name, []):
        raw_source_path = str(ref["path"])
        client_origin = None
        if ref["kind"] == "event-log":
            # Written by both the CLI and the IDE, so the origin is unknown.
            parsed = _parse_kiro_event_log(ref, anonymizer, include_thinking)
        elif ref["kind"] == "cli-db":
            parsed = _parse_kiro_cli_db_conversation(ref, anonymizer)
            raw_source_path += f"#conversation={ref['id']}"
            client_origin = "cli"
        elif ref["kind"] == "cli-jsonl":
            parsed = _parse_kiro_cli_jsonl(ref, anonymizer)
            client_origin = "cli"
        else:
            if executions is None:
                executions = _kiro_ide_execution_index(ref["cwd"]) if ref["cwd"] else {}
            parsed = _parse_kiro_ide_session(ref, anonymizer, executions)
            client_origin = "desktop"
        if not parsed or not parsed["messages"]:
            continue
        parsed["project"] = _build_kiro_project_name(project_dir_name)
        parsed["source"] = KIRO_SOURCE
        parsed["raw_source_path"] = raw_source_path
        if client_origin:
            parsed["client_origin"] = client_origin
        sessions.append(parsed)
    return sessions


def _parse_kiro_event_log(
    ref: dict[str, Any], anonymizer: Anonymizer, include_thinking: bool
) -> dict | None:
    """Parse sess_<id>/messages.jsonl: one {id, timestamp, payload} event per line."""
    transcript = _KiroTranscript(ref["id"], anonymizer)
    meta = _load_kiro_json(ref["path"].parent / "session.json")
    meta = meta if isinstance(meta, dict) else {}
    metadata = transcript.metadata
    metadata["model"] = _kiro_model(meta.get("modelId"))
    metadata["start_time"] = _kiro_timestamp(meta.get("createdAt"))
    metadata["end_time"] = _kiro_timestamp(meta.get("lastModifiedAt"))
    metadata["segment_title"] = _kiro_title(meta.get("title"), anonymizer)

    try:
        for entry in _iter_jsonl(ref["path"]):
            payload = entry.get("payload") if isinstance(entry, dict) else None
            if not isinstance(payload, dict):
                continue
            timestamp = _kiro_timestamp(entry.get("timestamp"))
            kind = payload.get("type")
            if kind == "user":
                transcript.user(_kiro_text(payload.get("content")), timestamp)
            elif kind == "assistant":
                if metadata["model"] is None:
                    metadata["model"] = _kiro_model(payload.get("reasoningModelId"))
                thinking = payload.get("operationType") == "Reasoning"
                if include_thinking or not thinking:
                    transcript.assistant_text(
                        _kiro_text(payload.get("content")), timestamp, thinking=thinking
                    )
            elif kind == "tool_call":
                transcript.tool_call(
                    payload.get("toolCallId"), payload.get("toolName"),
                    payload.get("args"), timestamp,
                )
            elif kind == "tool_result":
                transcript.tool_result(payload.get("toolCallId"), payload.get("content"))
            elif kind == "sub_agent_start":
                # A subagent spawn is recorded as its own event pair rather
                # than a tool call; its session ID is what links the two.
                transcript.tool_call(
                    payload.get("subSessionId"),
                    "invoke_subagent",
                    {
                        "subagent_type": payload.get("subAgentName"),
                        "description": payload.get("explanation"),
                        "prompt": payload.get("prompt"),
                    },
                    timestamp,
                )
            elif kind == "sub_agent_complete":
                transcript.tool_result(
                    payload.get("subSessionId"),
                    payload.get("response") or payload.get("errorMessage"),
                    payload.get("status"),
                )
    except OSError:
        return None
    return transcript.result()


def _parse_kiro_cli_db_conversation(ref: dict[str, Any], anonymizer: Anonymizer) -> dict | None:
    """Parse one conversation row of the Kiro CLI database.

    The row's value is the CLI's serialized conversation state, whose
    ``history`` is a list of {user, assistant, request_metadata} turns.
    """
    v2 = ref["table"] == "conversations_v2"
    try:
        with closing(_connect_sqlite_readonly(ref["path"])) as conn:
            if v2:
                row = conn.execute(
                    "SELECT value, created_at, updated_at FROM conversations_v2 "
                    "WHERE conversation_id = ? AND key = ?",
                    (ref["id"], ref["key"]),
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT value FROM conversations WHERE key = ?", (ref["key"],)
                ).fetchone()
    except sqlite3.Error:
        logger.warning("Failed to parse Kiro conversation %s in %s", ref["id"], ref["path"])
        return None
    if row is None:
        return None
    state = _load_json_field(row["value"])

    transcript = _KiroTranscript(ref["id"], anonymizer)
    metadata = transcript.metadata
    if v2:
        metadata["start_time"] = _kiro_timestamp(row["created_at"])
        metadata["end_time"] = _kiro_timestamp(row["updated_at"])
    model_info = state.get("model_info")
    metadata["model"] = _kiro_model(
        model_info.get("model_id") if isinstance(model_info, dict) else state.get("model")
    )

    history = state.get("history")
    for turn in history if isinstance(history, list) else []:
        if isinstance(turn, dict):
            user, assistant, request = (
                turn.get("user"), turn.get("assistant"), turn.get("request_metadata")
            )
        elif isinstance(turn, list) and len(turn) >= 2:
            # Early versions stored each turn as a [user, assistant] pair.
            user, assistant, request = turn[0], turn[1], None
        else:
            continue

        if isinstance(user, dict) and isinstance(user.get("content"), dict):
            timestamp = _kiro_timestamp(user.get("timestamp"))
            content = user["content"]
            prompt = content.get("Prompt")
            results = content.get("ToolUseResults")
            cancelled = content.get("CancelledToolUses")
            for block in (results, cancelled):
                tool_results = block.get("tool_use_results") if isinstance(block, dict) else None
                for item in tool_results if isinstance(tool_results, list) else []:
                    if isinstance(item, dict):
                        transcript.tool_result(
                            item.get("tool_use_id"), item.get("content"), item.get("status")
                        )
            for block in (prompt, cancelled):
                if isinstance(block, dict):
                    transcript.user(block.get("prompt"), timestamp)

        if isinstance(assistant, dict):
            reply = assistant.get("ToolUse") or assistant.get("Response")
            if not isinstance(reply, dict):
                continue
            timestamp = _kiro_timestamp(
                request.get("stream_end_timestamp_ms") if isinstance(request, dict) else None
            )
            # Each turn holds exactly one model response.
            transcript.close()
            transcript.assistant_text(reply.get("content"), timestamp)
            tool_uses = reply.get("tool_uses")
            for tool_use in tool_uses if isinstance(tool_uses, list) else []:
                if isinstance(tool_use, dict):
                    transcript.tool_call(
                        tool_use.get("id"), tool_use.get("name"), tool_use.get("args"), timestamp
                    )
    return transcript.result()


def _parse_kiro_cli_jsonl(ref: dict[str, Any], anonymizer: Anonymizer) -> dict | None:
    """Parse a Kiro CLI <id>.jsonl: one {version, kind, data} record per line."""
    transcript = _KiroTranscript(ref["id"], anonymizer)
    meta = _load_kiro_json(ref["path"].with_suffix(".json"))
    meta = meta if isinstance(meta, dict) else {}
    metadata = transcript.metadata
    metadata["start_time"] = _kiro_timestamp(meta.get("created_at"))
    metadata["end_time"] = _kiro_timestamp(meta.get("updated_at"))
    metadata["segment_title"] = _kiro_title(meta.get("title"), anonymizer)
    model: Any = meta
    for key in ("session_state", "rts_model_state", "model_info", "model_id"):
        model = model.get(key) if isinstance(model, dict) else None
    metadata["model"] = _kiro_model(model)

    try:
        for entry in _iter_jsonl(ref["path"]):
            data = entry.get("data") if isinstance(entry, dict) else None
            if not isinstance(data, dict):
                continue
            record_meta = data.get("meta")
            timestamp = _kiro_timestamp(
                record_meta.get("timestamp") if isinstance(record_meta, dict) else None
            )
            content = data.get("content")
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            kind = entry.get("kind")
            if kind == "Prompt":
                transcript.user(_kiro_text(blocks), timestamp)
            elif kind == "AssistantMessage":
                # Each record holds exactly one model response.
                transcript.close()
                for block in blocks:
                    block_data = block.get("data")
                    if block.get("kind") == "text":
                        transcript.assistant_text(block_data, timestamp)
                    elif block.get("kind") == "toolUse" and isinstance(block_data, dict):
                        transcript.tool_call(
                            block_data.get("toolUseId"), block_data.get("name"),
                            block_data.get("input"), timestamp,
                        )
            elif kind == "ToolResults":
                for block in blocks:
                    block_data = block.get("data")
                    if block.get("kind") == "toolResult" and isinstance(block_data, dict):
                        transcript.tool_result(
                            block_data.get("toolUseId"), block_data.get("content"),
                            block_data.get("status"),
                        )
    except OSError:
        return None
    return transcript.result()


def _kiro_ide_execution_index(workspace: str) -> dict[str, Path]:
    """Map executionId to its execution log for one IDE workspace.

    An IDE session file is only the conversation spine. What the agent said
    and did in each turn is in extensionless JSON execution logs under
    <IDE storage>/<sha256(workspace)[:32]>/<bucket>/, linked by executionId.
    """
    root = KIRO_IDE_DIR / hashlib.sha256(workspace.encode("utf-8")).hexdigest()[:32]
    index: dict[str, Path] = {}
    try:
        buckets = [p for p in root.iterdir() if p.is_dir()]
    except OSError:
        return index
    for bucket in buckets:
        try:
            logs = [p for p in bucket.iterdir() if p.is_file() and not p.suffix]
        except OSError:
            continue
        for log in logs:
            try:
                with log.open("rb") as handle:
                    match = _KIRO_EXECUTION_ID_RE.search(handle.read(2048))
            except OSError:
                continue
            if match:
                index[match.group(1).decode("utf-8", errors="replace")] = log
    return index


def _kiro_ide_action_input(action_input: dict[str, Any]) -> dict[str, Any]:
    """Reduce an IDE file action to its path plus what changed.

    An edit stores the whole file before and after, which would otherwise put
    two full copies of every edited file into the trace.
    """
    result = {
        key: value
        for key, value in action_input.items()
        if key not in ("originalContent", "modifiedContent")
    }
    original = action_input.get("originalContent")
    modified = action_input.get("modifiedContent")
    if isinstance(modified, str):
        if isinstance(original, str) and original:
            name = action_input.get("file") if isinstance(action_input.get("file"), str) else "file"
            result["diff"] = "\n".join(difflib.unified_diff(
                original.splitlines(), modified.splitlines(),
                fromfile=f"a/{name}", tofile=f"b/{name}", lineterm="",
            ))
        else:
            result["content"] = modified
    return result


def _replay_kiro_ide_execution(transcript: _KiroTranscript, execution_log: Path) -> bool:
    """Add one execution log's actions in order; return whether any were added."""
    execution = _load_kiro_json(execution_log)
    actions = execution.get("actions") if isinstance(execution, dict) else None
    added = False
    for action in actions if isinstance(actions, list) else []:
        if not isinstance(action, dict):
            continue
        action_type = action.get("actionType")
        action_input = action.get("input")
        output = action.get("output")
        if action_type == "say":
            message = output.get("message") if isinstance(output, dict) else None
            added |= transcript.assistant_text(message, None)
        elif isinstance(action_input, dict) and action_input:
            added |= transcript.tool_call(
                action.get("actionId"), action_type, _kiro_ide_action_input(action_input), None
            )
    return added


def _parse_kiro_ide_session(
    ref: dict[str, Any], anonymizer: Anonymizer, executions: dict[str, Path]
) -> dict | None:
    """Parse a Kiro IDE workspace-sessions/<base64url(cwd)>/<id>.json file."""
    session = _load_kiro_json(ref["path"])
    history = session.get("history") if isinstance(session, dict) else None
    if not isinstance(history, list):
        return None

    transcript = _KiroTranscript(ref["id"], anonymizer)
    metadata = transcript.metadata
    # Messages carry no timestamps, so the session spans from its listed
    # creation time to the file's last write.
    try:
        modified = datetime.fromtimestamp(
            ref["path"].stat().st_mtime, tz=timezone.utc
        ).isoformat()
    except OSError:
        modified = None
    metadata["start_time"] = _kiro_timestamp(ref.get("created")) or modified
    metadata["end_time"] = modified
    metadata["segment_title"] = _kiro_title(session.get("title") or ref.get("title"), anonymizer)

    for item in history:
        message = item.get("message") if isinstance(item, dict) else None
        if not isinstance(message, dict):
            continue
        text = _kiro_text(message.get("content"))
        if message.get("role") == "user":
            transcript.user(text, None)
        elif message.get("role") == "assistant":
            transcript.close()
            execution_id = item.get("executionId")
            execution_log = executions.get(execution_id) if isinstance(execution_id, str) else None
            if execution_log and _replay_kiro_ide_execution(transcript, execution_log):
                continue
            if not text:
                # No execution log and no reply text: use the raw completions.
                prompt_logs = item.get("promptLogs")
                text = _kiro_text([
                    log.get("completion")
                    for log in (prompt_logs if isinstance(prompt_logs, list) else [])
                    if isinstance(log, dict)
                ])
            transcript.assistant_text(text, None)
    return transcript.result()


# Antigravity CLI (`agy`) writes a plaintext step log per conversation:
#
#   <data dir>/brain/<conversation-id>/.system_generated/logs/
#       transcript_full.jsonl   every step, nothing cut
#       transcript.jsonl        the same steps, with long fields cut and tool
#                               arguments encoded as JSON strings
#
# A line is one step: {step_index, source, type, status, created_at} plus
# content, thinking, tool_calls and token counts, depending on the type. The
# log names neither the working directory nor a title. Those come from
# conversation_summaries.db, with history.jsonl and
# cache/last_conversations.json as further sources for the directory.
#
# Projects are keyed by working directory, as for Codex and Kiro.

_ANTIGRAVITY_TRANSCRIPT_NAMES = ("transcript_full.jsonl", "transcript.jsonl")
_ANTIGRAVITY_USER_REQUEST_RE = re.compile(r"<USER_REQUEST>(.*)</USER_REQUEST>", re.DOTALL)
# Blocks the CLI appends to a prompt: the local time, and settings changes.
_ANTIGRAVITY_INJECTED_BLOCK_RE = re.compile(
    r"<(ADDITIONAL_METADATA|USER_SETTINGS_CHANGE)>.*?</\1>", re.DOTALL
)
_ANTIGRAVITY_MODEL_RE = re.compile(r"`Model Selection` from .+? to (.+?)\.(?=\s|$)")
# The tag must start a line: the sentence introducing the block names it too.
_ANTIGRAVITY_SYSTEM_MESSAGE_RE = re.compile(
    r"^<SYSTEM_MESSAGE>(.*)</SYSTEM_MESSAGE>", re.DOTALL | re.MULTILINE
)
_ANTIGRAVITY_MESSAGE_RE = re.compile(
    r"\[Message\] timestamp=\S+ sender=(\S+)(?: priority=\S+)?(?: content=|\s+)?(.*)",
    re.DOTALL,
)
# The line a command's result opens with, after its Created At / Completed At
# lines. Anchored so the same words inside a command's own output never match.
_ANTIGRAVITY_EXIT_CODE_RE = re.compile(
    r"\A(?:(?:Created|Completed) At:[^\n]*\n)*\s*(?:The )?[Cc]ommand exited with code (-?\d+)\."
)


def _antigravity_file_uri_path(uri: Any) -> str | None:
    """Return the local path a file:// URI names, or None if it names none."""
    if not isinstance(uri, str):
        return None
    try:
        parts = urlsplit(uri.strip())
    except ValueError:
        return None
    if parts.scheme.lower() != "file" or parts.netloc.lower() not in ("", "localhost"):
        return None
    path = unquote(parts.path)
    # file:///C:/Users/dev/project names C:/Users/dev/project.
    if re.match(r"/[A-Za-z]:", path):
        path = path[1:]
    return (path.rstrip("/") or path) or None


def _load_antigravity_summaries() -> dict[str, dict[str, Any]]:
    """Read each conversation's folder, title and parent from conversation_summaries.db."""
    db_path = ANTIGRAVITY_DIR / "conversation_summaries.db"
    summaries: dict[str, dict[str, Any]] = {}
    try:
        db_path.stat()
    except FileNotFoundError:
        # Older installations may provide only the resume files.
        return summaries
    with closing(_connect_sqlite_readonly(db_path)) as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(conversation_summaries)")}
        if "conversation_id" not in columns:
            raise ValueError("missing conversation_summaries.conversation_id")
        # Earlier builds lack some of these columns.
        wanted = [
            column
            for column in ("workspace_uris", "title", "parent_conversation_id")
            if column in columns
        ]
        for row in conn.execute(
            f"SELECT {', '.join(['conversation_id', *wanted])} FROM conversation_summaries"
        ):
            conversation_id = _kiro_str(row["conversation_id"])
            if conversation_id is None:
                continue
            values = {column: row[column] for column in wanted}
            uris = _json_loads_maybe(values.get("workspace_uris"))
            if uris is not None and not isinstance(uris, list):
                raise ValueError("invalid workspace_uris")
            folders = map(_antigravity_file_uri_path, uris or [])
            summaries[conversation_id] = {
                "folders": [folder for folder in folders if folder],
                "title": values.get("title"),
                "parent": _kiro_str(values.get("parent_conversation_id")),
            }
    return summaries


def _load_antigravity_prompt_workspaces() -> dict[str, list[str]]:
    """Read the workspace names recorded by history.jsonl and the resume cache."""
    workspaces: dict[str, dict[str, None]] = {}
    try:
        for entry in _iter_jsonl(ANTIGRAVITY_DIR / "history.jsonl"):
            if not isinstance(entry, dict):
                continue
            conversation_id = _kiro_str(entry.get("conversationId"))
            workspace = _kiro_str(entry.get("workspace"))
            if conversation_id and workspace:
                workspaces.setdefault(conversation_id, {})[workspace] = None
    except FileNotFoundError:
        pass
    try:
        with (ANTIGRAVITY_DIR / "cache" / "last_conversations.json").open(encoding="utf-8") as f:
            newest = json.load(f)
    except FileNotFoundError:
        newest = {}
    if not isinstance(newest, dict):
        raise ValueError("invalid Antigravity resume cache")
    for workspace, conversation_id in newest.items():
        workspace = _kiro_str(workspace)
        if isinstance(conversation_id, str) and workspace:
            workspaces.setdefault(conversation_id, {})[workspace] = None
    return {sid: list(folders) for sid, folders in workspaces.items()}


def _antigravity_system_message(content: str) -> tuple[str, str | None, str]:
    block = _ANTIGRAVITY_SYSTEM_MESSAGE_RE.search(content)
    text = (block.group(1) if block else content).strip()
    sent = _ANTIGRAVITY_MESSAGE_RE.match(text)
    if sent:
        return text, sent.group(1), sent.group(2).strip()
    return text, None, ""


def _antigravity_assignment(records: Iterable[Any]) -> tuple[str, str] | None:
    """Find the opening assignment in a promptless child's possibly out-of-order steps."""
    first_step = None
    assignment = None
    step = 0
    for record in records:
        if not isinstance(record, dict):
            continue
        kind = record.get("type")
        if kind == "USER_INPUT":
            # A user conversation can also receive reports from its children.
            return None
        index = record.get("step_index")
        if isinstance(index, int) and not isinstance(index, bool):
            step = index
        if first_step is not None and step >= first_step:
            continue
        if kind == "PLANNER_RESPONSE":
            # A message received after work started is not the opening assignment.
            first_step, assignment = step, None
        elif kind == "SYSTEM_MESSAGE" and record.get("source") != "SYSTEM_SDK":
            content = record.get("content")
            if isinstance(content, str):
                _, sender, body = _antigravity_system_message(content)
                if sender and _UUID_RE.fullmatch(sender):
                    first_step, assignment = step, (sender, body)
    return assignment


def _antigravity_folders(
    conversation_id: str | None,
    summaries: dict[str, dict[str, Any]],
    workspaces: dict[str, list[str]],
) -> list[str]:
    # A subagent can have its own worktree while still belonging to its parent's project.
    seen: set[str] = set()
    folders: list[str] = []
    while conversation_id and conversation_id not in seen:
        seen.add(conversation_id)
        summary = summaries.get(conversation_id, {})
        folders.extend(summary.get("folders", []))
        folders.extend(workspaces.get(conversation_id, []))
        conversation_id = summary.get("parent") or summary.get("inferred_parent")
    return list(dict.fromkeys(folders)) or [UNKNOWN_ANTIGRAVITY_CWD]


def _antigravity_transcript(conversation_dir: Path) -> tuple[Path, int] | None:
    """Return the step log to read for a conversation, and its size."""
    for name in _ANTIGRAVITY_TRANSCRIPT_NAMES:
        path = conversation_dir / ".system_generated" / "logs" / name
        size = _kiro_file_size(path)
        if size:
            return path, size
    return None


def _get_antigravity_project_index(refresh: bool = False) -> dict[str, list[dict[str, Any]]]:
    global _ANTIGRAVITY_PROJECT_INDEX
    if refresh or not _ANTIGRAVITY_PROJECT_INDEX:
        _ANTIGRAVITY_PROJECT_INDEX = _build_antigravity_project_index()
    return _ANTIGRAVITY_PROJECT_INDEX


def _build_antigravity_project_index() -> dict[str, list[dict[str, Any]]]:
    """Group every conversation that has a step log by working directory."""
    index: dict[str, list[dict[str, Any]]] = {}
    try:
        conversation_dirs = sorted(
            p for p in (ANTIGRAVITY_DIR / "brain").iterdir() if p.is_dir()
        )
    except OSError:
        return index
    transcripts: dict[str, tuple[Path, int]] = {}
    try:
        summaries = _load_antigravity_summaries()
        workspaces = _load_antigravity_prompt_workspaces()
        for conversation_dir in conversation_dirs:
            transcript = _antigravity_transcript(conversation_dir)
            if transcript is None:
                continue
            conversation_id = conversation_dir.name
            transcripts[conversation_id] = transcript
            summary = summaries.setdefault(conversation_id, {})
            if not summary.get("parent"):
                assignment = _antigravity_assignment(_iter_jsonl(transcript[0]))
                if assignment:
                    summary["inferred_parent"] = assignment[0]
    except (OSError, sqlite3.Error, ValueError):
        # Partial ownership data could move an excluded trace into an allowed
        # project. Defer this source and leave its previously indexed rows intact.
        logger.warning("Deferring Antigravity CLI discovery: cannot read project metadata")
        return index

    for conversation_id, (path, size) in transcripts.items():
        summary = summaries[conversation_id]
        folders = _antigravity_folders(conversation_id, summaries, workspaces)
        memberships = list(dict.fromkeys(map(_build_antigravity_project_name, folders)))
        index.setdefault(folders[0], []).append({
            "id": conversation_id,
            "path": path,
            "size": size,
            "title": summary.get("title"),
            "parent": summary.get("parent"),
            "inferred_parent": summary.get("inferred_parent"),
            "project_memberships": memberships,
        })
    return index


def _discover_antigravity_projects() -> list[dict]:
    index = _get_antigravity_project_index(refresh=True)
    projects = []
    for cwd, refs in sorted(index.items()):
        project = {
            "dir_name": cwd,
            "display_name": _build_antigravity_project_name(cwd),
            "session_count": len(refs),
            "total_size_bytes": sum(ref["size"] for ref in refs),
            "source": ANTIGRAVITY_SOURCE,
        }
        additional = {name for ref in refs for name in ref["project_memberships"][1:]}
        if additional:
            project["additional_projects"] = sorted(additional)
        projects.append(project)
    return projects


def _parse_antigravity_project_sessions(
    project_dir_name: str,
    anonymizer: Anonymizer,
    include_thinking: bool,
) -> list[dict]:
    sessions = []
    for ref in _get_antigravity_project_index().get(project_dir_name, []):
        parsed = _parse_antigravity_transcript(ref, anonymizer, include_thinking)
        if not parsed or not parsed["messages"]:
            continue
        parsed["project"] = _build_antigravity_project_name(project_dir_name)
        parsed["project_memberships"] = ref["project_memberships"]
        parsed["source"] = ANTIGRAVITY_SOURCE
        parsed["raw_source_path"] = str(ref["path"])
        parsed["client_origin"] = "cli"
        sessions.append(parsed)
    return sessions


def _antigravity_prompt(content: str) -> str:
    """Return the prompt the user sent, without the blocks the CLI adds to it."""
    request = _ANTIGRAVITY_USER_REQUEST_RE.search(content)
    if request:
        return request.group(1).strip()
    return _ANTIGRAVITY_INJECTED_BLOCK_RE.sub("", content).strip()


def _antigravity_tool_input(args: Any, anonymizer: Anonymizer) -> dict:
    """Anonymize a tool call's arguments, keeping every field.

    Antigravity names arguments in PascalCase (CommandLine, TargetFile, Cwd),
    which the field names _parse_tool_input knows do not cover.
    """
    args = _json_loads_maybe(args)
    if args is None:
        return {}
    if not isinstance(args, dict):
        return {"raw": anonymizer.text(str(args))}
    result: dict[str, Any] = {}
    for key, value in args.items():
        if key == "CommandLine" and isinstance(value, str):
            redacted, _, _ = redact_text(value)
            result[key] = anonymizer.text(redacted)
        else:
            result[key] = _anonymize_claude_science_value(value, anonymizer, str(key))
    return result


def _antigravity_result_status(record: dict[str, Any], content: str) -> str | None:
    status = record.get("status")
    if status == "RUNNING":
        # Sent to the background. Its end arrives later as a system message.
        return None
    if status == "ERROR" or record.get("error"):
        return "error"
    # A command that exits nonzero is still DONE; only its exit code says so.
    exit_code = record.get("exit_code")
    if isinstance(exit_code, bool) or not isinstance(exit_code, int):
        reported = _ANTIGRAVITY_EXIT_CODE_RE.match(content)
        exit_code = int(reported.group(1)) if reported else 0
    return "error" if exit_code else "success"


def _parse_antigravity_transcript(
    ref: dict[str, Any], anonymizer: Anonymizer, include_thinking: bool
) -> dict | None:
    """Parse one conversation's step log into the shared session shape."""
    records: list[tuple[int, int, dict[str, Any]]] = []
    try:
        for position, record in enumerate(_iter_jsonl(ref["path"])):
            if not isinstance(record, dict):
                continue
            step = record.get("step_index")
            if isinstance(step, bool) or not isinstance(step, int):
                # No index: keep the line right after the one before it.
                step = records[-1][0] if records else 0
            records.append((step, position, record))
    except OSError:
        return None
    # Steps are appended as they settle, which is not always in step order.
    records.sort(key=lambda item: item[:2])

    messages: list[dict[str, Any]] = []
    metadata: dict[str, Any] = {
        "session_id": ref["id"],
        "cwd": None,
        "git_branch": None,
        "model": None,
        "model_effort": None,
        "start_time": None,
        "end_time": None,
        "segment_title": _kiro_title(ref.get("title"), anonymizer),
    }
    stats = _make_stats()
    # Results carry no call ID. The nth call of the response at step N is
    # answered by the step at N + n, so calls are kept by that step.
    calls_by_result_step: dict[int, dict[str, Any]] = {}
    assignment = _antigravity_assignment(record for _, _, record in records)
    parent = ref.get("parent")
    if not parent:
        parent = assignment[0] if assignment else None
        if parent != ref.get("inferred_parent"):
            # The file changed after discovery. Resolve ownership on the next scan.
            return None
    if assignment and assignment[1] and metadata["segment_title"] is None:
        metadata["segment_title"] = _kiro_title(assignment[1].split("\n", 1)[0], anonymizer)

    for step, _, record in records:
        kind = record.get("type")
        timestamp = _kiro_timestamp(record.get("created_at"))
        content = record.get("content")
        content = content if isinstance(content, str) else ""

        if kind == "USER_INPUT":
            if metadata["model"] is None:
                # The model is only named in the settings change that the CLI
                # appends to a conversation's first prompt.
                model = _ANTIGRAVITY_MODEL_RE.search(content)
                if model:
                    metadata["model"] = model.group(1).strip()
            prompt = _antigravity_prompt(content)
            if prompt:
                messages.append(
                    {"role": "user", "content": anonymizer.text(prompt), "timestamp": timestamp}
                )
                stats["user_messages"] += 1
                _update_time_bounds(metadata, timestamp)

        elif kind == "PLANNER_RESPONSE":
            message: dict[str, Any] = {"role": "assistant", "timestamp": timestamp}
            text = _kiro_str(content)
            if text:
                message["content"] = anonymizer.text(text)
            thinking = _kiro_str(record.get("thinking")) if include_thinking else None
            if thinking:
                message["thinking"] = anonymizer.text(thinking)
            tool_uses: list[dict[str, Any]] = []
            calls = _json_loads_maybe(record.get("tool_calls"))
            for offset, call in enumerate(calls if isinstance(calls, list) else [], start=1):
                name = _kiro_str(call.get("name")) if isinstance(call, dict) else None
                if name is None:
                    continue
                tool_use = {
                    "tool": name,
                    "input": _antigravity_tool_input(call.get("args"), anonymizer),
                }
                tool_uses.append(tool_use)
                calls_by_result_step[step + offset] = tool_use
            if tool_uses:
                message["tool_uses"] = tool_uses
            # input_tokens excludes what cache_read_tokens counts.
            stats["input_tokens"] += _safe_int(record.get("input_tokens"))
            stats["output_tokens"] += _safe_int(record.get("output_tokens"))
            stats["cache_read_tokens"] += _safe_int(record.get("cache_read_tokens"))
            if len(message) > 2:
                messages.append(message)
                stats["assistant_messages"] += 1
                stats["tool_uses"] += len(tool_uses)
                _update_time_bounds(metadata, timestamp)

        elif kind == "SYSTEM_MESSAGE":
            # Text the CLI hands the model in the user's turn: a background
            # task's result, or a message from another conversation (a
            # subagent's report, or the task its parent gave it).
            text, sender, body = _antigravity_system_message(content)
            # The CLI's own reminders and notices are not part of the
            # conversation. It adds one each time a conversation is resumed.
            if record.get("source") == "SYSTEM_SDK" or (
                sender == "system" and body.startswith("[Notice]")
            ):
                continue
            if not text:
                continue
            messages.append(
                {"role": "user", "content": anonymizer.text(text), "timestamp": timestamp}
            )
            stats["user_messages"] += 1
            _update_time_bounds(metadata, timestamp)

        elif record.get("source") == "MODEL":
            # Any other step the model produced is a tool's result.
            tool_use = calls_by_result_step.get(step)
            if tool_use is None:
                continue
            text = content.strip() or _kiro_str(record.get("error")) or ""
            tool_use["output"] = {"text": anonymizer.text(text)} if text else {}
            status = _antigravity_result_status(record, content)
            if status:
                tool_use["status"] = status
            _update_time_bounds(metadata, timestamp)

        # CHECKPOINT (a compaction summary of steps already in the log),
        # CONVERSATION_HISTORY and ERROR_MESSAGE add nothing to the conversation.

    if metadata["model"] is None:
        metadata["model"] = "antigravity-unknown"

    result = _make_session_result(metadata, messages, stats)
    if result is not None and parent and parent != ref["id"]:
        result["parent_session_id"] = parent
    return result
