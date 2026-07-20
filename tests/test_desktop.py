"""Tests for the optional desktop shortcut and dynamic expression icon."""

from __future__ import annotations

import datetime as dt
import json
import socket
import subprocess
from pathlib import Path

import pytest

from clawjournal import desktop


@pytest.fixture
def isolated_desktop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    home = tmp_path / "home"
    desktop_dir = home / "Desktop"
    # Every desktop path derives from `clawjournal.config.CONFIG_DIR` at call
    # time, so redirecting that one attribute isolates all of them. The autouse
    # `tmp_config` fixture in conftest already does this for every test in the
    # suite; this only repoints it under the fake home so state and shortcut
    # live together.
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", home / ".clawjournal")
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
    smile = (desktop._icons_dir() / "clawjournal-day-0.png").read_bytes()
    crying = (desktop._icons_dir() / "clawjournal-day-10.png").read_bytes()
    ico = (desktop._icons_dir() / "clawjournal-day-10.ico").read_bytes()
    icns = (desktop._icons_dir() / "clawjournal-day-10.icns").read_bytes()

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
    assert not desktop._state_dir().exists()


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
    assert not desktop._last_opened_file().exists()


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
    desktop._log_file().parent.mkdir(parents=True, exist_ok=True)
    line = "x" * 99 + "\n"
    desktop._log_file().write_text(line * 40_000, encoding="utf-8")
    assert desktop._log_file().stat().st_size > desktop.LOG_MAX_BYTES

    desktop._trim_log()

    trimmed = desktop._log_file().read_text(encoding="utf-8")
    assert len(trimmed) <= desktop.LOG_MAX_BYTES // 2
    # Whole lines only, and the newest content survives.
    assert trimmed.startswith("x")
    assert trimmed.endswith(line)

    desktop._trim_log()  # already small enough: unchanged
    assert desktop._log_file().read_text(encoding="utf-8") == trimmed


def test_days_since_last_opened_accepts_a_naive_stamp(isolated_desktop: Path) -> None:
    desktop._last_opened_file().parent.mkdir(parents=True, exist_ok=True)
    naive = dt.datetime.now() - dt.timedelta(days=4)
    desktop._last_opened_file().write_text(naive.isoformat(timespec="seconds") + "\n", encoding="utf-8")
    assert desktop.days_since_last_opened() == 4


def test_desktop_paths_follow_the_patched_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: these paths were module constants bound at import time.

    `tmp_config` in conftest is autouse but only patches
    `clawjournal.config.CONFIG_DIR`, so anything captured at import escaped it
    and still pointed at the real ~/.clawjournal. A daemon test that served
    index.html would then reach `note_opened()` and rewrite the developer's
    actual desktop shortcut. Deliberately does NOT use `isolated_desktop`.
    """
    assert Path.home() not in desktop._state_file().parents

    redirected = tmp_path / "elsewhere"
    monkeypatch.setattr("clawjournal.config.CONFIG_DIR", redirected)
    state = redirected / "desktop"
    assert desktop._state_dir() == state
    assert desktop._state_file() == state / "install.json"
    assert desktop._icons_dir() == state / "icons"
    assert desktop._last_opened_file() == state / "last_opened"
    assert desktop._log_file() == state / "launcher.log"
    assert desktop._windows_bootstrap() == state / "windows-launch.py"


def test_refresh_refuses_to_overwrite_a_foreign_file(isolated_desktop: Path) -> None:
    """Refresh runs unattended daily — it must not clobber a user's file."""
    shortcut = Path(desktop.install()["shortcut"])
    shortcut.unlink()
    shortcut.write_text("[Desktop Entry]\nName=My own launcher\n", encoding="utf-8")

    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop.refresh(quiet=True)
    assert "My own launcher" in shortcut.read_text(encoding="utf-8")


def test_refresh_does_not_resurrect_a_deleted_shortcut(isolated_desktop: Path) -> None:
    """Deleting the icon must stick, rather than reappearing at the next run."""
    shortcut = Path(desktop.install()["shortcut"])
    shortcut.unlink()

    with pytest.raises(desktop.DesktopError, match="is missing"):
        desktop.refresh(quiet=True)
    assert not shortcut.exists()

    # note_opened swallows it so the daemon keeps serving pages regardless.
    desktop._write_last_opened(dt.datetime.now().astimezone() - dt.timedelta(days=3))
    desktop.note_opened()
    assert not shortcut.exists()


def test_windows_refresh_never_writes_an_unowned_shortcut(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Windows path had no guards at all: it recreated a deleted .lnk at
    every scheduled run, leaving the user unable to remove the icon."""
    wrote: list[Path] = []
    monkeypatch.setattr(desktop, "_write_windows_shortcut", lambda p, i: wrote.append(p))
    lnk = isolated_desktop / "Desktop" / f"{desktop.SHORTCUT_NAME}.lnk"
    lnk.parent.mkdir(parents=True, exist_ok=True)

    with pytest.raises(desktop.DesktopError, match="is missing"):
        desktop._refresh_windows(lnk, 0)
    assert wrote == []
    assert not lnk.exists()

    lnk.write_bytes(b"L\x00\x00\x00" + "C:\\Games\\solitaire.exe".encode("utf-16-le"))
    with pytest.raises(desktop.DesktopError, match="Refusing to replace"):
        desktop._refresh_windows(lnk, 0)
    assert wrote == []


def test_macos_install_leaves_nothing_behind_when_it_fails(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial bundle would lack Info.plist and disown itself permanently."""
    monkeypatch.setattr(desktop, "_platform", lambda: "macos")
    app = isolated_desktop / "Desktop" / f"{desktop.SHORTCUT_NAME}.app"
    real_write = desktop._write_bytes

    def fail_on_bundle_icon(path: Path, payload: bytes) -> None:
        if path.name == "ClawJournal.icns":
            raise OSError("No space left on device")
        real_write(path, payload)

    monkeypatch.setattr(desktop, "_write_bytes", fail_on_bundle_icon)
    with pytest.raises(OSError):
        desktop.install()

    assert not app.exists()
    assert list(app.parent.glob(f".{app.name}.*.new")) == []

    # And the failure must not have wedged a later attempt.
    monkeypatch.setattr(desktop, "_write_bytes", real_write)
    result = desktop.install()
    assert Path(result["shortcut"]) == app
    assert desktop._macos_app_is_managed(app) is True
    assert (app / "Contents" / "MacOS" / "ClawJournal").exists()


def test_launchers_start_outside_home_shadow_package(isolated_desktop: Path) -> None:
    shell_launcher = desktop._shell_launcher(desktop._command("desktop", "launch"), log=True)
    launcher_lines = shell_launcher.splitlines()
    assert f"cd {desktop.shlex.quote(str(desktop._state_dir()))}" in launcher_lines
    assert f"cd {desktop.shlex.quote(str(isolated_desktop))}" not in launcher_lines

    desktop._write_windows_bootstrap()
    bootstrap = desktop._windows_bootstrap().read_text(encoding="utf-8")
    assert "from clawjournal.cli import main" in bootstrap
    assert "CREATE_NO_WINDOW" in bootstrap
    assert repr(str(desktop._log_file())) in bootstrap
    compile(bootstrap, str(desktop._windows_bootstrap()), "exec")


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


def test_busy_daemon_is_reused_instead_of_starting_a_second_one(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: liveness was an /api/stats call with a 0.6s timeout, so a
    daemon busy scanning read as 'not running' and a duplicate was started."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Never accepts, exactly like a daemon too busy to answer. The backlog
    # must exceed the number of probes: each unaccepted connect holds a slot.
    listener.listen(64)
    port = listener.getsockname()[1]

    calls: list[object] = []
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": port})
    monkeypatch.setattr(desktop, "_request_scan", lambda p: calls.append(("scan", p)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: calls.append(("browser", u)))

    def must_not_run(**kwargs: object) -> None:
        raise AssertionError("started a second daemon against a live port")

    monkeypatch.setattr("clawjournal.workbench.daemon.run_server", must_not_run)
    try:
        assert desktop._workbench_running(port) is True
        desktop.launch()
    finally:
        listener.close()

    assert calls == [("scan", port), ("browser", f"http://localhost:{port}/")]
    assert desktop._workbench_running(port) is False  # closed again


def test_losing_the_startup_race_does_not_spawn_a_duplicate(
    isolated_desktop: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two fast clicks: the loser must join the winner, not take a random port."""
    calls: list[object] = []
    monkeypatch.setattr(desktop, "load_config", lambda: {"daemon_port": 8384})
    monkeypatch.setattr(desktop, "_workbench_running", lambda p: False)
    monkeypatch.setattr(desktop, "_request_scan", lambda p: calls.append(("scan", p)))
    monkeypatch.setattr(desktop.webbrowser, "open", lambda u: calls.append(("browser", u)))
    monkeypatch.setattr("clawjournal.pricing.ensure_pricing_fresh", lambda *a, **k: None)

    def bind_conflict(**kwargs: object) -> None:
        assert kwargs["allow_port_fallback"] is False
        raise OSError(48, "Address already in use")

    monkeypatch.setattr("clawjournal.workbench.daemon.run_server", bind_conflict)
    desktop.launch()

    assert calls == [("scan", 8384), ("browser", "http://localhost:8384/")]


def test_run_server_can_refuse_the_ephemeral_port_fallback() -> None:
    """`serve` keeps the fallback; the launcher opts out of it."""
    from clawjournal.workbench import daemon as daemon_mod

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(64)
    port = listener.getsockname()[1]
    try:
        with pytest.raises(OSError):
            daemon_mod.run_server(
                port=port, open_browser=False, allow_port_fallback=False
            )
    finally:
        listener.close()


def test_status_json_shape(isolated_desktop: Path) -> None:
    assert desktop.status() == {"installed": False}
    desktop._state_dir().mkdir(parents=True)
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
