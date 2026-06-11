# setup.ps1 — one-shot dev setup + build for the Star Citizen Cargo Manager.
#
# Run from the repo root in PowerShell:
#
#     .\setup.ps1                  # install deps + smoke test (default)
#     .\setup.ps1 -Build           # also rebuild the .exe
#     .\setup.ps1 -Build -Clean    # rebuild from scratch (deletes dist/)
#     .\setup.ps1 -Run             # install deps + launch from source
#
# Goals: make sure THIS Python has every runtime dependency the app
# uses, then optionally rebuild the PyInstaller bundle so the deps are
# baked into the .exe too. Diagnostic output names which interpreter
# pip ran against — the #1 source of "I installed it but the .exe
# still complains" confusion.

[CmdletBinding()]
param(
    [switch]$Build,
    [switch]$Clean,
    [switch]$Run
)

$ErrorActionPreference = "Stop"

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Write-OK($msg) {
    Write-Host "    [OK] $msg" -ForegroundColor Green
}

function Write-Warn($msg) {
    Write-Host "    [WARN] $msg" -ForegroundColor Yellow
}

# ── 1. Find a Python interpreter ─────────────────────────────────────

Write-Step "Locating Python"
$python = $null
foreach ($candidate in @("python", "py -3", "python3")) {
    try {
        $version = & cmd /c "$candidate --version 2>&1"
        if ($LASTEXITCODE -eq 0) {
            $python = $candidate
            Write-OK "Using '$candidate' -> $version"
            break
        }
    } catch {}
}
if (-not $python) {
    Write-Error "No Python found on PATH. Install Python 3.11+ from python.org."
    exit 1
}

# Print sys.executable so the user knows EXACTLY which interpreter
# the install will hit (matches the path shown in Settings -> Capture).
$exe = & cmd /c "$python -c `"import sys; print(sys.executable)`""
Write-OK "Target interpreter: $exe"

# ── 2. Install runtime dependencies ───────────────────────────────────

Write-Step "Installing runtime dependencies from requirements.txt"
& cmd /c "$python -m pip install --upgrade pip"
& cmd /c "$python -m pip install -r requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Error "pip install failed. Read the output above."
    exit 1
}
Write-OK "requirements.txt installed"

# ── 3. Verify the capture stack ───────────────────────────────────────

Write-Step "Verifying capture libraries"
$probe = @"
import importlib, sys
results = {}
for name in ('mss', 'pygetwindow', 'keyboard'):
    try:
        m = importlib.import_module(name)
        results[name] = ('OK', getattr(m, '__version__', '(no version)'))
    except Exception as e:
        results[name] = ('FAIL', str(e))
for n, (status, info) in results.items():
    print(f'{status:4} {n:12} {info}')
sys.exit(0 if all(s == 'OK' for s, _ in results.values()) else 2)
"@
$probeFile = New-TemporaryFile
$probe | Set-Content -Path $probeFile -Encoding UTF8
try {
    & cmd /c "$python `"$probeFile`""
    $probeExit = $LASTEXITCODE
} finally {
    Remove-Item $probeFile -ErrorAction SilentlyContinue
}
if ($probeExit -ne 0) {
    Write-Warn "One or more capture libraries failed to import. The "
    Write-Warn "Capture tab in Settings will show ⛔ chips and a fix command."
} else {
    Write-OK "mss + pygetwindow + keyboard all importable"
}

# ── 4. Optional: launch from source ───────────────────────────────────

if ($Run) {
    Write-Step "Launching the app from source"
    & cmd /c "$python run.py"
    exit $LASTEXITCODE
}

# ── 5. Optional: PyInstaller build ────────────────────────────────────

if ($Build) {
    Write-Step "Verifying PyInstaller"
    & cmd /c "$python -m pip install --upgrade pyinstaller"

    if ($Clean -and (Test-Path "dist")) {
        Write-Step "Removing previous dist/ folder"
        Remove-Item -Recurse -Force "dist"
        Write-OK "dist/ cleared"
    }
    if ($Clean -and (Test-Path "build")) {
        Remove-Item -Recurse -Force "build"
        Write-OK "build/ cleared"
    }

    Write-Step "Running PyInstaller (cargo_manager.spec)"
    $pyiArgs = "cargo_manager.spec"
    if ($Clean) { $pyiArgs = "$pyiArgs --clean" }
    & cmd /c "$python -m PyInstaller $pyiArgs"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "PyInstaller build failed. Read the output above."
        exit 1
    }

    $outDir = "dist\CargoManager"
    if (Test-Path $outDir) {
        $size = (Get-ChildItem $outDir -Recurse | Measure-Object -Property Length -Sum).Sum
        $sizeMB = [math]::Round($size / 1MB, 1)
        Write-OK "Bundle: $outDir  (~$sizeMB MB)"
        Write-Host ""
        Write-Host "    Launch with:  $outDir\CargoManager.exe" -ForegroundColor Green
    } else {
        Write-Warn "Build finished but $outDir was not created."
    }
}

Write-Step "Done."
