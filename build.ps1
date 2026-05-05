# build.ps1  — builds agent + UI and packages both as ZIPs ready for upload
#
# Usage (from repo root, venv active):
#   .\build.ps1
#
# Output:
#   dist\telemetry_agent.zip   — upload to blob as telemetry_agent.zip
#   dist\telemetry_ui.zip      — upload to blob as telemetry_ui.zip

$ErrorActionPreference = 'Stop'
$PyInstaller = "user-track\Scripts\pyinstaller.exe"

Write-Host ""
Write-Host "=== ProdAnalytics Build ===" -ForegroundColor Cyan
Write-Host ""

# ── Clean previous build ──────────────────────────────────────────────────
Write-Host "[1/5] Cleaning previous build..." -ForegroundColor Gray
Remove-Item -Recurse -Force build, dist -ErrorAction SilentlyContinue
Write-Host "      Done" -ForegroundColor Green

# ── Build agent ───────────────────────────────────────────────────────────
Write-Host "[2/5] Building telemetry_agent..." -ForegroundColor Gray
& $PyInstaller telemetry_agent.spec
if (-not $?) { Write-Host "ERROR: agent build failed" -ForegroundColor Red; exit 1 }
Write-Host "      Done  →  dist\telemetry_agent\" -ForegroundColor Green

# ── Build UI ──────────────────────────────────────────────────────────────
Write-Host "[3/5] Building telemetry_ui..." -ForegroundColor Gray
& $PyInstaller telemetry_ui.spec
if (-not $?) { Write-Host "ERROR: UI build failed" -ForegroundColor Red; exit 1 }
Write-Host "      Done  →  dist\telemetry_ui\" -ForegroundColor Green

# ── Package ZIPs ─────────────────────────────────────────────────────────
Write-Host "[4/5] Packaging ZIPs..." -ForegroundColor Gray
Compress-Archive -Path "dist\telemetry_agent\*" -DestinationPath "dist\telemetry_agent.zip" -Force
Compress-Archive -Path "dist\telemetry_ui\*"    -DestinationPath "dist\telemetry_ui.zip"    -Force
Write-Host "      Done" -ForegroundColor Green

# ── Summary ───────────────────────────────────────────────────────────────
Write-Host ""
Write-Host "[5/5] Build complete:" -ForegroundColor Cyan
$agentSize = [math]::Round((Get-Item "dist\telemetry_agent.zip").Length / 1MB, 1)
$uiSize    = [math]::Round((Get-Item "dist\telemetry_ui.zip").Length / 1MB, 1)
Write-Host "      dist\telemetry_agent.zip  ($agentSize MB)" -ForegroundColor Green
Write-Host "      dist\telemetry_ui.zip     ($uiSize MB)"    -ForegroundColor Green
Write-Host ""
Write-Host "Next steps:" -ForegroundColor Yellow
Write-Host "  1. Upload both ZIPs to blob storage" -ForegroundColor Gray
Write-Host '     az storage blob upload --connection-string "<conn>" --container-name agent-releases --name telemetry_agent.zip --file dist\telemetry_agent.zip --overwrite' -ForegroundColor DarkGray
Write-Host '     az storage blob upload --connection-string "<conn>" --container-name agent-releases --name telemetry_ui.zip    --file dist\telemetry_ui.zip    --overwrite' -ForegroundColor DarkGray
Write-Host "  2. Update AGENT_DOWNLOAD_URL and UI_DOWNLOAD_URL in Azure App Service to point to the new .zip URLs" -ForegroundColor Gray
Write-Host "  3. Deploy backend: az webapp up --name <APP_NAME> --resource-group <RG>" -ForegroundColor Gray
Write-Host ""
