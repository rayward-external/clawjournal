from __future__ import annotations

import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_posix_installer_supports_managed_sharing_dependencies():
    script = (ROOT / "scripts" / "install.sh").read_text()

    assert "--with-sharing" in script
    assert '"$VENV_BIN/clawjournal" betterleaks install' in script
    assert '"$VENV_BIN/clawjournal" trufflehog install' in script
    assert "--finalize-install" in script
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
    assert "--clear-pending" not in script
