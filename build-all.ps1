# build-all.ps1 — Build Windows EXEs + Linux ZIP + Mac ZIP package
#
# Usage (from repo root, venv active):
#   .\build-all.ps1              # Windows EXEs + Linux ZIP + Mac ZIP
#   .\build-all.ps1 -SkipWindows # Linux + Mac only
#   .\build-all.ps1 -SkipLinux   # Windows + Mac only
#   .\build-all.ps1 -SkipMac     # Windows + Linux only
#   .\build-all.ps1 -NoLog       # Stream PyInstaller output to the console (debug)
#
# Output:
#   dist\telemetry_agent\        (+ dist\telemetry_agent.zip)
#   dist\telemetry_ui\           (+ dist\telemetry_ui.zip)
#   dist\linux-telemetry-agent.zip
#   dist\mac-telemetry-agent.zip
#   build.log  (full PyInstaller log unless -NoLog is passed)
#
# Exit code: 0 on success, non-zero on first failure.

[CmdletBinding()]
param(
    [switch]$SkipWindows,
    [switch]$SkipLinux,
    [switch]$SkipMac,
    [switch]$NoLog
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

# ── Paths ─────────────────────────────────────────────────────────────────────
$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Path
$RepoRoot    = $ScriptDir
$PyInstaller = Join-Path $RepoRoot "user-track\Scripts\pyinstaller.exe"
$VenvPython  = Join-Path $RepoRoot "user-track\Scripts\python.exe"
$DistDir     = Join-Path $RepoRoot "dist"
$BuildDir    = Join-Path $RepoRoot "build"
$LogFile     = Join-Path $RepoRoot "build.log"

# ── Helpers ───────────────────────────────────────────────────────────────────
function Step($n, $total, $label) {
    Write-Host ""
    Write-Host ("[{0}/{1}] {2}" -f $n, $total, $label) -ForegroundColor Cyan
}
function OK($msg)   { Write-Host "      OK   $msg" -ForegroundColor Green }
function INFO($msg) { Write-Host "      INFO $msg" -ForegroundColor Gray }
function FAIL($msg) {
    Write-Host ""
    Write-Host "      ERROR: $msg" -ForegroundColor Red
    if (-not $NoLog -and (Test-Path $LogFile)) {
        Write-Host "      See full log: $LogFile" -ForegroundColor Yellow
    }
    exit 1
}

# ── Sanity checks ─────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "================================================" -ForegroundColor Cyan
Write-Host "   Telemetry Platform Build" -ForegroundColor Cyan
Write-Host "================================================" -ForegroundColor Cyan
Write-Host ""
INFO "Repo root  : $RepoRoot"
INFO "Python     : $VenvPython"
INFO "PyInstaller: $PyInstaller"

if (-not (Test-Path $VenvPython)) {
    FAIL "Venv not found. Run: python -m venv user-track ; user-track\Scripts\pip install -r requirements.txt"
}

if (-not $SkipWindows -and -not (Test-Path $PyInstaller)) {
    FAIL "PyInstaller not found. Run: user-track\Scripts\pip install pyinstaller"
}

# ── Step counter ──────────────────────────────────────────────────────────────
$total = 0
if (-not $SkipWindows) { $total += 3 }   # agent, UI, zip
if (-not $SkipLinux)   { $total += 1 }   # linux zip
if (-not $SkipMac)     { $total += 1 }   # mac zip
$step  = 0

# ── Clean ─────────────────────────────────────────────────────────────────────
INFO "Cleaning previous build artifacts..."
Remove-Item -Recurse -Force $BuildDir, $DistDir -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $DistDir -Force | Out-Null

# ── Build one PyInstaller spec, capturing noise to build.log ──────────────────
# PyInstaller writes its 'INFO:' chatter to stderr, which PowerShell turns into
# error records.  We run with $ErrorActionPreference='Continue' for the duration
# of the native call so the script does NOT abort on every stderr line, then
# restore the stricter setting and check $LASTEXITCODE explicitly.
function Invoke-PyInstaller([string]$SpecFile) {
    $prevEAP = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        if ($NoLog) {
            # Debug mode: stream everything
            & $PyInstaller --noconfirm $SpecFile 2>&1 | Out-Null
            $exit = $LASTEXITCODE
        } else {
            # Capture to log; only show tail on success, full log on failure
            $out = & $PyInstaller --noconfirm $SpecFile 2>&1
            $exit = $LASTEXITCODE
            $out | Out-File -FilePath $LogFile -Encoding utf8
        }
    } finally {
        $ErrorActionPreference = $prevEAP
    }

    if ($exit -ne 0) {
        if (-not $NoLog -and (Test-Path $LogFile)) {
            Write-Host ""
            Write-Host "      --- last 40 lines of build.log ---" -ForegroundColor Yellow
            Get-Content $LogFile -Tail 40 | ForEach-Object { Write-Host "      $_" -ForegroundColor Yellow }
        }
        FAIL "$SpecFile build failed (exit $exit)"
    }
}

# ════════════════════════════════════════════════════════════════════════════
# WINDOWS BUILDS
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipWindows) {

    $step++
    Step $step $total "Building Windows agent (PyInstaller)"
    Invoke-PyInstaller "telemetry_agent.spec"
    if (-not (Test-Path (Join-Path $DistDir "telemetry_agent\telemetry_agent.exe"))) {
        FAIL "telemetry_agent.exe not produced"
    }
    OK "dist\telemetry_agent\"

    $step++
    Step $step $total "Building Windows UI (PyInstaller)"
    Invoke-PyInstaller "telemetry_ui.spec"
    if (-not (Test-Path (Join-Path $DistDir "telemetry_ui\telemetry_ui.exe"))) {
        FAIL "telemetry_ui.exe not produced"
    }
    OK "dist\telemetry_ui\"

    $step++
    Step $step $total "Packaging Windows ZIPs"
    $agentZip = Join-Path $DistDir "telemetry_agent.zip"
    $uiZip    = Join-Path $DistDir "telemetry_ui.zip"
    Compress-Archive -Path "$DistDir\telemetry_agent\*" -DestinationPath $agentZip -Force
    Compress-Archive -Path "$DistDir\telemetry_ui\*"    -DestinationPath $uiZip    -Force
    $agentMB = [math]::Round((Get-Item $agentZip).Length / 1MB, 1)
    $uiMB    = [math]::Round((Get-Item $uiZip).Length    / 1MB, 1)
    OK "telemetry_agent.zip  $agentMB MB"
    OK "telemetry_ui.zip     $uiMB MB"
}

# ════════════════════════════════════════════════════════════════════════════
# LINUX SCRIPT ZIP
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipLinux) {

    $step++
    Step $step $total "Packaging Linux script bundle"

    $stage = Join-Path $DistDir "linux-stage"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    # Required source files — fail fast if any are missing
    $required = @(
        @{ Src = "linux_telemetry_agent.py";     Dst = "linux_telemetry_agent.py"   },
        @{ Src = "linux_telemetry_ui.py";        Dst = "linux_telemetry_ui.py"      },
        @{ Src = "linux\install.sh";             Dst = "install.sh"                 },
        @{ Src = "linux\requirements-linux.txt"; Dst = "requirements-linux.txt"    },
        @{ Src = "agent.config.json";            Dst = "agent.config.json"          }
    )

    foreach ($f in $required) {
        $src = Join-Path $RepoRoot $f.Src
        $dst = Join-Path $stage   $f.Dst
        if (-not (Test-Path $src)) {
            FAIL "Required source missing: $f.Src"
        }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }

    # Normalise line endings to LF (required for bash scripts on Linux)
    foreach ($sh in (Get-ChildItem -LiteralPath $stage -Filter "*.sh")) {
        $c = [System.IO.File]::ReadAllText($sh.FullName) -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($sh.FullName, $c, [System.Text.UTF8Encoding]::new($false))
    }

    $linuxZip = Join-Path $DistDir "linux-telemetry-agent.zip"
    if (Test-Path $linuxZip) { Remove-Item -LiteralPath $linuxZip -Force }
    Compress-Archive -Path "$stage\*" -DestinationPath $linuxZip -Force
    Remove-Item -Recurse -Force $stage

    $linuxKB = [math]::Round((Get-Item $linuxZip).Length / 1KB, 1)
    OK "linux-telemetry-agent.zip  $linuxKB KB"
}

# ════════════════════════════════════════════════════════════════════════════
# MAC SCRIPT ZIP
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipMac) {

    $step++
    Step $step $total "Packaging Mac script bundle"

    $stage = Join-Path $DistDir "mac-stage"
    Remove-Item -Recurse -Force $stage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $stage -Force | Out-Null

    # Required source files — fail fast if any are missing
    $required = @(
        @{ Src = "mac_telemetry_agent.py";     Dst = "mac_telemetry_agent.py"   },
        @{ Src = "mac_telemetry_ui.py";        Dst = "mac_telemetry_ui.py"      },
        @{ Src = "mac\install.sh";             Dst = "install.sh"               },
        @{ Src = "mac\uninstall.sh";           Dst = "uninstall.sh"             },
        @{ Src = "mac\requirements-mac.txt";   Dst = "requirements-mac.txt"     },
        @{ Src = "agent.config.json";          Dst = "agent.config.json"        }
    )

    foreach ($f in $required) {
        $src = Join-Path $RepoRoot $f.Src
        $dst = Join-Path $stage   $f.Dst
        if (-not (Test-Path $src)) {
            FAIL "Required source missing: $($f.Src)"
        }
        Copy-Item -LiteralPath $src -Destination $dst -Force
    }

    # Normalise line endings to LF (required for bash scripts on macOS)
    foreach ($sh in (Get-ChildItem -LiteralPath $stage -Filter "*.sh")) {
        $c = [System.IO.File]::ReadAllText($sh.FullName) -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($sh.FullName, $c, [System.Text.UTF8Encoding]::new($false))
    }

    $macZip = Join-Path $DistDir "mac-telemetry-agent.zip"
    if (Test-Path $macZip) { Remove-Item -LiteralPath $macZip -Force }
    Compress-Archive -Path "$stage\*" -DestinationPath $macZip -Force
    Remove-Item -Recurse -Force $stage

    $macKB = [math]::Round((Get-Item $macZip).Length / 1KB, 1)
    OK "mac-telemetry-agent.zip  $macKB KB"
}

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "================================================" -ForegroundColor Green
Write-Host "   Build complete!" -ForegroundColor Green
Write-Host "================================================" -ForegroundColor Green
Write-Host ""
foreach ($f in (Get-ChildItem -LiteralPath $DistDir -Filter "*.zip" -ErrorAction SilentlyContinue)) {
    $kb = [math]::Round($f.Length / 1KB, 0)
    Write-Host ("  {0,-44} {1,8} KB" -f $f.Name, $kb) -ForegroundColor Green
}
if (-not $NoLog -and (Test-Path $LogFile)) {
    Write-Host ""
    INFO "Full PyInstaller log: $LogFile"
}
Write-Host ""
