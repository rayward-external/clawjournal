from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_posix_installer_supports_managed_sharing_dependencies():
    script = (ROOT / "scripts" / "install.sh").read_text()

    assert "--with-sharing" in script
    assert '"$VENV_BIN/clawjournal" betterleaks install' in script
    assert '"$VENV_BIN/clawjournal" trufflehog install' in script
    assert "--finalize-install" in script
    assert "record_install_sync" in script
    assert "install_lock.py" in script
    assert "CLAWJOURNAL_INSTALL_LOCK_HELD" in script
    assert "merge-base --is-ancestor" in script
    assert '"$REPO_DIR" "$SYNC_FROM" "$SYNC_TO"' in script
    assert script.index("record_install_sync") < script.index("pip install")
    assert "--clear-pending" not in script

    help_result = subprocess.run(
        ["sh", str(ROOT / "scripts" / "install.sh"), "--help"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert help_result.returncode == 0
    assert "--with-sharing" in help_result.stdout


def test_powershell_installer_supports_managed_sharing_dependencies():
    script = (ROOT / "scripts" / "install.ps1").read_text()

    assert "[switch]$WithSharing" in script
    assert "& $ClawJournalExe betterleaks install" in script
    assert "& $ClawJournalExe trufflehog install" in script
    assert "--finalize-install" in script
    assert "record_install_sync" in script
    assert "install_lock.py" in script
    assert "CLAWJOURNAL_INSTALL_LOCK_HELD" in script
    assert "merge-base --is-ancestor" in script
    assert "$RepoDir $script:SyncFrom $script:SyncTo" in script
    assert script.index("record_install_sync") < script.index("pip install")
    assert "--clear-pending" not in script


@pytest.mark.skipif(os.name == "nt", reason="POSIX advisory-lock check")
def test_direct_installer_uses_the_selfupdate_lock(tmp_path):
    import fcntl

    home = tmp_path / "home"
    lock = home / ".clawjournal" / "reinstall.lock"
    lock.parent.mkdir(parents=True)
    fd = os.open(str(lock), os.O_CREAT | os.O_RDWR, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    marker = tmp_path / "installer-started"
    env = {**os.environ, "HOME": str(home)}
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts" / "install_lock.py"),
            "--",
            sys.executable,
            "-c",
            f"from pathlib import Path; Path({str(marker)!r}).write_text('yes')",
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    stdout = stderr = b""
    try:
        time.sleep(0.2)
        assert not marker.exists()
        fcntl.flock(fd, fcntl.LOCK_UN)
        stdout, stderr = process.communicate(timeout=5)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        os.close(fd)

    assert process.returncode == 0, (stdout, stderr)
    assert marker.read_text() == "yes"


@pytest.mark.skipif(os.name == "nt", reason="POSIX installer invocation")
def test_posix_installer_rejects_clean_local_ahead_checkout(tmp_path):
    remote = tmp_path / "remote.git"
    repo = tmp_path / "repo"
    subprocess.run(
        ["git", "init", "--quiet", "--bare", "-b", "main", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "init", "--quiet", "-b", "main", str(repo)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "config", "user.name", "Test"],
        check=True,
    )
    scripts = repo / "scripts"
    scripts.mkdir()
    for name in ("install.sh", "install_lock.py"):
        (scripts / name).write_text((ROOT / "scripts" / name).read_text())
    (repo / "pyproject.toml").write_text("[project]\nname='lock-test'\nversion='0'\n")
    subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "published"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo), "push", "--quiet", "-u", "origin", "main"],
        check=True,
    )
    (repo / "LOCAL").write_text("unpublished\n")
    subprocess.run(["git", "-C", str(repo), "add", "LOCAL"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--quiet", "-m", "local ahead"],
        check=True,
    )
    venv = tmp_path / "must-not-exist"
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAWJOURNAL_VENV": str(venv),
    }

    result = subprocess.run(
        ["sh", str(scripts / "install.sh")],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=15,
    )

    assert result.returncode == 1
    assert "unpublished local commits" in result.stderr
    assert not venv.exists()
