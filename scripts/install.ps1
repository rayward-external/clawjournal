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

.PARAMETER VenvPath
    Where to create the venv. Default: $HOME\.clawjournal-venv (or the
    CLAWJOURNAL_VENV environment variable, if set).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1

.EXAMPLE
    .\scripts\install.ps1 -WithFrontend
#>
[CmdletBinding()]
param(
    [switch]$WithFrontend,
    [string]$VenvPath
)

$ErrorActionPreference = 'Stop'

# Resolve repo root (parent of scripts\). If we were piped via iwr|iex with no
# script on disk, $PSScriptRoot is empty — clone the repo first.
$RepoDir = $null
if ($PSScriptRoot -and (Test-Path (Join-Path $PSScriptRoot '..\pyproject.toml'))) {
    $RepoDir = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
} else {
    $target = if ($env:CLAWJOURNAL_REPO) { $env:CLAWJOURNAL_REPO } else { Join-Path $HOME 'clawjournal' }
    if (Test-Path (Join-Path $target '.git')) {
        Write-Host "-> Updating existing checkout at $target"
        & git -C $target pull --ff-only --quiet
    } else {
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

$versionLine = (& $python.Source @($python.Prefix + @('--version')) 2>&1) -join ' '
Write-Host "[ok] Python: $versionLine ($($python.Source))"

# 2) Create venv if missing.
$VenvPy = Join-Path $VenvPath 'Scripts\python.exe'
if (-not (Test-Path $VenvPy)) {
    Write-Host "-> Creating venv at $VenvPath"
    & $python.Source @($python.Prefix + @('-m', 'venv', $VenvPath))
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

$VenvBin = Join-Path $VenvPath 'Scripts'
$ClawJournalExe = Join-Path $VenvBin 'clawjournal.exe'

# 3) Install ClawJournal in editable mode.
Write-Host "-> Installing ClawJournal (editable) from $RepoDir"
& $VenvPy -m pip install --quiet --upgrade pip
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
& $VenvPy -m pip install --quiet -e $RepoDir
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 4) Optional frontend build. Failures here are non-fatal — the CLI install
#    already succeeded; only the opt-in frontend is missing.
if ($WithFrontend) {
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
                }
            }
        } finally {
            Pop-Location
        }
    }
}

# 5) Report.
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

# 6) Soft hints for optional runtime deps.
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

$managedTrufflehog = Join-Path $HOME ".clawjournal\bin\trufflehog.exe"
if (-not (Get-Command trufflehog -ErrorAction SilentlyContinue) -and -not (Test-Path $managedTrufflehog)) {
    Write-Host ""
    Write-Host "[i] TruffleHog is required when sharing exports."
    Write-Host "    Install a pinned, checksum-verified copy: $ClawJournalExe trufflehog install"
    Write-Host "    Or download a release binary: https://github.com/trufflesecurity/trufflehog/releases"
}
