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
$ErrorActionPreference = 'SilentlyContinue'
$ServerUrl = '{base}'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $f = "$env:TEMP\\pa_install.ps1"
    Invoke-WebRequest -Uri "$ServerUrl/install-script" -OutFile $f -UseBasicParsing
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$f`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== ProdAnalytics Installer ===" -ForegroundColor Cyan
Write-Host "Server : $ServerUrl"
Write-Host ""

Write-Host "[1/4] Downloading agent..." -ForegroundColor Cyan
$AgentZip = "$env:TEMP\\agent.zip"
$AgentDir = "C:\\Program Files\\TelemetryAgent"
Invoke-WebRequest -Uri "$ServerUrl/download-agent-zip" -OutFile $AgentZip -UseBasicParsing -ErrorAction Stop
Unblock-File -Path $AgentZip
Remove-Item -Recurse -Force $AgentDir
New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null
Expand-Archive -Path $AgentZip -DestinationPath $AgentDir -Force -ErrorAction Stop
Get-ChildItem -Path $AgentDir -Recurse | Unblock-File
Remove-Item $AgentZip -Force
Write-Host "      OK" -ForegroundColor Green

Write-Host "[2/4] Installing agent..." -ForegroundColor Cyan
& "$AgentDir\\telemetry_agent.exe" --install --server-url $ServerUrl
Write-Host "      Agent installed and started" -ForegroundColor Green

Write-Host "[3/4] Downloading UI companion..." -ForegroundColor Cyan
$UiZip = "$env:TEMP\\ui.zip"
$UiDir = "C:\\Program Files\\TelemetryUI"
$UiExe = "$UiDir\\telemetry_ui.exe"
Invoke-WebRequest -Uri "$ServerUrl/download-ui" -OutFile $UiZip -UseBasicParsing -ErrorAction Stop
Unblock-File -Path $UiZip
Remove-Item -Recurse -Force $UiDir
New-Item -ItemType Directory -Path $UiDir -Force | Out-Null
Expand-Archive -Path $UiZip -DestinationPath $UiDir -Force -ErrorAction Stop
Get-ChildItem -Path $UiDir -Recurse | Unblock-File
Remove-Item $UiZip -Force
Write-Host "      OK" -ForegroundColor Green

Write-Host "[4/4] Setting up UI autostart..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
$action   = New-ScheduledTaskAction  -Execute $UiExe
$trigger  = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName TelemetryUI -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
Start-Process $UiExe
Write-Host "      Registered and launched" -ForegroundColor Green

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Installation complete!" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Agent  : C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"
Write-Host " UI     : $UiExe"
Write-Host " Log    : C:\\ProgramData\\TelemetryAgent\\agent.log"
Write-Host " Both start automatically at every Windows login." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(content=script, media_type="text/plain")


@router.get("/uninstall-script")
async def uninstall_script(request: Request):
    """PowerShell uninstall script."""
    base = _public_base(request)
    script = f"""\
$ErrorActionPreference = 'SilentlyContinue'
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
Stop-Process -Name telemetry_agent -Force
Write-Host "      Done" -ForegroundColor Green

Write-Host "[2/3] Removing UI companion..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
Stop-Process -Name telemetry_ui -Force
Start-Sleep -Seconds 2
Write-Host "      Done" -ForegroundColor Green

Write-Host "[3/3] Removing files..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryAgent"
Remove-Item -Recurse -Force "C:\\ProgramData\\TelemetryAgent"
Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryUI"
Remove-Item -Recurse -Force "$env:APPDATA\\TelemetryUI"
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\\TelemetryUI"
$tmp = [System.IO.Path]::GetTempPath()
Remove-Item -Recurse -Force ($tmp + "TelemetryAgent")
Remove-Item -Recurse -Force ($tmp + "telemetry_backup")
Write-Host "      Done" -ForegroundColor Green

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Uninstall complete!" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Agent data in the cloud is not affected."
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(content=script, media_type="text/plain")


@router.get("/install-script-linux")
async def install_script_linux(request: Request):
    """
    Bash installer for Linux — serves linux/install.sh with SERVER_URL and
    AGENT_API_KEY injected so `curl | bash` works with no extra flags.
    """
    base = _public_base(request)
    install_sh = Path(__file__).parent.parent.parent / "linux" / "install.sh"
    if not install_sh.exists():
        raise HTTPException(status_code=404, detail="linux/install.sh not found in repo")

    content = install_sh.read_text(encoding="utf-8")

    content = content.replace(
        'SERVER_URL="${SERVER_URL:-}"',
        f'SERVER_URL="${{SERVER_URL:-{base}}}"',
    )
    # AGENT_KEY is write-only (POST /ingest) — safe to embed in the install script
    if AGENT_KEY:
        content = content.replace(
            'AGENT_API_KEY="${AGENT_API_KEY:-}"',
            f'AGENT_API_KEY="${{AGENT_API_KEY:-{AGENT_KEY}}}"',
        )
    content = content.replace("\r\n", "\n")
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@router.get("/uninstall-script-linux")
async def uninstall_script_linux(request: Request):
    """
    Bash uninstaller for Linux.

    curl -fsSL <server>/uninstall-script-linux | bash
    curl -fsSL <server>/uninstall-script-linux | bash -s -- --yes
    YES=1 curl -fsSL <server>/uninstall-script-linux | bash
    """
    uninstall_sh = Path(__file__).parent.parent.parent / "linux" / "uninstall.sh"
    if not uninstall_sh.exists():
        raise HTTPException(status_code=404, detail="linux/uninstall.sh not found in repo")

    content = uninstall_sh.read_text(encoding="utf-8")
    content = content.replace("\r\n", "\n")
    return PlainTextResponse(content=content, media_type="text/plain; charset=utf-8")


@router.get("/download-linux-agent")
async def download_linux_agent():
    """Serve linux_telemetry_agent.py for curl-based installs."""
    f = Path(__file__).parent.parent.parent / "linux_telemetry_agent.py"
    if not f.exists():
        raise HTTPException(status_code=404, detail="linux_telemetry_agent.py not found")
    return PlainTextResponse(content=f.read_text(encoding="utf-8"),
                             media_type="text/plain; charset=utf-8")


@router.get("/download-linux-ui")
async def download_linux_ui():
    """Backward-compat redirect — old agents that request the tray UI get the dashboard."""
    return RedirectResponse(url="/download-linux-dashboard", status_code=301)


@router.get("/download-linux-dashboard")
async def download_linux_dashboard():
    """
    Serve linux_telemetry_dashboard.py for curl-based installs.
    IMPORTANT: this file must remain pure stdlib — no pip packages.
    """
    f = Path(__file__).parent.parent.parent / "linux_telemetry_dashboard.py"
    if not f.exists():
        raise HTTPException(status_code=404, detail="linux_telemetry_dashboard.py not found")
    return PlainTextResponse(content=f.read_text(encoding="utf-8"),
                             media_type="text/plain; charset=utf-8")


@router.get("/download-linux-bundle")
async def download_linux_bundle():
    """Serve linux-telemetry-agent.zip — pre-built script bundle for manual installs."""
    root = Path(__file__).parent.parent.parent
    zp = root / "dist" / "linux-telemetry-agent.zip"
    if zp.exists():
        return FileResponse(str(zp), media_type="application/zip",
                            filename="linux-telemetry-agent.zip")
    raise HTTPException(
        status_code=404,
        detail="Linux bundle not built yet. Run: .\\build-all.ps1 -SkipWindows",
    )
