"""Tests for the optional desktop shortcut and dynamic expression icon."""

from __future__ import annotations

import datetime as dt
import json
import subprocess
from pathlib import Path

import pytest

from clawjournal import desktop


@pytest.fixture
def isolated_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    config = home / ".clawjournal"
    state = config / "desktop"
    desktop_dir = home / "Desktop"
    monkeypatch.setattr(desktop, "CONFIG_DIR", config)
    monkeypatch.setattr(desktop, "STATE_DIR", state)
    monkeypatch.setattr(desktop, "ICONS_DIR", state / "icons")
    monkeypatch.setattr(desktop, "STATE_FILE", state / "install.json")
    monkeypatch.setattr(desktop, "LAST_OPENED_FILE", state / "last_opened")
    monkeypatch.setattr(desktop, "LOG_FILE", state / "launcher.log")
    monkeypatch.setattr(desktop, "WINDOWS_BOOTSTRAP", state / "windows-launch.py")
    monkeypatch.setattr(desktop, "_home", lambda: home)
    monkeypatch.setattr(desktop, "_desktop_dir", lambda _platform: desktop_dir)
    monkeypatch.setattr(desktop, "_platform", lambda: "linux")
    monkeypatch.setattr(desktop, "_frontend_available", lambda: True)
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)
    monkeypatch.setattr(
        desktop,
        "_run_quiet",
        lambda command: subprocess.CompletedProcess(command, 0, "", ""),
    )
    return home


def test_days_since_last_opened_uses_calendar_days(
    isolated_desktop: Path,
) -> None:
    now = dt.datetime(2026, 7, 17, 0, 5, tzinfo=dt.timezone(dt.timedelta(hours=-4)))
    desktop._write_last_opened(now - dt.timedelta(minutes=10))
    assert desktop.days_since_last_opened(now) == 1

    desktop._write_last_opened(now - dt.timedelta(days=30))
    assert desktop.days_since_last_opened(now) == 10


def test_render_face_writes_standard_icon_formats(isolated_desktop: Path) -> None:
    desktop.render_icon(0)
    desktop.render_icon(10)
    smile = (desktop.ICONS_DIR / "clawjournal-day-0.png").read_bytes()
    crying = (desktop.ICONS_DIR / "clawjournal-day-10.png").read_bytes()
    ico = (desktop.ICONS_DIR / "clawjournal-day-10.ico").read_bytes()
    icns = (desktop.ICONS_DIR / "clawjournal-day-10.icns").read_bytes()

    assert smile.startswith(b"\x89PNG\r\n\x1a\n")
    assert crying.startswith(b"\x89PNG\r\n\x1a\n")
    assert smile != crying
    assert ico[:6] == b"\x00\x00\x01\x00\x01\x00"
    assert b"\x89PNG\r\n\x1a\n" in ico
    assert icns.startswith(b"icns")
    assert b"ic08" in icns[:16]


def test_linux_install_refresh_and_uninstall(isolated_desktop: Path) -> None:
    result = desktop.install()
    shortcut = Path(result["shortcut"])

    assert shortcut.exists()
    assert "X-ClawJournal-Managed=true" in shortcut.read_text(encoding="utf-8")
    assert "clawjournal-day-0.png" in shortcut.read_text(encoding="utf-8")
    assert desktop.status()["installed"] is True

    opened = dt.datetime.now().astimezone() - dt.timedelta(days=20)
    desktop._write_last_opened(opened)
    refreshed = desktop.refresh(quiet=True)
    assert refreshed["day"] == 10
    assert refreshed["mood"] == "dramatic crying"
    assert "clawjournal-day-10.png" in shortcut.read_text(encoding="utf-8")

    removed = desktop.uninstall()
    assert removed["removed"] is True
    assert not shortcut.exists()
    assert not desktop.STATE_DIR.exists()


def test_install_removes_managed_previous_shortcut(isolated_desktop: Path) -> None:
    previous = isolated_desktop / "Desktop" / "ClawJournal Workbench.desktop"
    previous.parent.mkdir(parents=True)
    previous.write_text("X-ClawJournal-Managed=true\n", encoding="utf-8")
    desktop._write_state({
        "version": 1,
        "platform": "linux",
        "shortcut": str(previous),
    })

    result = desktop.install()

    assert not previous.exists()
    assert Path(result["shortcut"]).name == "ClawJournal.desktop"
    assert Path(result["shortcut"]).exists()


def test_note_opened_is_noop_until_installed(isolated_desktop: Path) -> None:
    desktop.note_opened()
    assert not desktop.LAST_OPENED_FILE.exists()


def test_note_opened_rewrites_only_on_a_new_day(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    desktop.install()
    refreshes: list[bool] = []
    monkeypatch.setattr(desktop, "refresh", lambda *, quiet=False: refreshes.append(quiet))

    # The daemon calls this on every index.html load; same-day opens must not
    # rewrite the shortcut.
    desktop.note_opened()
    desktop.note_opened()
    assert refreshes == []

    desktop._write_last_opened(dt.datetime.now().astimezone() - dt.timedelta(days=3))
    desktop.note_opened()
    assert refreshes == [True]
    assert desktop.opened_today() is True


def test_install_refuses_to_clobber_a_foreign_file_despite_stale_state(
    isolated_desktop: Path,
) -> None:
    """Ownership must be proven by the artifact, not by install.json."""
    result = desktop.install()
    shortcut = Path(result["shortcut"])

    # The user removed our shortcut and put their own file at the same path,
    # while install.json still names it.
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop.install()
    assert "My own launcher" in shortcut.read_text(encoding="utf-8")


def test_uninstall_leaves_a_foreign_file_in_place(isolated_desktop: Path) -> None:
    result = desktop.install()
    shortcut = Path(result["shortcut"])
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    assert desktop.uninstall()["removed"] is True
    assert shortcut.exists()


def test_uninstall_rejects_an_unknown_platform(isolated_desktop: Path) -> None:
    desktop._write_state({
        "version": 1,
        "platform": "haiku",
        "shortcut": str(isolated_desktop / "Desktop" / "ClawJournal.desktop"),
    })
    with pytest.raises(desktop.DesktopError, match="Unknown desktop integration platform"):
        desktop.uninstall()


def test_windows_shortcut_ownership_is_detected_in_both_encodings(
    isolated_desktop: Path,
) -> None:
    lnk = isolated_desktop / "Desktop" / "ClawJournal.lnk"
    lnk.parent.mkdir(parents=True)

    lnk.write_bytes(b"L\x00\x00\x00" + "windows-launch.py".encode("utf-16-le"))
    assert desktop._windows_shortcut_is_managed(lnk) is True

    lnk.write_bytes(b"L\x00\x00\x00" + b"clawjournal.cli")
    assert desktop._windows_shortcut_is_managed(lnk) is True

    lnk.write_bytes(b"L\x00\x00\x00" + "C:\\Games\\solitaire.exe".encode("utf-16-le"))
    assert desktop._windows_shortcut_is_managed(lnk) is False


def test_trim_log_keeps_the_recent_tail(isolated_desktop: Path) -> None:
    desktop.LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    line = "x" * 99 + "\n"
    desktop.LOG_FILE.write_text(line * 40_000, encoding="utf-8")
    assert desktop.LOG_FILE.stat().st_size > desktop.LOG_MAX_BYTES

    desktop._trim_log()

    trimmed = desktop.LOG_FILE.read_text(encoding="utf-8")
    assert len(trimmed) <= desktop.LOG_MAX_BYTES // 2
    # Whole lines only, and the newest content survives.
    assert trimmed.startswith("x")
    assert trimmed.endswith(line)

    desktop._trim_log()  # already small enough: unchanged
    assert desktop.LOG_FILE.read_text(encoding="utf-8") == trimmed


def test_days_since_last_opened_accepts_a_naive_stamp(isolated_desktop: Path) -> None:
    desktop.LAST_OPENED_FILE.parent.mkdir(parents=True, exist_ok=True)
    naive = dt.datetime.now() - dt.timedelta(days=4)
    desktop.LAST_OPENED_FILE.write_text(naive.isoformat(timespec="seconds") + "\n", encoding="utf-8")
    assert desktop.days_since_last_opened() == 4


def test_launchers_start_outside_home_shadow_package(isolated_desktop: Path) -> None:
    shell_launcher = desktop._shell_launcher(desktop._command("desktop", "launch"), log=True)
    launcher_lines = shell_launcher.splitlines()
    assert f"cd {desktop.shlex.quote(str(desktop.STATE_DIR))}" in launcher_lines
    assert f"cd {desktop.shlex.quote(str(isolated_desktop))}" not in launcher_lines

    desktop._write_windows_bootstrap()
    bootstrap = desktop.WINDOWS_BOOTSTRAP.read_text(encoding="utf-8")
    assert "from clawjournal.cli import main" in bootstrap
    assert "CREATE_NO_WINDOW" in bootstrap
    assert repr(str(desktop.LOG_FILE)) in bootstrap
    compile(bootstrap, str(desktop.WINDOWS_BOOTSTRAP), "exec")


def test_launch_reuses_live_workbench(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[object] = []
    monkeypatch.setattr(desktop, "note_opened", lambda: calls.append("opened"))
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 9001})
    monkeypatch.setattr(desktop, "_workbench_running", lambda port: port == 9001)
    monkeypatch.setattr(desktop, "_request_scan", lambda port: calls.append(("scan", port)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda url: calls.append(("browser", url)))

    desktop.launch()

    assert calls == [
        "opened",
        ("scan", 9001),
        ("browser", "http://localhost:9001/"),
    ]


def test_status_json_shape(isolated_desktop: Path) -> None:
    assert desktop.status() == {"installed": False}
    desktop.STATE_DIR.mkdir(parents=True)
    shortcut = isolated_desktop / "Desktop" / "ClawJournal.desktop"
    shortcut.parent.mkdir(parents=True)
    shortcut.write_text("managed", encoding="utf-8")
    desktop._write_last_opened()
    desktop._write_state({
        "version": 1,
        "platform": "linux",
        "shortcut": str(shortcut),
        "refresh_mode": "test",
    })
    result = json.loads(json.dumps(desktop.status()))
    assert result["installed"] is True
    assert result["day"] == 0
    assert result["mood"] == "big smile"
