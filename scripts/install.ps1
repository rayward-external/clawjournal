#Requires -Version 5.1
<#
.SYNOPSIS
    Install ClawJournal in editable mode into a venv on Windows.

.DESCRIPTION
    Native Windows PowerShell installer. For macOS / Linux / WSL / Git Bash,
    use scripts/install.sh instead. Idempotent: re-running upgrades the
    existing install.

.PARAMETER WithFrontend
    Also build the browser workbench (requires Node.js / npm).

.PARAMETER DesktopShortcut
    Build the browser workbench and install the one-click desktop shortcut.

.PARAMETER WithSharing
    Also install the pinned, checksum-verified Betterleaks and TruffleHog
    binaries used by the share gate.

.PARAMETER VenvPath
    Where to create the venv. Default: $HOME\.clawjournal-venv (or the
    CLAWJOURNAL_VENV environment variable, if set).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -WithFrontend

.EXAMPLE
    .\scripts\install.ps1 -DesktopShortcut

.EXAMPLE
    .\scripts\install.ps1 -WithFrontend -WithSharing
#>
[CmdletBinding()]
param(
    [switch]$WithFrontend,
    [switch]$DesktopShortcut,
    [switch]$WithSharing,
    [string]$VenvPath
)

$ErrorActionPreference = 'Stop'

if ($DesktopShortcut) {
    $WithFrontend = $true
}

# Bring an existing checkout up to the latest published version before
# installing from it. Safe by construction: fast-forward only, and only on a
# clean `main` — anything else is left untouched with an explanation, and the
# install proceeds from the code that is already there.
$script:SyncFrom = $null
$script:SyncTo = $null
$script:SyncBlocked = $false
function Sync-Checkout {
    param([string]$Repo)
    if (-not (Test-Path (Join-Path $Repo '.git'))) { return }
    if ($env:CLAWJOURNAL_NO_AUTO_UPDATE) {
        # Set by `clawjournal selfupdate --reinstall`, which already synced.
        return
    }
    # A failed sync must never abort the install — relax the error preference
    # locally (on PS 5.1, native stderr under redirection can otherwise become
    # a terminating error).
    $prevEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $branch = & git -C $Repo symbolic-ref --short -q HEAD 2>$null
        if ($branch -ne 'main') {
            Write-Host "[i] Not updating the checkout: it is on branch '$branch', not 'main'. Installing the current code."
            return
        }
        $dirty = & git -C $Repo status --porcelain --untracked-files=no 2>$null
        if ($dirty) {
            Write-Host "[i] Not updating the checkout: it has local changes (they are preserved). Installing the current code."
            return
        }
        $before = (& git -C $Repo rev-parse HEAD 2>$null) -join ''
        if ($LASTEXITCODE -ne 0) { $before = $null }
        & git -C $Repo fetch --quiet origin main 2>$null
        $fetchExit = $LASTEXITCODE
        if ($fetchExit -ne 0) {
            Write-Host "[i] Could not fetch the latest version (offline). Installing the current code."
            return
        }
        $upstream = (& git -C $Repo rev-parse FETCH_HEAD 2>$null) -join ''
        $upstreamExit = $LASTEXITCODE
        if (-not $before -or $upstreamExit -ne 0 -or -not $upstream) {
            Write-Host "[i] Could not compare the checkout with the latest published version. Installing the current code."
            return
        }
        if ($before -eq $upstream) {
            $script:SyncFrom = $before
            $script:SyncTo = $upstream
            Write-Host "[ok] Checkout is on the latest published version."
            return
        }
        & git -C $Repo merge-base --is-ancestor $before $upstream 2>$null
        if ($LASTEXITCODE -eq 0) {
            & git -C $Repo merge --ff-only --quiet $upstream 2>$null
            if ($LASTEXITCODE -eq 0) {
                $script:SyncFrom = $before
                $script:SyncTo = $upstream
                Write-Host "[ok] Checkout is on the latest published version."
                return
            }
            Write-Host "[x] Not installing: the latest published version could not be applied. Retry after checking the checkout." -ForegroundColor Red
            $script:SyncBlocked = $true
            return
        }
        & git -C $Repo merge-base --is-ancestor $upstream $before 2>$null
        if ($LASTEXITCODE -eq 0) {
            Write-Host "[x] Not installing: this main checkout has unpublished local commits. Move them to a branch, then retry." -ForegroundColor Red
        } else {
            Write-Host "[x] Not installing: this main checkout has diverged from the published version. Reconcile it, then retry." -ForegroundColor Red
        }
        $script:SyncBlocked = $true
    } finally {
        $ErrorActionPreference = $prevEap
    }
}

# Resolve repo root (parent of scripts\). If we were piped via iwr|iex with no
# script on disk, $PSScriptRoot is empty — clone the repo first.
$RepoDir = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot '..\pyproject.toml'))) {
    $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    $target = if ($env:CLAWJOURNAL_REPO) { $env:CLAWJOURNAL_REPO } else { Join-Path $HOME 'clawjournal' }
    if (-not (Test-Path (Join-Path $target '.git'))) {
        Write-Host "-> Cloning ClawJournal to $target"
        & git clone --quiet https://github.com/rayward-external/clawjournal.git $target
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    $RepoDir = $target
}

if (-not $VenvPath) {
    $VenvPath = if ($env:CLAWJOURNAL_VENV) { $env:CLAWJOURNAL_VENV } else { Join-Path $HOME '.clawjournal-venv' }
}

# 1) Find a Python 3.10+ launcher. The 'py' launcher is the canonical choice on
#    Windows, but fall back to python3 / python in case it's missing.
function Find-Python {
    $candidates = @(
        @{ Exe = 'py';      Prefix = @('-3') },
        @{ Exe = 'python3'; Prefix = @() },
        @{ Exe = 'python';  Prefix = @() }
    )
    $code = 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)'
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -ErrorAction SilentlyContinue
        if (-not $cmd) { continue }
        $checkArgs = @($c.Prefix + @('-c', $code))
        & $cmd.Source @checkArgs *> $null
        if ($LASTEXITCODE -eq 0) {
            return [pscustomobject]@{ Source = $cmd.Source; Prefix = $c.Prefix }
        }
    }
    return $null
}

$python = Find-Python
if (-not $python) {
    Write-Host @"
[x] Python 3.10+ not found on PATH.
    Install from https://python.org/downloads (check "Add Python to PATH"),
    open a new PowerShell window, then re-run this script.
"@ -ForegroundColor Red
    exit 1
}

# Direct installer invocations join the same advisory lock as automatic
# reinstalls. Internal reinstall children mark the environment to avoid
# recursively acquiring the lock their parent already owns.
if ($env:CLAWJOURNAL_INSTALL_LOCK_HELD -ne '1') {
    $installerArgs = @()
    if ($DesktopShortcut) {
        $installerArgs += '-DesktopShortcut'
    } elseif ($WithFrontend) {
        $installerArgs += '-WithFrontend'
    }
    if ($WithSharing) { $installerArgs += '-WithSharing' }
    if ($VenvPath) { $installerArgs += @('-VenvPath', $VenvPath) }
    $hostExe = (Get-Process -Id $PID).Path
    $lockCommand = @(
        (Join-Path $RepoDir 'scripts\install_lock.py'),
        '--',
        $hostExe,
        '-NoProfile',
        '-ExecutionPolicy', 'Bypass',
        '-File', (Join-Path $RepoDir 'scripts\install.ps1')
    ) + $installerArgs
    & $python.Source @($python.Prefix + $lockCommand)
    exit $LASTEXITCODE
}

$versionLine = (& $python.Source @($python.Prefix + @('--version')) 2>&1) -join ' '
Write-Host "[ok] Python: $versionLine ($($python.Source))"

Sync-Checkout -Repo $RepoDir
if ($script:SyncBlocked) { exit 1 }

# 2) Create venv if missing.
$VenvPy = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "-> Creating venv at $VenvPath"
    & $python.Source @($python.Prefix + @('-m', 'venv', $VenvPath))
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$VenvBin = Join-Path $VenvPath 'Scripts'
$ClawJournalExe = Join-Path $VenvBin 'clawjournal.exe'

# Record anything the direct checkout sync changed before installation begins.
# If pip or an optional install later fails, the pending notice must survive.
if ($script:SyncFrom -and $script:SyncTo) {
    $recordCode = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from clawjournal.selfupdate import record_install_sync; record_install_sync(Path(sys.argv[1]), sys.argv[2], sys.argv[3])'
    $previousRecordEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        & $VenvPy -c $recordCode $RepoDir $script:SyncFrom $script:SyncTo *> $null
    } finally {
        $ErrorActionPreference = $previousRecordEap
    }
}

# 3) Install ClawJournal in editable mode.
Write-Host "-> Installing ClawJournal (editable) from $RepoDir"
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPy -m pip install --quiet -e $RepoDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 4) Optional frontend build. Failures here are non-fatal — the CLI install
#    already succeeded; only the opt-in frontend is missing.
if ($WithFrontend) {
    $frontendBuildSucceeded = $false
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Warning "-WithFrontend requested but npm not found. Install Node.js (https://nodejs.org) and re-run."
    } else {
        Write-Host "-> Building browser workbench"
        Push-Location (Join-Path $RepoDir 'clawjournal\web\frontend')
        try {
            & npm install --silent
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "npm install failed (exit $LASTEXITCODE); workbench not built. The CLI is installed."
            } else {
                & npm run build --silent
                if ($LASTEXITCODE -ne 0) {
                    Write-Warning "npm run build failed (exit $LASTEXITCODE); workbench not built. The CLI is installed."
                } else {
                    $frontendBuildSucceeded = $true
                }
            }
        } finally {
            Pop-Location
        }
        if ($frontendBuildSucceeded) {
            # A revision stamp detects source deletions that mtime checks
            # cannot see. Failure to stamp is safe: finalization stays pending.
            $recordBuildCode = 'import sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); from clawjournal.selfupdate import record_frontend_build; record_frontend_build(Path(sys.argv[1]))'
            $previousBuildEap = $ErrorActionPreference
            $ErrorActionPreference = 'Continue'
            try {
                & $VenvPy -c $recordBuildCode $RepoDir *> $null
            } finally {
                $ErrorActionPreference = $previousBuildEap
            }
        }
    }
}

# 5) Optional sharing dependencies. Keep auto-update disabled while the
# installer is already operating on a freshly synchronized checkout.
if ($WithSharing) {
    Write-Host "-> Installing managed secret scanners"
    $previousNoAutoUpdate = $env:CLAWJOURNAL_NO_AUTO_UPDATE
    $env:CLAWJOURNAL_NO_AUTO_UPDATE = '1'
    try {
        & $ClawJournalExe betterleaks install
        if ($LASTEXITCODE -ne 0) { throw "Betterleaks installation failed (exit $LASTEXITCODE)." }
        & $ClawJournalExe trufflehog install
        if ($LASTEXITCODE -ne 0) { throw "TruffleHog installation failed (exit $LASTEXITCODE)." }
    } finally {
        if ($null -eq $previousNoAutoUpdate) {
            Remove-Item Env:CLAWJOURNAL_NO_AUTO_UPDATE -ErrorAction SilentlyContinue
        } else {
            $env:CLAWJOURNAL_NO_AUTO_UPDATE = $previousNoAutoUpdate
        }
    }
}

# 6) Optional desktop launcher. It uses the just-installed venv executable so
#    the shortcut remains independent of the user's PATH.
if ($DesktopShortcut) {
    Write-Host "-> Installing desktop shortcut"
    & $ClawJournalExe desktop install
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

# 7) Retire only the pending reasons this run actually reconciled. Frontend
# failures are non-fatal above, so the CLI verifies the built assets before
# clearing that reason; unrequested optional components remain pending.
$previousNoAutoUpdate = $env:CLAWJOURNAL_NO_AUTO_UPDATE
$env:CLAWJOURNAL_NO_AUTO_UPDATE = '1'
try {
    $finalizeArgs = @('selfupdate', '--finalize-install')
    if ($WithFrontend) { $finalizeArgs += '--frontend-requested' }
    if ($WithSharing) { $finalizeArgs += '--scanners-installed' }
    & $ClawJournalExe @finalizeArgs *> $null
} catch {
    # Finalization is best-effort; any unresolved notice remains in place.
} finally {
    if ($null -eq $previousNoAutoUpdate) {
        Remove-Item Env:CLAWJOURNAL_NO_AUTO_UPDATE -ErrorAction SilentlyContinue
    } else {
        $env:CLAWJOURNAL_NO_AUTO_UPDATE = $previousNoAutoUpdate
    }
}

$InstalledVersion = & $VenvPy -c "import clawjournal; print(clawjournal.__version__)" 2>$null
if (-not $InstalledVersion) { $InstalledVersion = '?' }
Write-Host ""
Write-Host "[ok] ClawJournal $InstalledVersion installed."

Write-Host ""
Write-Host "Run:    $ClawJournalExe scan"
Write-Host "        $ClawJournalExe serve"
Write-Host ""
Write-Host "Or add the venv to PATH for this session:"
Write-Host "        `$env:Path = `"$VenvBin;`" + `$env:Path"

# 8) Soft hints for optional runtime deps.
$DistHtml = Join-Path $RepoDir 'clawjournal\web\frontend\dist\index.html'
$FeSrcDir = Join-Path $RepoDir 'clawjournal\web\frontend\src'
$frontendBuilt = Test-Path $DistHtml
if (-not $frontendBuilt) {
    Write-Host ""
    Write-Host "[i] Browser workbench not built. To enable 'clawjournal serve':"
    Write-Host "      .\scripts\install.ps1 -WithFrontend     (requires Node.js)"
}
elseif (Test-Path $FeSrcDir) {
    # Source newer than the built assets — a sync without a rebuild leaves
    # 'clawjournal serve' showing a stale workbench (e.g. an empty Share queue).
    $distTime = (Get-Item $DistHtml).LastWriteTimeUtc
    $newestSrc = Get-ChildItem -Path $FeSrcDir -Recurse -File -ErrorAction SilentlyContinue |
        Sort-Object LastWriteTimeUtc -Descending | Select-Object -First 1
    if ($newestSrc -and $newestSrc.LastWriteTimeUtc -gt $distTime) {
        Write-Host ""
        Write-Host "[i] The browser workbench build looks out of date (its source is newer"
        Write-Host "    than the built assets). 'clawjournal serve' may show an old UI until"
        Write-Host "    you rebuild:"
        Write-Host "      .\scripts\install.ps1 -WithFrontend     (requires Node.js)"
    }
}

$managedBetterleaks = Join-Path $HOME ".clawjournal\bin\betterleaks.exe"
if (-not $WithSharing -and -not (Get-Command betterleaks -ErrorAction SilentlyContinue) -and -not (Test-Path $managedBetterleaks)) {
    Write-Host ""
    Write-Host "[i] Betterleaks is required when sharing exports."
    Write-Host "    Install a pinned, checksum-verified copy: $ClawJournalExe betterleaks install"
    Write-Host "    Or re-run: .\scripts\install.ps1 -WithSharing"
}

$managedTrufflehog = Join-Path $HOME ".clawjournal\bin\trufflehog.exe"
if (-not $WithSharing -and -not (Get-Command trufflehog -ErrorAction SilentlyContinue) -and -not (Test-Path $managedTrufflehog)) {
    Write-Host ""
    Write-Host "[i] TruffleHog is required when sharing exports."
    Write-Host "    Install a pinned, checksum-verified copy: $ClawJournalExe trufflehog install"
    Write-Host "    Or re-run: .\scripts\install.ps1 -WithSharing"
}
