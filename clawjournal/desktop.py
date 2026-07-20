"""One-click desktop launcher with a locally rendered reminder icon.

The shortcut starts the workbench, whose daemon performs an immediate
background scan.  When the workbench is already running, the launcher asks it
to scan again and opens the existing instance instead.

No icon assets are downloaded.  Eleven expressions (Day 0 through Day 10) are
rendered with the Python standard library and a user-level daily task points
the shortcut at the appropriate one.
"""

from __future__ import annotations

import ctypes
import datetime as dt
import json
import math
import os
import plistlib
import shlex
import shutil
import struct
import subprocess
import sys
import urllib.error
import urllib.request
import webbrowser
import zlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import config as _config
from .config import load_config
from .paths import atomic_write_text, ensure_api_token


# Every path below is resolved on each call rather than bound at import time.
# Binding `CONFIG_DIR` into module constants would pin the real ~/.clawjournal
# even when a caller (notably the autouse `tmp_config` test fixture) has
# redirected `clawjournal.config.CONFIG_DIR` elsewhere — which is how a test
# run would end up rewriting the user's actual shortcut.
def _config_dir() -> Path:
    return _config.CONFIG_DIR


def _state_dir() -> Path:
    return _config_dir() / "desktop"


def _icons_dir() -> Path:
    return _state_dir() / "icons"


def _state_file() -> Path:
    return _state_dir() / "install.json"


def _last_opened_file() -> Path:
    return _state_dir() / "last_opened"


def _log_file() -> Path:
    return _state_dir() / "launcher.log"


def _windows_bootstrap() -> Path:
    return _state_dir() / "windows-launch.py"


SHORTCUT_NAME = "ClawJournal"
LINUX_MANAGED_MARKER = "X-ClawJournal-Managed=true"
WINDOWS_TASK_NAME = "ClawJournal Desktop Icon"
MACOS_LAUNCH_AGENT = "ai.rayward.clawjournal.desktop-icon"
LINUX_SYSTEMD_UNIT = "clawjournal-desktop-icon"

MAX_SAD_DAYS = 10
ICON_SIZE = 256

# The shortcut redirects the daemon's stdout/stderr here, and the daemon logs
# every HTTP request, so the log needs a ceiling to stay bounded over months.
LOG_MAX_BYTES = 1_000_000


class DesktopError(RuntimeError):
    """A user-facing desktop integration error."""


def _now() -> dt.datetime:
    return dt.datetime.now().astimezone()


def _home() -> Path:
    return Path.home()


def _platform() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    if sys.platform.startswith("linux"):
        return "linux"
    return sys.platform


def _command(*args: str) -> list[str]:
    """Use this interpreter so editable, venv, and wheel installs all work."""
    return [sys.executable, "-m", "clawjournal.cli", *args]


def _windows_background_python() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return str(pythonw) if pythonw.exists() else sys.executable


def _write_bytes(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_bytes(payload)
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _read_state() -> dict[str, Any] | None:
    try:
        value = json.loads(_state_file().read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(state: dict[str, Any]) -> None:
    atomic_write_text(_state_file(), json.dumps(state, indent=2) + "\n", parents=True)


def _write_last_opened(when: dt.datetime | None = None) -> None:
    stamp = (when or _now()).isoformat(timespec="seconds")
    atomic_write_text(_last_opened_file(), stamp + "\n", parents=True)


def _trim_log() -> None:
    """Keep the most recent half of an oversized launcher log.

    Truncating in place rather than rotating matters: the POSIX launchers hand
    the daemon an O_APPEND descriptor, which keeps writing to the open inode.
    """
    try:
        if _log_file().stat().st_size <= LOG_MAX_BYTES:
            return
        with _log_file().open("rb") as handle:
            handle.seek(-(LOG_MAX_BYTES // 2), os.SEEK_END)
            handle.readline()  # discard the partial line at the seek point
            tail = handle.read()
        with _log_file().open("wb") as handle:
            handle.write(tail)
    except OSError:
        pass


def _frontend_available() -> bool:
    return (Path(__file__).resolve().parent / "web" / "frontend" / "dist" / "index.html").is_file()


def _read_last_opened() -> dt.datetime | None:
    try:
        opened = dt.datetime.fromisoformat(_last_opened_file().read_text(encoding="utf-8").strip())
    except (FileNotFoundError, OSError, ValueError):
        return None
    return opened if opened.tzinfo else opened.astimezone()


def days_since_last_opened(now: dt.datetime | None = None) -> int:
    """Return a local-calendar day count, clamped to the icon range."""
    current = now or _now()
    if current.tzinfo is None:
        current = current.astimezone()
    opened = _read_last_opened()
    if opened is None:
        return 0
    elapsed = (current.date() - opened.astimezone(current.tzinfo).date()).days
    return max(0, min(MAX_SAD_DAYS, elapsed))


def opened_today(now: dt.datetime | None = None) -> bool:
    """Whether an open is already recorded for the current local calendar day."""
    current = now or _now()
    if current.tzinfo is None:
        current = current.astimezone()
    opened = _read_last_opened()
    return opened is not None and opened.astimezone(current.tzinfo).date() == current.date()


class _Canvas:
    """Tiny supersampled RGBA rasterizer used to avoid an image dependency."""

    def __init__(self, size: int, scale: int = 2) -> None:
        self.size = size
        self.scale = scale
        self.width = size * scale
        self.pixels = bytearray(self.width * self.width * 4)

    def _blend(self, x: int, y: int, color: tuple[int, int, int, int]) -> None:
        if x < 0 or y < 0 or x >= self.width or y >= self.width:
            return
        i = (y * self.width + x) * 4
        alpha = color[3] / 255.0
        inverse = 1.0 - alpha
        self.pixels[i] = round(color[0] * alpha + self.pixels[i] * inverse)
        self.pixels[i + 1] = round(color[1] * alpha + self.pixels[i + 1] * inverse)
        self.pixels[i + 2] = round(color[2] * alpha + self.pixels[i + 2] * inverse)
        self.pixels[i + 3] = round(255 * (alpha + (self.pixels[i + 3] / 255.0) * inverse))

    def ellipse(
        self,
        cx: float,
        cy: float,
        rx: float,
        ry: float,
        color: tuple[int, int, int, int],
    ) -> None:
        s = self.scale
        x0, x1 = int((cx - rx) * s), int((cx + rx) * s) + 1
        y0, y1 = int((cy - ry) * s), int((cy + ry) * s) + 1
        for y in range(y0, y1):
            dy = ((y + 0.5) / s - cy) / ry
            if abs(dy) > 1:
                continue
            span = rx * math.sqrt(max(0.0, 1.0 - dy * dy))
            left, right = int((cx - span) * s), int((cx + span) * s) + 1
            for x in range(max(x0, left), min(x1, right)):
                self._blend(x, y, color)

    def line(
        self,
        points: list[tuple[float, float]],
        width: float,
        color: tuple[int, int, int, int],
    ) -> None:
        if len(points) < 2:
            return
        for start, end in zip(points, points[1:]):
            dx, dy = end[0] - start[0], end[1] - start[1]
            # Adjacent curve samples are already close together.  Spacing the
            # round stamps by roughly a quarter stroke width keeps joins solid
            # without doing hundreds of redundant ellipse fills.
            steps = max(1, int(max(abs(dx), abs(dy)) * 4 / max(width, 1)))
            for n in range(steps + 1):
                t = n / steps
                self.ellipse(
                    start[0] + dx * t,
                    start[1] + dy * t,
                    width / 2,
                    width / 2,
                    color,
                )

    def quadratic(
        self,
        start: tuple[float, float],
        control: tuple[float, float],
        end: tuple[float, float],
        width: float,
        color: tuple[int, int, int, int],
    ) -> None:
        points: list[tuple[float, float]] = []
        for n in range(49):
            t = n / 48
            inv = 1 - t
            points.append((
                inv * inv * start[0] + 2 * inv * t * control[0] + t * t * end[0],
                inv * inv * start[1] + 2 * inv * t * control[1] + t * t * end[1],
            ))
        self.line(points, width, color)

    def png(self) -> bytes:
        s, out_size = self.scale, self.size
        reduced = bytearray(out_size * out_size * 4)
        for y in range(out_size):
            for x in range(out_size):
                dst = (y * out_size + x) * 4
                if s == 2:
                    top = ((y * 2) * self.width + x * 2) * 4
                    bottom = top + self.width * 4
                    for channel in range(4):
                        reduced[dst + channel] = (
                            self.pixels[top + channel]
                            + self.pixels[top + 4 + channel]
                            + self.pixels[bottom + channel]
                            + self.pixels[bottom + 4 + channel]
                        ) // 4
                else:
                    samples = s * s
                    for channel in range(4):
                        total = 0
                        for sy in range(s):
                            for sx in range(s):
                                src = (((y * s + sy) * self.width) + x * s + sx) * 4
                                total += self.pixels[src + channel]
                        reduced[dst + channel] = total // samples

        rows = b"".join(
            b"\x00" + bytes(reduced[y * out_size * 4:(y + 1) * out_size * 4])
            for y in range(out_size)
        )

        def chunk(kind: bytes, data: bytes) -> bytes:
            return (
                struct.pack(">I", len(data)) + kind + data
                + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)
            )

        header = struct.pack(">IIBBBBB", out_size, out_size, 8, 6, 0, 0, 0)
        return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(
            b"IDAT", zlib.compress(rows, 9)
        ) + chunk(b"IEND", b"")


def render_face_png(day: int, *, size: int = ICON_SIZE) -> bytes:
    """Render the Day 0 smile through the Day 10 crying face."""
    day = max(0, min(MAX_SAD_DAYS, int(day)))
    mood = day / MAX_SAD_DAYS
    c = _Canvas(size)
    ratio = size / ICON_SIZE

    # Soft shadow and warm face disk.
    c.ellipse(128 * ratio, 134 * ratio, 103 * ratio, 103 * ratio, (79, 53, 18, 55))
    c.ellipse(128 * ratio, 126 * ratio, 104 * ratio, 104 * ratio, (181, 116, 15, 255))
    c.ellipse(128 * ratio, 123 * ratio, 98 * ratio, 98 * ratio, (255, 202, 40, 255))
    c.ellipse(101 * ratio, 80 * ratio, 48 * ratio, 35 * ratio, (255, 235, 121, 85))

    ink = (77, 48, 24, 255)
    brow_y = 78 + 10 * mood
    brow_slant = 14 * mood
    c.line(
        [((70) * ratio, (brow_y + brow_slant / 2) * ratio),
         ((105) * ratio, (brow_y - brow_slant / 2) * ratio)],
        7 * ratio,
        ink,
    )
    c.line(
        [((151) * ratio, (brow_y - brow_slant / 2) * ratio),
         ((186) * ratio, (brow_y + brow_slant / 2) * ratio)],
        7 * ratio,
        ink,
    )

    if day <= 2:
        # Joyful squinting eyes.
        c.quadratic((70 * ratio, 111 * ratio), (88 * ratio, 92 * ratio),
                    (106 * ratio, 111 * ratio), 9 * ratio, ink)
        c.quadratic((150 * ratio, 111 * ratio), (168 * ratio, 92 * ratio),
                    (186 * ratio, 111 * ratio), 9 * ratio, ink)
    else:
        eye_ry = (10 + 4 * mood) * ratio
        c.ellipse(88 * ratio, 109 * ratio, 8 * ratio, eye_ry, ink)
        c.ellipse(168 * ratio, 109 * ratio, 8 * ratio, eye_ry, ink)
        c.ellipse(85 * ratio, 104 * ratio, 2.2 * ratio, 3 * ratio, (255, 255, 255, 210))
        c.ellipse(165 * ratio, 104 * ratio, 2.2 * ratio, 3 * ratio, (255, 255, 255, 210))

    if day < 8:
        # The smile flattens and then begins to turn down.
        center_y = 198 - 7.2 * day
        c.quadratic((72 * ratio, 155 * ratio), (128 * ratio, center_y * ratio),
                    (184 * ratio, 155 * ratio), 11 * ratio, ink)
        if day <= 3:
            # A light mouth highlight makes Day 0 read as a big grin at icon size.
            c.quadratic((85 * ratio, 165 * ratio), (128 * ratio, (184 - 4 * day) * ratio),
                        (171 * ratio, 165 * ratio), 4 * ratio, (255, 238, 190, 210))
    else:
        # Open, increasingly dramatic wail.
        mouth_ry = (12 + (day - 8) * 8) * ratio
        c.ellipse(128 * ratio, 169 * ratio, (28 + (day - 8) * 4) * ratio, mouth_ry, ink)
        c.ellipse(128 * ratio, (176 + (day - 8) * 3) * ratio,
                  (16 + (day - 8) * 3) * ratio, 6 * ratio, (201, 65, 70, 255))

    if day >= 7:
        tear = (75, 178, 255, 230)
        length = 10 + (day - 7) * 10
        for x in (88, 168):
            c.ellipse(x * ratio, (125 + length / 2) * ratio, (5 + mood * 3) * ratio,
                      (length / 2) * ratio, tear)
            c.ellipse(x * ratio, (125 + length) * ratio, (8 + mood * 3) * ratio,
                      (7 + mood * 3) * ratio, tear)

    return c.png()


def _ico_from_png(png: bytes) -> bytes:
    header = struct.pack("<HHH", 0, 1, 1)
    entry = struct.pack("<BBBBHHII", 0, 0, 0, 0, 1, 32, len(png), 22)
    return header + entry + png


def _icns_from_png(png: bytes) -> bytes:
    element = b"ic08" + struct.pack(">I", len(png) + 8) + png
    return b"icns" + struct.pack(">I", len(element) + 8) + element


def render_icon(day: int) -> None:
    day = max(0, min(MAX_SAD_DAYS, int(day)))
    _icons_dir().mkdir(parents=True, exist_ok=True)
    png = render_face_png(day)
    _write_bytes(_icons_dir() / f"clawjournal-day-{day}.png", png)
    _write_bytes(_icons_dir() / f"clawjournal-day-{day}.ico", _ico_from_png(png))
    _write_bytes(_icons_dir() / f"clawjournal-day-{day}.icns", _icns_from_png(png))


def render_icon_set() -> None:
    """Render all variants, primarily useful for previews and packaging."""
    for day in range(MAX_SAD_DAYS + 1):
        render_icon(day)


def _powershell_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_quiet(command: list[str]) -> subprocess.CompletedProcess[str]:
    kwargs: dict[str, Any] = {"capture_output": True, "text": True, "check": False}
    if os.name == "nt":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.run(command, **kwargs)
    except OSError as exc:
        return subprocess.CompletedProcess(command, 127, "", str(exc))


def _windows_desktop_dir() -> Path:
    # FOLDERID_Desktop respects OneDrive and other Explorer redirections.
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", wintypes.DWORD),
            ("Data2", wintypes.WORD),
            ("Data3", wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    desktop = GUID(
        0xB4BFCC3A, 0xDB2C, 0x424C,
        (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41),
    )
    path_ptr = ctypes.c_wchar_p()
    try:
        result = ctypes.windll.shell32.SHGetKnownFolderPath(
            ctypes.byref(desktop), 0, None, ctypes.byref(path_ptr)
        )
        if result == 0 and path_ptr.value:
            return Path(path_ptr.value)
    finally:
        if path_ptr:
            ctypes.windll.ole32.CoTaskMemFree(path_ptr)
    return _home() / "Desktop"


def _desktop_dir(platform_name: str) -> Path:
    if platform_name == "windows":
        return _windows_desktop_dir()
    if platform_name == "linux" and shutil.which("xdg-user-dir"):
        result = _run_quiet(["xdg-user-dir", "DESKTOP"])
        if result.returncode == 0 and result.stdout.strip():
            return Path(result.stdout.strip())
    return _home() / "Desktop"


def _write_windows_shortcut(path: Path, icon: Path) -> None:
    _write_windows_bootstrap()
    args = subprocess.list2cmdline([str(_windows_bootstrap()), "desktop", "launch"])
    target = _windows_background_python()
    script = "; ".join([
        "$w = New-Object -ComObject WScript.Shell",
        f"$s = $w.CreateShortcut({_powershell_quote(path)})",
        f"$s.TargetPath = {_powershell_quote(target)}",
        f"$s.Arguments = {_powershell_quote(args)}",
        f"$s.WorkingDirectory = {_powershell_quote(_state_dir())}",
        f"$s.IconLocation = {_powershell_quote(str(icon) + ',0')}",
        "$s.Description = 'Scan sessions and open the local ClawJournal workbench'",
        "$s.WindowStyle = 7",
        "$s.Save()",
    ])
    powershell = shutil.which("powershell.exe") or shutil.which("powershell")
    if not powershell:
        raise DesktopError("Windows PowerShell is required to create the shortcut.")
    result = _run_quiet([powershell, "-NoProfile", "-NonInteractive", "-Command", script])
    if result.returncode != 0:
        detail = result.stderr.strip() or "PowerShell could not create the .lnk file"
        raise DesktopError(detail)


def _write_windows_bootstrap() -> None:
    """Write a no-console entry point that captures even early import errors."""
    content = "\n".join([
        '"""Generated ClawJournal desktop entry point."""',
        "import os",
        "import subprocess",
        "from pathlib import Path",
        "import sys",
        "import traceback",
        "",
        "# pythonw has no console to inherit. Force console-mode children such",
        "# as Scoop shims to remain invisible instead of flashing a new window.",
        "if os.name == 'nt':",
        "    _original_popen = subprocess.Popen",
        "    _create_no_window = getattr(subprocess, 'CREATE_NO_WINDOW', 0x08000000)",
        "    _detached_process = 0x00000008",
        "",
        "    class _NoConsolePopen(_original_popen):",
        "        def __init__(self, *args, **kwargs):",
        "            flags = int(kwargs.get('creationflags', 0))",
        "            if not flags & _detached_process:",
        "                kwargs['creationflags'] = flags | _create_no_window",
        "            super().__init__(*args, **kwargs)",
        "",
        "    subprocess.Popen = _NoConsolePopen",
        "",
        f"log_path = Path({str(_log_file())!r})",
        "log_path.parent.mkdir(parents=True, exist_ok=True)",
        'with log_path.open("a", encoding="utf-8", buffering=1) as log_stream:',
        "    sys.stdout = log_stream",
        "    sys.stderr = log_stream",
        "    try:",
        "        from clawjournal.cli import main",
        "        main()",
        "    except BaseException:",
        "        traceback.print_exc()",
        "        raise",
        "",
    ])
    atomic_write_text(_windows_bootstrap(), content, parents=True)


def _notify_windows_shortcut(path: Path) -> None:
    try:
        ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, str(path), None)
    except (AttributeError, OSError):
        pass


def _shell_launcher(command: list[str], *, log: bool) -> str:
    rendered = " ".join(shlex.quote(part) for part in command)
    suffix = f" >> {shlex.quote(str(_log_file()))} 2>&1" if log else ""
    return f"#!/bin/sh\ncd {shlex.quote(str(_state_dir()))}\nexec {rendered}{suffix}\n"


def _write_executable(path: Path, content: str) -> None:
    atomic_write_text(path, content, parents=True)
    path.chmod(0o755)


def _linux_desktop_content(launcher: Path, icon: Path) -> str:
    return "\n".join([
        "[Desktop Entry]",
        "Type=Application",
        "Version=1.0",
        f"Name={SHORTCUT_NAME}",
        "Comment=Scan sessions and open the local ClawJournal workbench",
        f"Exec={_desktop_exec_quote(launcher)}",
        f"Icon={icon}",
        "Terminal=false",
        "StartupNotify=true",
        "Categories=Utility;Development;",
        LINUX_MANAGED_MARKER,
        "",
    ])


def _desktop_exec_quote(path: Path) -> str:
    """Quote one executable path using the Desktop Entry Exec grammar."""
    value = str(path)
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return f'"{value}"'


def _linux_entry_is_managed(path: Path) -> bool:
    try:
        return LINUX_MANAGED_MARKER in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _macos_app_is_managed(path: Path) -> bool:
    try:
        info = plistlib.loads((path / "Contents" / "Info.plist").read_bytes())
    except (FileNotFoundError, OSError, plistlib.InvalidFileException):
        return False
    return info.get("CFBundleIdentifier") == MACOS_LAUNCH_AGENT


def _windows_shortcut_is_managed(path: Path) -> bool:
    """A .lnk is ours when it still points at one of our own entry points.

    .lnk is a binary format with no comment field, but the target and argument
    strings are stored verbatim, so scan the raw bytes for our entry-point
    names in both the ASCII and UTF-16LE encodings the format mixes.
    """
    try:
        raw = path.read_bytes()
    except OSError:
        return False
    return any(
        token.encode("ascii") in raw or token.encode("utf-16-le") in raw
        for token in (_windows_bootstrap().name, "clawjournal.cli")
    )


def _ensure_available(path: Path, is_managed: Callable[[Path], bool]) -> None:
    """Refuse to clobber anything at the shortcut path we did not create.

    Ownership is proven by the artifact itself, never by the presence of our
    install state: a user can delete our shortcut and put their own file at
    the same path while `install.json` still names it.
    """
    if not path.exists() or is_managed(path):
        return
    raise DesktopError(f"Refusing to replace an existing item: {path}")


def _install_windows(day: int) -> tuple[Path, str, list[str]]:
    shortcut = _desktop_dir("windows") / f"{SHORTCUT_NAME}.lnk"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    _ensure_available(shortcut, _windows_shortcut_is_managed)
    # Writes the bootstrap the shortcut and the daily task both point at.
    _write_windows_shortcut(shortcut, _icons_dir() / f"clawjournal-day-{day}.ico")
    _notify_windows_shortcut(shortcut)

    task_parts = [
        _windows_background_python(), str(_windows_bootstrap()),
        "desktop", "refresh", "--quiet",
    ]
    task_command = subprocess.list2cmdline(task_parts)
    result = _run_quiet([
        "schtasks.exe", "/Create", "/F", "/SC", "DAILY", "/ST", "09:00",
        "/TN", WINDOWS_TASK_NAME, "/TR", task_command,
    ])
    warnings = []
    refresh_mode = "daily task"
    if result.returncode != 0:
        warnings.append("Could not register the daily icon task; the face will still update whenever ClawJournal opens.")
        refresh_mode = "when opened"
    else:
        # schtasks.exe defaults to refusing/terminating tasks on battery power,
        # and to skipping a run outright if the machine was asleep at 09:00.
        # Windows has no login-time fallback like the macOS and Linux paths, so
        # -StartWhenAvailable is what keeps a laptop's icon from going stale.
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        settings_script = "; ".join([
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -StartWhenAvailable "
            "-ExecutionTimeLimit (New-TimeSpan -Minutes 5)",
            f"Set-ScheduledTask -TaskName {_powershell_quote(WINDOWS_TASK_NAME)} "
            "-Settings $settings | Out-Null",
        ])
        settings_result = _run_quiet([
            str(powershell or "powershell.exe"), "-NoProfile", "-NonInteractive",
            "-Command", settings_script,
        ])
        if settings_result.returncode != 0:
            warnings.append(
                "The daily task was installed, but Windows kept its defaults: the icon "
                "may not refresh on battery or after a missed run."
            )
    return shortcut, refresh_mode, warnings


def _install_macos(day: int) -> tuple[Path, str, list[str]]:
    app = _desktop_dir("macos") / f"{SHORTCUT_NAME}.app"
    _ensure_available(app, _macos_app_is_managed)
    info = {
        "CFBundleDisplayName": SHORTCUT_NAME,
        "CFBundleExecutable": "ClawJournal",
        "CFBundleIconFile": "ClawJournal",
        "CFBundleIdentifier": MACOS_LAUNCH_AGENT,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": SHORTCUT_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "10.15",
    }
    # Stage the bundle alongside the target and swap it in only once complete.
    # Info.plist is the sole proof of ownership, and it is written last: a
    # failure partway through an in-place build would leave a bundle that
    # `_macos_app_is_managed` disowns forever, wedging both install and
    # uninstall with no way out but a manual `rm -rf`.
    staging = app.with_name(f".{app.name}.{os.getpid()}.new")
    shutil.rmtree(staging, ignore_errors=True)
    try:
        resources = staging / "Contents" / "Resources"
        resources.mkdir(parents=True, exist_ok=True)
        _write_executable(
            staging / "Contents" / "MacOS" / "ClawJournal",
            _shell_launcher(_command("desktop", "launch"), log=True),
        )
        _write_bytes(
            resources / "ClawJournal.icns",
            (_icons_dir() / f"clawjournal-day-{day}.icns").read_bytes(),
        )
        _write_bytes(staging / "Contents" / "Info.plist", plistlib.dumps(info))
        if app.exists():
            shutil.rmtree(app)
        os.replace(staging, app)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    agent = _home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT}.plist"
    agent.parent.mkdir(parents=True, exist_ok=True)
    plist = {
        "Label": MACOS_LAUNCH_AGENT,
        "ProgramArguments": _command("desktop", "refresh", "--quiet"),
        "StartCalendarInterval": {"Hour": 9, "Minute": 0},
        "RunAtLoad": True,
        "ProcessType": "Background",
    }
    _write_bytes(agent, plistlib.dumps(plist))
    uid = str(os.getuid()) if hasattr(os, "getuid") else ""
    _run_quiet(["launchctl", "bootout", f"gui/{uid}", str(agent)])
    result = _run_quiet(["launchctl", "bootstrap", f"gui/{uid}", str(agent)])
    warnings = []
    refresh_mode = "daily LaunchAgent"
    if result.returncode != 0:
        warnings.append("Could not load the daily LaunchAgent; it is installed and will be retried at next login.")
        refresh_mode = "login/when opened"
    os.utime(app, None)
    return app, refresh_mode, warnings


def _systemd_quote(path: Path) -> str:
    return '"' + str(path).replace("%", "%%").replace("\\", "\\\\").replace('"', '\\"') + '"'


def _install_linux(day: int) -> tuple[Path, str, list[str]]:
    desktop = _desktop_dir("linux") / f"{SHORTCUT_NAME}.desktop"
    desktop.parent.mkdir(parents=True, exist_ok=True)
    _ensure_available(desktop, _linux_entry_is_managed)
    launcher = _state_dir() / "launch"
    refresher = _state_dir() / "refresh"
    _write_executable(launcher, _shell_launcher(_command("desktop", "launch"), log=True))
    _write_executable(refresher, _shell_launcher(_command("desktop", "refresh", "--quiet"), log=False))
    atomic_write_text(
        desktop,
        _linux_desktop_content(launcher, _icons_dir() / f"clawjournal-day-{day}.png"),
        parents=True,
    )
    desktop.chmod(0o755)
    if shutil.which("gio"):
        _run_quiet(["gio", "set", str(desktop), "metadata::trusted", "true"])

    autostart = _home() / ".config" / "autostart" / "clawjournal-desktop-icon.desktop"
    atomic_write_text(
        autostart,
        "\n".join([
            "[Desktop Entry]", "Type=Application", "Name=Refresh ClawJournal icon",
            f"Exec={_desktop_exec_quote(refresher)}", "Terminal=false", "NoDisplay=true",
            "X-GNOME-Autostart-enabled=true", LINUX_MANAGED_MARKER, "",
        ]),
        parents=True,
    )

    unit_dir = _home() / ".config" / "systemd" / "user"
    service = unit_dir / f"{LINUX_SYSTEMD_UNIT}.service"
    timer = unit_dir / f"{LINUX_SYSTEMD_UNIT}.timer"
    atomic_write_text(
        service,
        "\n".join([
            "[Unit]", "Description=Refresh the ClawJournal desktop expression", "",
            "[Service]", "Type=oneshot", f"ExecStart={_systemd_quote(refresher)}", "",
        ]),
        parents=True,
    )
    atomic_write_text(
        timer,
        "\n".join([
            "[Unit]", "Description=Daily ClawJournal desktop expression refresh", "",
            "[Timer]", "OnCalendar=daily", "Persistent=true", "RandomizedDelaySec=15m", "",
            "[Install]", "WantedBy=timers.target", "",
        ]),
        parents=True,
    )
    warnings = []
    refresh_mode = "daily systemd timer"
    if shutil.which("systemctl"):
        _run_quiet(["systemctl", "--user", "daemon-reload"])
        result = _run_quiet([
            "systemctl", "--user", "enable", "--now", f"{LINUX_SYSTEMD_UNIT}.timer"
        ])
        if result.returncode != 0:
            warnings.append("The daily systemd timer could not be enabled; login and launch refreshes remain active.")
            refresh_mode = "login/when opened"
    else:
        refresh_mode = "login/when opened"
    return desktop, refresh_mode, warnings


def install() -> dict[str, Any]:
    platform_name = _platform()
    if platform_name not in {"windows", "macos", "linux"}:
        raise DesktopError(f"Desktop shortcuts are not supported on {platform_name}.")
    if not _frontend_available():
        raise DesktopError(
            "The browser workbench is not built. Re-run the installer with "
            "`--desktop-shortcut` (macOS/Linux) or `-DesktopShortcut` (Windows)."
        )
    previous_state = _read_state()
    if not _last_opened_file().exists():
        _write_last_opened()
    day = days_since_last_opened()
    render_icon(day)
    installers = {
        "windows": _install_windows,
        "macos": _install_macos,
        "linux": _install_linux,
    }
    shortcut, refresh_mode, warnings = installers[platform_name](day)
    _remove_previous_shortcut(previous_state, shortcut, platform_name)
    state = {
        "version": 1,
        "platform": platform_name,
        "shortcut": str(shortcut),
        "refresh_mode": refresh_mode,
        "installed_at": _now().isoformat(timespec="seconds"),
    }
    _write_state(state)
    # No refresh() here: each installer already wrote the Day `day` icon and
    # notified the shell, so refreshing would just rewrite the shortcut again.
    return {**state, "day": day, "warnings": warnings}


def _remove_previous_shortcut(
    previous_state: dict[str, Any] | None,
    current: Path,
    platform_name: str,
) -> None:
    """Remove a renamed shortcut only when our prior state identifies it."""
    if not previous_state or previous_state.get("platform") != platform_name:
        return
    raw_previous = previous_state.get("shortcut")
    if not isinstance(raw_previous, str) or not raw_previous:
        return
    previous = Path(raw_previous)
    same_path = os.path.normcase(os.path.abspath(previous)) == os.path.normcase(os.path.abspath(current))
    same_parent = os.path.normcase(os.path.abspath(previous.parent)) == os.path.normcase(
        os.path.abspath(current.parent)
    )
    if same_path or not same_parent:
        return
    if platform_name == "windows":
        if _windows_shortcut_is_managed(previous):
            _remove_file(previous)
    elif platform_name == "linux":
        if _linux_entry_is_managed(previous):
            _remove_file(previous)
    elif platform_name == "macos":
        if _macos_app_is_managed(previous):
            shutil.rmtree(previous)


def _require_managed(path: Path, is_managed: Callable[[Path], bool], kind: str) -> None:
    """Gate a refresh on the artifact still being present and still being ours.

    Refresh runs unattended — a daily scheduled task, and the daemon's page
    handler — so it must never resurrect a shortcut the user deleted, nor
    overwrite a file they put in its place.
    """
    if not path.exists():
        raise DesktopError(f"Desktop {kind} is missing: {path}")
    _ensure_available(path, is_managed)


def _refresh_windows(shortcut: Path, day: int) -> None:
    _require_managed(shortcut, _windows_shortcut_is_managed, "shortcut")
    _write_windows_shortcut(shortcut, _icons_dir() / f"clawjournal-day-{day}.ico")
    _notify_windows_shortcut(shortcut)


def _refresh_macos(app: Path, day: int) -> None:
    _require_managed(app, _macos_app_is_managed, "app")
    icon = app / "Contents" / "Resources" / "ClawJournal.icns"
    _write_bytes(icon, (_icons_dir() / f"clawjournal-day-{day}.icns").read_bytes())
    os.utime(app, None)


def _refresh_linux(shortcut: Path, day: int) -> None:
    _require_managed(shortcut, _linux_entry_is_managed, "shortcut")
    launcher = _state_dir() / "launch"
    atomic_write_text(
        shortcut,
        _linux_desktop_content(launcher, _icons_dir() / f"clawjournal-day-{day}.png"),
        parents=True,
    )
    shortcut.chmod(0o755)


def refresh(*, quiet: bool = False) -> dict[str, Any]:
    state = _read_state()
    if state is None:
        raise DesktopError("The desktop shortcut is not installed. Run `clawjournal desktop install` first.")
    platform_name = str(state.get("platform", ""))
    shortcut = Path(str(state.get("shortcut", "")))
    day = days_since_last_opened()
    if not all(
        (_icons_dir() / f"clawjournal-day-{day}.{suffix}").exists()
        for suffix in ("png", "ico", "icns")
    ):
        render_icon(day)
    refreshers = {
        "windows": _refresh_windows,
        "macos": _refresh_macos,
        "linux": _refresh_linux,
    }
    try:
        refreshers[platform_name](shortcut, day)
    except KeyError as exc:
        raise DesktopError(f"Unknown desktop integration platform: {platform_name}") from exc
    result = {**state, "day": day, "mood": mood_label(day)}
    if not quiet:
        print(f"ClawJournal icon: Day {day} — {result['mood']}")
    return result


def mood_label(day: int) -> str:
    if day == 0:
        return "big smile"
    if day <= 3:
        return "happy"
    if day <= 6:
        return "missing you"
    if day <= 9:
        return "crying"
    return "dramatic crying"


def note_opened() -> None:
    """Record an open only when the optional desktop integration exists.

    Cheap to call repeatedly: the icon only ever changes on a calendar-day
    boundary, so an open already recorded for today needs no shortcut rewrite.
    That keeps this safe on the daemon's page-serving path.
    """
    if _read_state() is None or opened_today():
        return
    _write_last_opened()
    try:
        refresh(quiet=True)
    except (DesktopError, OSError):
        # Opening the workbench must never fail because Explorer/Finder is busy.
        pass


def _workbench_running(port: int) -> bool:
    """Whether anything already holds the workbench port.

    Deliberately a bare TCP connect, not an API call. This used to request
    `/api/stats` — a real SQL aggregate over the index — with a 0.6s timeout,
    so a daemon that was merely busy scanning read as "not running" and the
    launcher started a second one. Liveness here only needs to answer "is the
    port taken", and `_daemon_port_is_open` already answers exactly that.
    """
    from .cli import _daemon_port_is_open

    return _daemon_port_is_open(port)


def _request_scan(port: int) -> None:
    token = ensure_api_token(_config_dir())
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/scan",
        data=b"",
        method="POST",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        urllib.request.urlopen(request, timeout=1.0).close()
    except (OSError, urllib.error.URLError):
        pass


def launch() -> None:
    """Open the workbench and ensure a scan happens, reusing a live daemon."""
    # pythonw.exe gives Windows a true no-console launcher. Preserve diagnostics
    # by supplying streams before logging and the daemon are initialized.
    _trim_log()
    if sys.stdout is None or sys.stderr is None:
        _log_file().parent.mkdir(parents=True, exist_ok=True)
        log_stream = open(_log_file(), "a", encoding="utf-8", buffering=1)  # noqa: SIM115
        if sys.stdout is None:
            sys.stdout = log_stream
        if sys.stderr is None:
            sys.stderr = log_stream
    note_opened()
    config = load_config()
    port = int(config.get("daemon_port") or 8384)
    url = f"http://localhost:{port}/"
    if _workbench_running(port):
        _request_scan(port)
        webbrowser.open(url)
        return

    from .pricing import ensure_pricing_fresh
    from .workbench.daemon import run_server

    ensure_pricing_fresh()
    try:
        run_server(port=port, open_browser=True, allow_port_fallback=False)
    except OSError:
        # Another click won the race between the probe above and the bind.
        # That process owns the port, so join it instead of quietly starting a
        # duplicate daemon on an ephemeral port.
        _request_scan(port)
        webbrowser.open(url)


def _remove_file(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def uninstall() -> dict[str, Any]:
    state = _read_state()
    if state is None:
        return {"removed": False}
    platform_name = str(state.get("platform", ""))
    shortcut = Path(str(state.get("shortcut", "")))
    if platform_name == "windows":
        _run_quiet(["schtasks.exe", "/Delete", "/F", "/TN", WINDOWS_TASK_NAME])
        if _windows_shortcut_is_managed(shortcut):
            _remove_file(shortcut)
            _notify_windows_shortcut(shortcut)
    elif platform_name == "macos":
        agent = _home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT}.plist"
        uid = str(os.getuid()) if hasattr(os, "getuid") else ""
        _run_quiet(["launchctl", "bootout", f"gui/{uid}", str(agent)])
        _remove_file(agent)
        if _macos_app_is_managed(shortcut):
            shutil.rmtree(shortcut)
    elif platform_name == "linux":
        _run_quiet([
            "systemctl", "--user", "disable", "--now", f"{LINUX_SYSTEMD_UNIT}.timer"
        ])
        for suffix in ("service", "timer"):
            _remove_file(_home() / ".config" / "systemd" / "user" / f"{LINUX_SYSTEMD_UNIT}.{suffix}")
        _run_quiet(["systemctl", "--user", "daemon-reload"])
        _remove_file(_home() / ".config" / "autostart" / "clawjournal-desktop-icon.desktop")
        if _linux_entry_is_managed(shortcut):
            _remove_file(shortcut)
    else:
        raise DesktopError(f"Unknown desktop integration platform: {platform_name}")

    # _state_dir() is a fixed child of ~/.clawjournal and contains only artifacts
    # created by this feature.  Resolve and validate before recursive removal.
    resolved = _state_dir().resolve()
    if resolved.parent == _config_dir().resolve() and resolved.name == "desktop":
        shutil.rmtree(resolved, ignore_errors=True)
    return {"removed": True, "shortcut": str(shortcut)}


def status() -> dict[str, Any]:
    state = _read_state()
    if state is None:
        return {"installed": False}
    shortcut = Path(str(state.get("shortcut", "")))
    day = days_since_last_opened()
    return {
        "installed": shortcut.exists(),
        **state,
        "day": day,
        "mood": mood_label(day),
    }


def run_desktop_command(args: Any) -> int:
    """CLI adapter kept here so platform details stay out of cli.py."""
    try:
        if args.desktop_command == "install":
            result = install()
            print(f"[ok] Desktop shortcut installed: {result['shortcut']}")
            print(f"     Icon refresh: {result['refresh_mode']}")
            for warning in result["warnings"]:
                print(f"[!] {warning}")
            return 0
        if args.desktop_command == "uninstall":
            result = uninstall()
            print("[ok] Desktop shortcut removed." if result["removed"] else "Desktop shortcut is not installed.")
            return 0
        if args.desktop_command == "refresh":
            refresh(quiet=args.quiet)
            return 0
        if args.desktop_command == "status":
            print(json.dumps(status(), indent=2))
            return 0
        if args.desktop_command == "launch":
            launch()
            return 0
    except DesktopError as exc:
        print(f"[x] {exc}", file=sys.stderr)
        return 1
    raise DesktopError(f"Unknown desktop command: {args.desktop_command}")
