# dev.ps1 — Start Azurite + FastAPI server for local development
#
# Usage:
#   .\dev.ps1
#
# Starts:
#   Azurite  (Table/Blob/Queue)  ->  localhost:10000-10002
#   FastAPI                      ->  http://localhost:8000
#   Press Ctrl+C to stop both.

$ErrorActionPreference = "Stop"

$AzuriteJs = "C:\Users\MohamedAshmar\AppData\Roaming\npm\node_modules\azurite\dist\src\azurite.js"
$Python    = Join-Path $PSScriptRoot "user-track\Scripts\python.exe"
$WorkDir   = $PSScriptRoot

# ── Helpers ────────────────────────────────────────────────────────────────────
function Test-Port($port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $port); return $true } catch { return $false } finally { $c.Dispose() }
}

Write-Host ""
Write-Host "======================================" -ForegroundColor Cyan
Write-Host "  Dev Server Launcher" -ForegroundColor Cyan
Write-Host "======================================" -ForegroundColor Cyan
Write-Host ""

# ── 1. Azurite ─────────────────────────────────────────────────────────────────
if (Test-Port 10002) {
    Write-Host "[1/2] Azurite already running on port 10002" -ForegroundColor Yellow
} else {
    if (-not (Test-Path $AzuriteJs)) {
        Write-Host "Installing Azurite..." -ForegroundColor Gray
        npm install -g azurite | Out-Null
        $AzuriteJs = "C:\Users\MohamedAshmar\AppData\Roaming\npm\node_modules\azurite\dist\src\azurite.js"
    }
    Write-Host "[1/2] Starting Azurite..." -ForegroundColor Cyan

    $azInfo = New-Object System.Diagnostics.ProcessStartInfo
    $azInfo.FileName               = "node"
    $azInfo.Arguments              = "`"$AzuriteJs`" --location `"$WorkDir`" --silent"
    $azInfo.WorkingDirectory       = $WorkDir
    $azInfo.UseShellExecute        = $false
    $azInfo.RedirectStandardOutput = $true
    $azInfo.RedirectStandardError  = $true
    $azInfo.CreateNoWindow         = $true
    $script:azProc = [System.Diagnostics.Process]::Start($azInfo)

    # Wait up to 15s
    $ok = $false
    for ($i = 1; $i -le 15; $i++) {
        Start-Sleep -Seconds 1
        if (Test-Port 10002) { $ok = $true; break }
    }
    if (-not $ok) {
        $script:azProc.WaitForExit(200) | Out-Null
        Write-Host "ERROR: Azurite failed to start" -ForegroundColor Red
        Write-Host $script:azProc.StandardError.ReadToEnd()
        exit 1
    }
    Write-Host "      Azurite UP (PID $($script:azProc.Id)) — ports 10000/10001/10002" -ForegroundColor Green
}

# ── 2. FastAPI ─────────────────────────────────────────────────────────────────
if (Test-Port 8000) {
    Write-Host "[2/2] Something already on port 8000 — kill it first (netstat -ano | findstr :8000)" -ForegroundColor Yellow
    exit 0
}

Write-Host "[2/2] Starting FastAPI server..." -ForegroundColor Cyan
Write-Host "      http://localhost:8000" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop." -ForegroundColor Yellow
Write-Host ""

# Run uvicorn in the foreground (--reload for live reloading)
$env:AZURE_STORAGE_CONNECTION_STRING = "UseDevelopmentStorage=true"
& $Python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
