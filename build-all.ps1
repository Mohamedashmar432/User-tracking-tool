# build-all.ps1 — Build Windows EXEs + Linux ZIP package
#
# Usage (from repo root, venv active):
#   .\build-all.ps1              # Windows EXEs + Linux ZIP
#   .\build-all.ps1 -SkipWindows # Linux ZIP only
#   .\build-all.ps1 -SkipLinux   # Windows EXEs only

param(
    [switch]$SkipWindows,
    [switch]$SkipLinux
)

# 'Continue' so PyInstaller's stderr log output doesn't trigger NativeCommandError.
# Exit codes are checked explicitly with $LASTEXITCODE after each native call.
$ErrorActionPreference = 'Continue'

$PyInstaller = Join-Path $PSScriptRoot "user-track\Scripts\pyinstaller.exe"
$VenvPython  = Join-Path $PSScriptRoot "user-track\Scripts\python.exe"

# Validate required tools exist
if (-not $SkipWindows) {
    if (-not (Test-Path $PyInstaller)) {
        Write-Host "ERROR: PyInstaller not found at $PyInstaller" -ForegroundColor Red
        Write-Host "       Run: user-track\Scripts\pip install pyinstaller" -ForegroundColor Yellow
        exit 1
    }
}

function Step($n, $total, $label) {
    Write-Host ""
    Write-Host "[$n/$total] $label" -ForegroundColor Cyan
}
function OK($msg)   { Write-Host "      OK  $msg" -ForegroundColor Green }
function FAIL($msg) { Write-Host "      ERROR: $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   Telemetry Platform Build" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan

# Count total steps
$total = 0
if (-not $SkipWindows) { $total += 3 }   # agent, UI, zip
if (-not $SkipLinux)   { $total += 1 }   # linux zip
$step = 0

# ── Clean
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path dist -Force | Out-Null

# ════════════════════════════════════════════════════════════════════════════
# WINDOWS BUILDS
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipWindows) {

    $step++
    Step $step $total "Building Windows agent (PyInstaller)"
    & $PyInstaller telemetry_agent.spec
    if ($LASTEXITCODE -ne 0) { FAIL "Windows agent build failed" }
    OK "dist\telemetry_agent\"

    $step++
    Step $step $total "Building Windows UI (PyInstaller)"
    & $PyInstaller telemetry_ui.spec
    if ($LASTEXITCODE -ne 0) { FAIL "Windows UI build failed" }
    OK "dist\telemetry_ui\"

    $step++
    Step $step $total "Packaging Windows ZIPs"
    Compress-Archive -Path "dist\telemetry_agent\*" -DestinationPath "dist\telemetry_agent.zip" -Force
    Compress-Archive -Path "dist\telemetry_ui\*"    -DestinationPath "dist\telemetry_ui.zip"    -Force
    $agentMB = [math]::Round((Get-Item "dist\telemetry_agent.zip").Length / 1MB, 1)
    $uiMB    = [math]::Round((Get-Item "dist\telemetry_ui.zip").Length / 1MB, 1)
    OK "telemetry_agent.zip  $agentMB MB"
    OK "telemetry_ui.zip     $uiMB MB"
}

# ════════════════════════════════════════════════════════════════════════════
# LINUX SCRIPT ZIP
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipLinux) {

    $step++
    Step $step $total "Packaging Linux script bundle"

    $stage = "dist\linux-stage"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    Copy-Item "linux_telemetry_agent.py"       "$stage\linux_telemetry_agent.py"
    Copy-Item "linux_telemetry_ui.py"          "$stage\linux_telemetry_ui.py"
    Copy-Item "linux\install.sh"               "$stage\install.sh"
    Copy-Item "linux\requirements-linux.txt"   "$stage\requirements-linux.txt"
    if (Test-Path "agent.config.json") {
        Copy-Item "agent.config.json"          "$stage\agent.config.json"
    }

    # Normalise line endings to LF (required for bash scripts on Linux)
    foreach ($f in (Get-ChildItem $stage -Filter "*.sh")) {
        $c = [System.IO.File]::ReadAllText($f.FullName)
        $c = $c -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($f.FullName, $c, [System.Text.Encoding]::UTF8)
    }

    Compress-Archive -Path "$stage\*" -DestinationPath "dist\linux-telemetry-agent.zip" -Force
    Remove-Item -Recurse -Force $stage

    $linuxKB = [math]::Round((Get-Item "dist\linux-telemetry-agent.zip").Length / 1KB, 1)
    OK "linux-telemetry-agent.zip  $linuxKB KB"
}

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "   Build complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
foreach ($f in (Get-ChildItem dist\*.zip -ErrorAction SilentlyContinue)) {
    $kb = [math]::Round($f.Length / 1KB, 0)
    Write-Host ("  {0,-44} {1,6} KB" -f $f.Name, $kb) -ForegroundColor Green
}
Write-Host ""
