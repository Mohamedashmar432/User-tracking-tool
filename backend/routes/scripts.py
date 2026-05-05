"""
Script delivery and file download endpoints.

All routes are public (no auth required).
"""

import os
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse

from ..auth import AGENT_KEY
from ..deps import _public_base

router = APIRouter()


@router.get("/agent-config")
async def agent_config(request: Request):
    """Called by the agent during --install to self-configure."""
    base = _public_base(request)
    return {
        "server_url":    base,
        "ingest_url":    f"{base}/ingest",
        "agent_api_key": AGENT_KEY,
    }


@router.get("/download-agent")
async def download_agent():
    """Legacy single-EXE download for old agents (v2.8 and earlier)."""
    redirect_url = os.getenv("AGENT_DOWNLOAD_URL")
    if redirect_url:
        return RedirectResponse(url=redirect_url)
    exe = Path("dist/telemetry_agent.exe")
    if not exe.exists():
        raise HTTPException(
            status_code=404,
            detail="Set AGENT_DOWNLOAD_URL env var to point to the hosted EXE.",
        )
    return FileResponse(str(exe), media_type="application/octet-stream",
                        filename="telemetry_agent.exe")


@router.get("/download-agent-zip")
async def download_agent_zip():
    """Full --onedir ZIP package for new installs and auto-updater (v2.9+)."""
    redirect_url = (
        os.getenv("AGENT_ZIP_DOWNLOAD_URL")
        or os.getenv("AGENT_DOWNLOAD_URL")
    )
    if redirect_url:
        return RedirectResponse(url=redirect_url)
    zip_file = Path("dist/telemetry_agent.zip")
    if not zip_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Set AGENT_ZIP_DOWNLOAD_URL (or AGENT_DOWNLOAD_URL) env var to point to the hosted ZIP.",
        )
    return FileResponse(str(zip_file), media_type="application/zip",
                        filename="telemetry_agent.zip")


@router.get("/download-ui")
async def download_ui():
    """Download the UI companion ZIP (redirected to blob or served locally)."""
    redirect_url = os.getenv("UI_DOWNLOAD_URL")
    if redirect_url:
        return RedirectResponse(url=redirect_url)
    zip_file = Path(__file__).parent.parent.parent / "dist" / "telemetry_ui.zip"
    if not zip_file.exists():
        raise HTTPException(
            status_code=404,
            detail="Set UI_DOWNLOAD_URL env var or build dist/telemetry_ui.zip.",
        )
    return FileResponse(str(zip_file), media_type="application/zip",
                        filename="telemetry_ui.zip")


@router.get("/install-script")
async def install_script(request: Request):
    """PowerShell installer — downloads ZIPs, extracts to Program Files, runs --install."""
    base = _public_base(request)
    script = f"""\
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $f = "$env:TEMP\\pa_install.ps1"
    Invoke-WebRequest -Uri "$ServerUrl/install-script" -OutFile $f -UseBasicParsing
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== ProdAnalytics Installer ===" -ForegroundColor Cyan
Write-Host "Server: $ServerUrl"
Write-Host ""

Write-Host "[1/4] Downloading agent..." -ForegroundColor Cyan
$AgentZip = "$env:TEMP\\agent.zip"
$AgentDir = "C:\\Program Files\\TelemetryAgent"
Invoke-WebRequest -Uri "$ServerUrl/download-agent-zip" -OutFile $AgentZip -UseBasicParsing
Unblock-File -Path $AgentZip -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null
Expand-Archive -Path $AgentZip -DestinationPath $AgentDir -Force
Get-ChildItem -Path $AgentDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $AgentZip -Force -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host "[2/4] Installing agent..." -ForegroundColor Cyan
& "$AgentDir\\telemetry_agent.exe" --install --server-url $ServerUrl
Write-Host "    Done" -ForegroundColor Green

Write-Host "[3/4] Downloading UI companion..." -ForegroundColor Cyan
$UiZip = "$env:TEMP\\ui.zip"
$UiDir = "C:\\Program Files\\TelemetryUI"
$UiExe = "$UiDir\\telemetry_ui.exe"
Invoke-WebRequest -Uri "$ServerUrl/download-ui" -OutFile $UiZip -UseBasicParsing
Unblock-File -Path $UiZip -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $UiDir -Force | Out-Null
Expand-Archive -Path $UiZip -DestinationPath $UiDir -Force
Get-ChildItem -Path $UiDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $UiZip -Force -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host "[4/4] Setting up UI autostart..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
$UiAction   = New-ScheduledTaskAction  -Execute $UiExe
$UiTrigger  = New-ScheduledTaskTrigger -AtLogOn
$UiSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName TelemetryUI -Action $UiAction -Trigger $UiTrigger -Settings $UiSettings -RunLevel Limited -Force | Out-Null
Start-Process $UiExe
Write-Host "    Done" -ForegroundColor Green

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Installation complete!" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Agent : C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"
Write-Host " UI    : $UiExe"
Write-Host " Log   : C:\\ProgramData\\TelemetryAgent\\agent.log"
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(content=script, media_type="text/plain")


@router.get("/uninstall-script")
async def uninstall_script(request: Request):
    """PowerShell uninstall script."""
    base = _public_base(request)
    script = f"""\
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $f = "$env:TEMP\\pa_uninstall.ps1"
    Invoke-WebRequest -Uri "$ServerUrl/uninstall-script" -OutFile $f -UseBasicParsing
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== Telemetry Agent Uninstaller ===" -ForegroundColor Cyan
Write-Host ""

$AgentExe = "C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"

Write-Host "[1/3] Stopping agent..." -ForegroundColor Cyan
if (Test-Path $AgentExe) {{ & $AgentExe --uninstall; Start-Sleep -Seconds 3 }}
schtasks /delete /tn TelemetryAgent         /f 2>$null
schtasks /delete /tn TelemetryAgentWatchdog /f 2>$null
Stop-Process -Name telemetry_agent -Force -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host "[2/3] Stopping UI companion..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
Stop-Process -Name telemetry_ui -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "    Done" -ForegroundColor Green

Write-Host "[3/3] Removing files..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryAgent"  -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\\ProgramData\\TelemetryAgent"    -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryUI"     -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:APPDATA\\TelemetryUI"          -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\TelemetryUI"     -ErrorAction SilentlyContinue
$tmp = [System.IO.Path]::GetTempPath()
Remove-Item -Recurse -Force ($tmp + "TelemetryAgent")            -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force ($tmp + "telemetry_backup")          -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host ""
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Uninstall complete!" -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Cyan
Write-Host " Agent data in the cloud is not affected."
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(content=script, media_type="text/plain")
