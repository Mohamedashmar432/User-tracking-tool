# build-all.ps1 — Build Windows EXEs + Linux ZIP package in one command
#
# Usage (from repo root, venv active):
#   .\build-all.ps1                      # Windows + Linux
#   .\build-all.ps1 -SkipWindows         # Linux ZIP only
#   .\build-all.ps1 -SkipLinux           # Windows EXEs only
#   .\build-all.ps1 -DockerLinux         # also build Linux EXEs via Docker
#
# Outputs:
#   dist\telemetry_agent.zip             — Windows agent  (upload to blob)
#   dist\telemetry_ui.zip                — Windows UI     (upload to blob)
#   dist\linux-telemetry-agent.zip       — Linux scripts  (served by /install-script-linux)
#   dist\linux-telemetry-agent-docker.zip  (only with -DockerLinux)

param(
    [switch]$SkipWindows,
    [switch]$SkipLinux,
    [switch]$DockerLinux
)

$ErrorActionPreference = 'Stop'
$PyInstaller = "user-track\Scripts\pyinstaller.exe"

$total_steps = 0
if (-not $SkipWindows) { $total_steps += 3 }
if (-not $SkipLinux)   { $total_steps += 2 }
if ($DockerLinux)       { $total_steps += 1 }
$step = 0

function Step($label) {
    $script:step++
    Write-Host ""
    Write-Host "[$step/$total_steps] $label" -ForegroundColor Cyan
}

function OK($msg)   { Write-Host "      OK  $msg" -ForegroundColor Green }
function WARN($msg) { Write-Host "      WARN $msg" -ForegroundColor Yellow }
function ERR($msg)  { Write-Host "      ERROR $msg" -ForegroundColor Red; exit 1 }

# ── Banner ────────────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Telemetry Platform -- Unified Build" -ForegroundColor Cyan
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

# ── Run tests first ───────────────────────────────────────────────────────────
Write-Host "[pre] Running Linux logic tests..." -ForegroundColor Gray
$testResult = & python -W ignore test_linux.py 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host $testResult; ERR "Linux logic tests failed" }
$passLine = $testResult | Where-Object { $_ -match 'Results:' } | Select-Object -Last 1
OK "Logic tests:     $passLine"

Write-Host "[pre] Running Linux auto-update tests..." -ForegroundColor Gray
$testResult2 = & python -W ignore test_linux_autoupdate.py 2>&1
if ($LASTEXITCODE -ne 0) { Write-Host $testResult2; ERR "Linux auto-update tests failed" }
$passLine2 = $testResult2 | Where-Object { $_ -match 'Results:' } | Select-Object -Last 1
OK "Auto-update tests: $passLine2"

# ── Clean ─────────────────────────────────────────────────────────────────────
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path dist -Force | Out-Null
OK "Clean"

# ════════════════════════════════════════════════════════════════════════════
# WINDOWS BUILDS
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipWindows) {

    Step "Building Windows agent (PyInstaller)"
    & $PyInstaller telemetry_agent.spec
    if (-not $?) { ERR "Windows agent build failed" }
    OK "dist\telemetry_agent\"

    Step "Building Windows UI (PyInstaller)"
    & $PyInstaller telemetry_ui.spec
    if (-not $?) { ERR "Windows UI build failed" }
    OK "dist\telemetry_ui\"

    Step "Packaging Windows ZIPs"
    Compress-Archive -Path "dist\telemetry_agent\*" -DestinationPath "dist\telemetry_agent.zip" -Force
    Compress-Archive -Path "dist\telemetry_ui\*"    -DestinationPath "dist\telemetry_ui.zip"    -Force
    $agentMB = [math]::Round((Get-Item "dist\telemetry_agent.zip").Length / 1MB, 1)
    $uiMB    = [math]::Round((Get-Item "dist\telemetry_ui.zip").Length / 1MB, 1)
    OK "telemetry_agent.zip ($agentMB MB)"
    OK "telemetry_ui.zip    ($uiMB MB)"
}

# ════════════════════════════════════════════════════════════════════════════
# LINUX SCRIPT ZIP  (no cross-compilation needed — scripts run with system Python)
# ════════════════════════════════════════════════════════════════════════════
if (-not $SkipLinux) {

    Step "Packaging Linux script bundle"

    $linuxStage = "dist\linux-stage"
    Remove-Item -Recurse -Force $linuxStage -ErrorAction SilentlyContinue
    New-Item -ItemType Directory -Path $linuxStage -Force | Out-Null

    # Core scripts
    Copy-Item "linux_telemetry_agent.py" "$linuxStage\linux_telemetry_agent.py"
    Copy-Item "linux_telemetry_ui.py"    "$linuxStage\linux_telemetry_ui.py"

    # Installer and deps manifest
    Copy-Item "linux\install.sh"             "$linuxStage\install.sh"
    Copy-Item "linux\requirements-linux.txt" "$linuxStage\requirements-linux.txt"

    # Bundle default config if it exists
    if (Test-Path "agent.config.json") {
        Copy-Item "agent.config.json" "$linuxStage\agent.config.json"
    }

    # Normalise line endings to LF (critical for bash scripts on Linux)
    $shFiles = Get-ChildItem $linuxStage -Filter "*.sh"
    foreach ($f in $shFiles) {
        $content = [System.IO.File]::ReadAllText($f.FullName)
        $content = $content -replace "`r`n", "`n"
        [System.IO.File]::WriteAllText($f.FullName, $content, [System.Text.Encoding]::UTF8)
    }

    Compress-Archive -Path "$linuxStage\*" -DestinationPath "dist\linux-telemetry-agent.zip" -Force
    Remove-Item -Recurse -Force $linuxStage

    $linuxMB = [math]::Round((Get-Item "dist\linux-telemetry-agent.zip").Length / 1KB, 1)
    OK "linux-telemetry-agent.zip ($linuxMB KB)"

    Step "Verifying Linux ZIP contents"
    $zip  = [System.IO.Compression.ZipFile]::OpenRead((Resolve-Path "dist\linux-telemetry-agent.zip"))
    $entries = $zip.Entries | ForEach-Object { $_.Name }
    $zip.Dispose()
    $required = @("linux_telemetry_agent.py", "linux_telemetry_ui.py", "install.sh", "requirements-linux.txt")
    $missing  = $required | Where-Object { $entries -notcontains $_ }
    if ($missing) {
        ERR "Linux ZIP missing: $($missing -join ', ')"
    }
    OK "ZIP contains: $($entries -join ', ')"
}

# ════════════════════════════════════════════════════════════════════════════
# OPTIONAL: Linux EXEs via Docker (requires Docker Desktop)
# ════════════════════════════════════════════════════════════════════════════
if ($DockerLinux) {

    Step "Building Linux EXEs via Docker"

    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        WARN "Docker not found — skipping Linux binary build"
    } else {
        # Write a temporary Dockerfile for the Linux build
        $dockerfile = @"
FROM python:3.11-slim
RUN apt-get update -qq && apt-get install -y -qq \
    binutils xdotool xprintidle dbus-x11 \
    python3-tk tk-dev && \
    pip install --quiet pyinstaller requests pystray pillow
WORKDIR /build
COPY linux_telemetry_agent.py linux_telemetry_ui.py agent.config.json ./
RUN pyinstaller --onedir --noconsole \
    --hidden-import pystray._xorg \
    --add-data "agent.config.json:." \
    linux_telemetry_agent.py && \
    pyinstaller --onedir --noconsole \
    --hidden-import pystray._xorg \
    --hidden-import PIL._tkinter_finder \
    --add-data "agent.config.json:." \
    linux_telemetry_ui.py
"@
        $dockerfile | Set-Content "Dockerfile.linux-build" -Encoding UTF8

        docker build -f Dockerfile.linux-build -t telemetry-linux-build . 2>&1
        if ($LASTEXITCODE -ne 0) {
            WARN "Docker build failed — Linux EXEs not produced"
        } else {
            # Extract built dirs from container
            $cid = docker create telemetry-linux-build
            docker cp "${cid}:/build/dist/linux_telemetry_agent" "dist\linux_agent_bin"
            docker cp "${cid}:/build/dist/linux_telemetry_ui"    "dist\linux_ui_bin"
            docker rm $cid | Out-Null

            Compress-Archive -Path "dist\linux_agent_bin\*" -DestinationPath "dist\linux-telemetry-agent-docker.zip" -Force
            Compress-Archive -Path "dist\linux_ui_bin\*"    -DestinationPath "dist\linux-telemetry-ui-docker.zip"    -Force
            Remove-Item -Recurse -Force "dist\linux_agent_bin", "dist\linux_ui_bin"
            Remove-Item "Dockerfile.linux-build" -ErrorAction SilentlyContinue

            $bMB = [math]::Round((Get-Item "dist\linux-telemetry-agent-docker.zip").Length / 1MB, 1)
            OK "linux-telemetry-agent-docker.zip ($bMB MB)"
        }
    }
}

# ════════════════════════════════════════════════════════════════════════════
# SUMMARY
# ════════════════════════════════════════════════════════════════════════════
Write-Host ""
Write-Host "============================================" -ForegroundColor Cyan
Write-Host "  Build complete!" -ForegroundColor Green
Write-Host "============================================" -ForegroundColor Cyan
Write-Host ""

$artifacts = Get-ChildItem dist\*.zip -ErrorAction SilentlyContinue
foreach ($f in $artifacts) {
    $sz = [math]::Round($f.Length / 1KB, 0)
    Write-Host ("  {0,-42} {1,6} KB" -f $f.Name, $sz) -ForegroundColor Green
}

Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Upload Windows ZIPs to Azure Blob:" -ForegroundColor Gray
Write-Host '     az storage blob upload --connection-string "<conn>" --container-name agent-releases --name telemetry_agent.zip       --file dist\telemetry_agent.zip       --overwrite' -ForegroundColor DarkGray
Write-Host '     az storage blob upload --connection-string "<conn>" --container-name agent-releases --name telemetry_ui.zip          --file dist\telemetry_ui.zip          --overwrite' -ForegroundColor DarkGray
Write-Host '     az storage blob upload --connection-string "<conn>" --container-name agent-releases --name linux-telemetry-agent.zip --file dist\linux-telemetry-agent.zip --overwrite' -ForegroundColor DarkGray
Write-Host ""
Write-Host "  2. Linux install one-liner (on target machine):" -ForegroundColor Gray
Write-Host '     curl -fsSL https://<server>/install-script-linux | bash' -ForegroundColor DarkGray
Write-Host ""
Write-Host "  3. Or extract the ZIP and run manually:" -ForegroundColor Gray
Write-Host '     unzip linux-telemetry-agent.zip && bash install.sh --server-url https://<server>' -ForegroundColor DarkGray
Write-Host ""
Write-Host "  4. Deploy backend: az webapp up --name <APP_NAME> --resource-group <RG>" -ForegroundColor Gray
Write-Host ""
