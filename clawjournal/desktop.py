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
from pathlib import Path
from typing import Any

from .config import CONFIG_DIR, load_config
from .paths import atomic_write_text, ensure_api_token


STATE_DIR = CONFIG_DIR / "desktop"
ICONS_DIR = STATE_DIR / "icons"
STATE_FILE = STATE_DIR / "install.json"
LAST_OPENED_FILE = STATE_DIR / "last_opened"
LOG_FILE = STATE_DIR / "launcher.log"
WINDOWS_BOOTSTRAP = STATE_DIR / "windows-launch.py"

SHORTCUT_NAME = "ClawJournal"
WINDOWS_TASK_NAME = "ClawJournal Desktop Icon"
MACOS_LAUNCH_AGENT = "ai.rayward.clawjournal.desktop-icon"
LINUX_SYSTEMD_UNIT = "clawjournal-desktop-icon"

MAX_SAD_DAYS = 10
ICON_SIZE = 256


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
        value = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _write_state(state: dict[str, Any]) -> None:
    atomic_write_text(STATE_FILE, json.dumps(state, indent=2) + "\n", parents=True)


def _write_last_opened(when: dt.datetime | None = None) -> None:
    stamp = (when or _now()).isoformat(timespec="seconds")
    atomic_write_text(LAST_OPENED_FILE, stamp + "\n", parents=True)


def _frontend_available() -> bool:
    return (Path(__file__).resolve().parent / "web" / "frontend" / "dist" / "index.html").is_file()


def days_since_last_opened(now: dt.datetime | None = None) -> int:
    """Return a local-calendar day count, clamped to the icon range."""
    current = now or _now()
    if current.tzinfo is None:
        current = current.astimezone()
    try:
        opened = dt.datetime.fromisoformat(LAST_OPENED_FILE.read_text(encoding="utf-8").strip())
        if opened.tzinfo is None:
            opened = opened.astimezone()
    except (FileNotFoundError, OSError, ValueError):
        return 0
    elapsed = (current.astimezone().date() - opened.astimezone().date()).days
    return max(0, min(MAX_SAD_DAYS, elapsed))


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
    ICONS_DIR.mkdir(parents=True, exist_ok=True)
    png = render_face_png(day)
    _write_bytes(ICONS_DIR / f"clawjournal-day-{day}.png", png)
    _write_bytes(ICONS_DIR / f"clawjournal-day-{day}.ico", _ico_from_png(png))
    _write_bytes(ICONS_DIR / f"clawjournal-day-{day}.icns", _icns_from_png(png))


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
    args = subprocess.list2cmdline([str(WINDOWS_BOOTSTRAP), "desktop", "launch"])
    target = _windows_background_python()
    script = "; ".join([
        "$w = New-Object -ComObject WScript.Shell",
        f"$s = $w.CreateShortcut({_powershell_quote(path)})",
        f"$s.TargetPath = {_powershell_quote(target)}",
        f"$s.Arguments = {_powershell_quote(args)}",
        f"$s.WorkingDirectory = {_powershell_quote(STATE_DIR)}",
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
        f"log_path = Path({str(LOG_FILE)!r})",
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
    atomic_write_text(WINDOWS_BOOTSTRAP, content, parents=True)


def _notify_windows_shortcut(path: Path) -> None:
    try:
        ctypes.windll.shell32.SHChangeNotify(0x00002000, 0x0005, str(path), None)
    except (AttributeError, OSError):
        pass


def _shell_launcher(command: list[str], *, log: bool) -> str:
    rendered = " ".join(shlex.quote(part) for part in command)
    suffix = f" >> {shlex.quote(str(LOG_FILE))} 2>&1" if log else ""
    return f"#!/bin/sh\ncd {shlex.quote(str(STATE_DIR))}\nexec {rendered}{suffix}\n"


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
        "X-ClawJournal-Managed=true",
        "",
    ])


def _desktop_exec_quote(path: Path) -> str:
    """Quote one executable path using the Desktop Entry Exec grammar."""
    value = str(path)
    for char in ("\\", '"', "`", "$"):
        value = value.replace(char, "\\" + char)
    return f'"{value}"'


def _ensure_available(path: Path, *, managed_marker: str | None = None) -> None:
    if not path.exists() or _read_state() is not None:
        return
    if managed_marker:
        try:
            if managed_marker in path.read_text(encoding="utf-8"):
                return
        except (OSError, UnicodeDecodeError, IsADirectoryError):
            pass
    raise DesktopError(f"Refusing to replace an existing item: {path}")


def _install_windows(day: int) -> tuple[Path, str, list[str]]:
    shortcut = _desktop_dir("windows") / f"{SHORTCUT_NAME}.lnk"
    shortcut.parent.mkdir(parents=True, exist_ok=True)
    _ensure_available(shortcut)
    _write_windows_shortcut(shortcut, ICONS_DIR / f"clawjournal-day-{day}.ico")

    _write_windows_bootstrap()
    task_parts = [
        _windows_background_python(), str(WINDOWS_BOOTSTRAP),
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
        # schtasks.exe defaults to refusing/terminating tasks on battery power.
        # Icon refresh is tiny and should keep working on laptops unplugged.
        powershell = shutil.which("powershell.exe") or shutil.which("powershell")
        settings_script = "; ".join([
            "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
            "-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5)",
            f"Set-ScheduledTask -TaskName {_powershell_quote(WINDOWS_TASK_NAME)} "
            "-Settings $settings | Out-Null",
        ])
        settings_result = _run_quiet([
            str(powershell or "powershell.exe"), "-NoProfile", "-NonInteractive",
            "-Command", settings_script,
        ])
        if settings_result.returncode != 0:
            warnings.append("The daily task was installed, but Windows kept its default battery restriction.")
    return shortcut, refresh_mode, warnings


def _install_macos(day: int) -> tuple[Path, str, list[str]]:
    app = _desktop_dir("macos") / f"{SHORTCUT_NAME}.app"
    info_path = app / "Contents" / "Info.plist"
    if app.exists() and _read_state() is None:
        try:
            owned = plistlib.loads(info_path.read_bytes()).get("CFBundleIdentifier") == MACOS_LAUNCH_AGENT
        except (FileNotFoundError, OSError, plistlib.InvalidFileException):
            owned = False
        if not owned:
            raise DesktopError(f"Refusing to replace an existing item: {app}")
    resources = app / "Contents" / "Resources"
    executable = app / "Contents" / "MacOS" / "ClawJournal"
    resources.mkdir(parents=True, exist_ok=True)
    _write_executable(executable, _shell_launcher(_command("desktop", "launch"), log=True))
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
    _write_bytes(info_path, plistlib.dumps(info))
    _write_bytes(
        resources / "ClawJournal.icns",
        (ICONS_DIR / f"clawjournal-day-{day}.icns").read_bytes(),
    )

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
    _ensure_available(desktop, managed_marker="X-ClawJournal-Managed=true")
    launcher = STATE_DIR / "launch"
    refresher = STATE_DIR / "refresh"
    _write_executable(launcher, _shell_launcher(_command("desktop", "launch"), log=True))
    _write_executable(refresher, _shell_launcher(_command("desktop", "refresh", "--quiet"), log=False))
    atomic_write_text(
        desktop,
        _linux_desktop_content(launcher, ICONS_DIR / f"clawjournal-day-{day}.png"),
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
            "X-GNOME-Autostart-enabled=true", "X-ClawJournal-Managed=true", "",
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
    if not LAST_OPENED_FILE.exists():
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
    refresh(quiet=True)
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
        _remove_file(previous)
    elif platform_name == "linux":
        try:
            if "X-ClawJournal-Managed=true" in previous.read_text(encoding="utf-8"):
                _remove_file(previous)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            pass
    elif platform_name == "macos":
        try:
            info = plistlib.loads((previous / "Contents" / "Info.plist").read_bytes())
            managed = info.get("CFBundleIdentifier") == MACOS_LAUNCH_AGENT
        except (FileNotFoundError, OSError, plistlib.InvalidFileException):
            managed = False
        if managed:
            shutil.rmtree(previous)


def _refresh_windows(shortcut: Path, day: int) -> None:
    _write_windows_shortcut(shortcut, ICONS_DIR / f"clawjournal-day-{day}.ico")
    _notify_windows_shortcut(shortcut)


def _refresh_macos(app: Path, day: int) -> None:
    icon = app / "Contents" / "Resources" / "ClawJournal.icns"
    if not app.exists():
        raise DesktopError(f"Desktop app is missing: {app}")
    _write_bytes(icon, (ICONS_DIR / f"clawjournal-day-{day}.icns").read_bytes())
    os.utime(app, None)


def _refresh_linux(shortcut: Path, day: int) -> None:
    launcher = STATE_DIR / "launch"
    if not shortcut.exists():
        raise DesktopError(f"Desktop shortcut is missing: {shortcut}")
    atomic_write_text(
        shortcut,
        _linux_desktop_content(launcher, ICONS_DIR / f"clawjournal-day-{day}.png"),
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
        (ICONS_DIR / f"clawjournal-day-{day}.{suffix}").exists()
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
    """Record an open only when the optional desktop integration exists."""
    if _read_state() is None:
        return
    _write_last_opened()
    try:
        refresh(quiet=True)
    except (DesktopError, OSError):
        # Opening the workbench must never fail because Explorer/Finder is busy.
        pass


def _workbench_running(port: int) -> bool:
    token = ensure_api_token(CONFIG_DIR)
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/stats",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=0.6) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def _request_scan(port: int) -> None:
    token = ensure_api_token(CONFIG_DIR)
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
    if sys.stdout is None or sys.stderr is None:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        log_stream = open(LOG_FILE, "a", encoding="utf-8", buffering=1)  # noqa: SIM115
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
    run_server(port=port, open_browser=True)


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
        _remove_file(shortcut)
        _notify_windows_shortcut(shortcut)
    elif platform_name == "macos":
        agent = _home() / "Library" / "LaunchAgents" / f"{MACOS_LAUNCH_AGENT}.plist"
        uid = str(os.getuid()) if hasattr(os, "getuid") else ""
        _run_quiet(["launchctl", "bootout", f"gui/{uid}", str(agent)])
        _remove_file(agent)
        info = shortcut / "Contents" / "Info.plist"
        try:
            managed = plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") == MACOS_LAUNCH_AGENT
        except (FileNotFoundError, OSError, plistlib.InvalidFileException):
            managed = False
        if managed:
            shutil.rmtree(shortcut)
    elif platform_name == "linux":
        _run_quiet([
            "systemctl", "--user", "disable", "--now", f"{LINUX_SYSTEMD_UNIT}.timer"
        ])
        for suffix in ("service", "timer"):
            _remove_file(_home() / ".config" / "systemd" / "user" / f"{LINUX_SYSTEMD_UNIT}.{suffix}")
        _run_quiet(["systemctl", "--user", "daemon-reload"])
        _remove_file(_home() / ".config" / "autostart" / "clawjournal-desktop-icon.desktop")
        try:
            if "X-ClawJournal-Managed=true" in shortcut.read_text(encoding="utf-8"):
                _remove_file(shortcut)
        except (FileNotFoundError, OSError, UnicodeDecodeError):
            pass
    else:
        raise DesktopError(f"Unknown desktop integration platform: {platform_name}")

    # STATE_DIR is a fixed child of ~/.clawjournal and contains only artifacts
    # created by this feature.  Resolve and validate before recursive removal.
    resolved = STATE_DIR.resolve()
    if resolved.parent == CONFIG_DIR.resolve() and resolved.name == "desktop":
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
