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
    """PowerShell one-liner installer — installs agent + UI companion."""
    base = _public_base(request)
    script = f"""\
# ============================================================
#  ProdAnalytics - Full Installer (Agent + UI Companion)
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    $tmp = "$env:TEMP\\install_prodanalytics.ps1"
    Invoke-WebRequest -Uri "$ServerUrl/install-script" -OutFile $tmp -UseBasicParsing
    Start-Process PowerShell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== ProdAnalytics Installer ===" -ForegroundColor Cyan
Write-Host "Server : $ServerUrl"
Write-Host ""

# ── Pre-approve install paths with Windows Defender ──────────────────────
Write-Host "Configuring Windows Defender exclusions..." -ForegroundColor Gray
Add-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryAgent"  -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryUI"     -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionPath    "C:\\ProgramData\\TelemetryAgent"    -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionPath    "$env:TEMP\\telemetry_agent.exe"     -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "telemetry_agent.exe"                -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "telemetry_ui.exe"                   -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "      OK" -ForegroundColor Green

# ── Step 1: Download and install agent ───────────────────────────────────
Write-Host "[1/4] Downloading agent..." -ForegroundColor Cyan
$AgentZip = "$env:TEMP\\telemetry_agent.zip"
$AgentDir = "C:\\Program Files\\TelemetryAgent"
Invoke-WebRequest -Uri "$ServerUrl/download-agent-zip" -OutFile $AgentZip -UseBasicParsing
Unblock-File -Path $AgentZip -ErrorAction SilentlyContinue
if (-not (Test-Path $AgentDir)) {{ New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null }}
Expand-Archive -Path $AgentZip -DestinationPath $AgentDir -Force
Get-ChildItem -Path $AgentDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $AgentZip -Force -ErrorAction SilentlyContinue
Write-Host "      OK" -ForegroundColor Green

# ── Step 2: Run agent --install (writes config + registers scheduled task) ──
Write-Host "[2/4] Installing agent..." -ForegroundColor Cyan
& "$AgentDir\\telemetry_agent.exe" --install --server-url $ServerUrl
Write-Host "      Agent installed and started" -ForegroundColor Green

# ── Step 3: Download and install UI companion ─────────────────────────────
Write-Host "[3/4] Downloading UI companion..." -ForegroundColor Cyan
$UiZip  = "$env:TEMP\\telemetry_ui.zip"
$UiDir  = "C:\\Program Files\\TelemetryUI"
$UiExe  = "$UiDir\\telemetry_ui.exe"
Invoke-WebRequest -Uri "$ServerUrl/download-ui" -OutFile $UiZip -UseBasicParsing
Unblock-File -Path $UiZip -ErrorAction SilentlyContinue
if (-not (Test-Path $UiDir)) {{ New-Item -ItemType Directory -Path $UiDir -Force | Out-Null }}
Expand-Archive -Path $UiZip -DestinationPath $UiDir -Force
Get-ChildItem -Path $UiDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $UiZip -Force -ErrorAction SilentlyContinue
Write-Host "      Saved to $UiDir" -ForegroundColor Green

# ── Step 4: Register UI autostart task + launch now ───────────────────────
Write-Host "[4/4] Setting up UI companion autostart..." -ForegroundColor Cyan
try {{ schtasks /delete /tn "TelemetryUI" /f 2>&1 | Out-Null }} catch {{}}
$action  = New-ScheduledTaskAction  -Execute $UiExe
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "TelemetryUI" -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
Write-Host "      Registered startup task 'TelemetryUI'" -ForegroundColor Green

# Launch the UI for the current session immediately (no logout needed)
Start-Process $UiExe
Write-Host "      UI companion launched" -ForegroundColor Green

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Installation complete!" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " Agent  : C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe" -ForegroundColor White
Write-Host " UI     : $UiExe" -ForegroundColor White
Write-Host " Server : $ServerUrl" -ForegroundColor White
Write-Host " Log    : C:\\ProgramData\\TelemetryAgent\\logs.txt" -ForegroundColor Gray
Write-Host ""
Write-Host " Both will start automatically at every Windows login." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="install_prodanalytics.ps1"'},
    )


@router.get("/uninstall-script")
async def uninstall_script(request: Request):
    """PowerShell uninstall script."""
    base = _public_base(request)
    script = f"""\
# ============================================================
#  Telemetry Agent - Uninstaller
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $tmp = "$env:TEMP\\uninstall_prodanalytics.ps1"
    Invoke-WebRequest -Uri "{base}/uninstall-script" -OutFile $tmp -UseBasicParsing
    Start-Process PowerShell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== Telemetry Agent Uninstaller ===" -ForegroundColor Cyan
Write-Host ""

$AgentExe = "C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"

if (Test-Path $AgentExe) {{
    Write-Host "[1/4] Running built-in uninstall..." -ForegroundColor Cyan
    & $AgentExe --uninstall
    Start-Sleep -Seconds 3
}} else {{
    Write-Host "[1/4] Agent EXE not found — stopping processes..." -ForegroundColor Yellow
    schtasks /delete /tn TelemetryAgent /f 2>$null
    Stop-Process -Name telemetry_agent -Force 2>$null
    Start-Sleep -Seconds 3
}}

# Force-delete install dirs — the EXE could not delete its own folder while running
$tmp2 = [System.IO.Path]::GetTempPath()
Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryAgent"  -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\\ProgramData\\TelemetryAgent"    -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$tmp2\\TelemetryAgent"              -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$tmp2\\telemetry_backup"            -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "[2/4] Removing UI companion..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
Stop-Process -Name telemetry_ui -Force 2>$null
Start-Sleep -Seconds 2
$uiPaths = @(
    "C:\\Program Files\\TelemetryUI",
    "$env:APPDATA\\TelemetryUI",
    "$env:LOCALAPPDATA\\TelemetryUI"
)
foreach ($p in $uiPaths) {{ Remove-Item -Recurse -Force $p 2>$null }}

Write-Host "[3/4] Removing Windows Defender exclusions..." -ForegroundColor Cyan
Remove-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryAgent"  -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryUI"     -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionPath    "C:\\ProgramData\\TelemetryAgent"    -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess "telemetry_agent.exe"                -ErrorAction SilentlyContinue
Remove-MpPreference -ExclusionProcess "telemetry_ui.exe"                   -ErrorAction SilentlyContinue

Write-Host "[4/4] Cleanup complete." -ForegroundColor Green
Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host "  Telemetry Agent uninstalled successfully." -ForegroundColor Green
Write-Host "  All local files, tasks, and exclusions removed." -ForegroundColor Green
Write-Host "  Employee data in the cloud is not affected." -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="uninstall_agent.ps1"'},
    )


@router.get("/migrate-script")
async def migrate_script(request: Request):
    """
    Migration installer — kills old processes, removes old install, runs fresh install.
    Use when an old agent is already on the machine.
    """
    base = _public_base(request)
    script = f"""\
# ============================================================
#  ProdAnalytics - Migration Installer
#  Stops old agent, removes old install, installs fresh.
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    $tmp = "$env:TEMP\\migrate_prodanalytics.ps1"
    Invoke-WebRequest -Uri "$ServerUrl/migrate-script" -OutFile $tmp -UseBasicParsing
    Start-Process PowerShell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$tmp`"" -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== ProdAnalytics Migration Installer ===" -ForegroundColor Cyan
Write-Host "Server : $ServerUrl"
Write-Host ""

# ── Step 1: Remove scheduled tasks FIRST (prevents auto-restart after stop) ──
Write-Host "[1/5] Removing scheduled tasks..." -ForegroundColor Cyan
$agentTaskNames = @("TelemetryAgent", "TelemetryUI", "ProdAnalytics", "telemetry_agent", "telemetry_ui")
foreach ($t in $agentTaskNames) {{
    try {{ Unregister-ScheduledTask -TaskName $t -Confirm:$false }} catch {{}}
}}
Write-Host "      Done" -ForegroundColor Green

# ── Step 2: Stop running agent processes ──────────────────────────────────────
Write-Host "[2/5] Stopping old agent..." -ForegroundColor Cyan
$agentProcessNames = @("telemetry_agent", "telemetry_ui")
foreach ($name in $agentProcessNames) {{
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    if ($procs) {{
        Write-Host "      Stopping $name ($($procs.Count) process(es))..." -ForegroundColor Gray
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    }}
}}
Start-Sleep -Seconds 3

# Verify stopped — retry with Stop-Process if any remain
foreach ($name in $agentProcessNames) {{
    $procs = Get-Process -Name $name -ErrorAction SilentlyContinue
    if ($procs) {{
        Write-Host "      Still running — retrying stop for $name..." -ForegroundColor Yellow
        $procs | Stop-Process -Force -ErrorAction SilentlyContinue
    }}
}}
Start-Sleep -Seconds 3

# Run --uninstall to clean registry/tasks (safe since processes are stopped)
$AgentExe = "C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"
if (Test-Path $AgentExe) {{
    Write-Host "      Running cleanup..." -ForegroundColor Gray
    $proc = Start-Process -FilePath $AgentExe -ArgumentList "--uninstall" -PassThru -Wait -WindowStyle Hidden
    Write-Host "      Cleanup exited ($($proc.ExitCode))" -ForegroundColor Gray
    Start-Sleep -Seconds 2
}}

# Final check — stop any processes that --uninstall may have left
foreach ($name in $agentProcessNames) {{
    Get-Process -Name $name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
}}
Start-Sleep -Seconds 2
Write-Host "      Done" -ForegroundColor Green

# ── Step 3: Delete old installation directories ───────────────────────────────
Write-Host "[3/5] Removing old files..." -ForegroundColor Cyan
$dirsToRemove = @(
    "C:\\Program Files\\TelemetryAgent",
    "C:\\Program Files\\TelemetryUI",
    "C:\\ProgramData\\TelemetryAgent",
    "$env:APPDATA\\TelemetryAgent",
    "$env:APPDATA\\TelemetryUI",
    "$env:LOCALAPPDATA\\TelemetryAgent",
    "$env:LOCALAPPDATA\\TelemetryUI",
    "$env:TEMP\\TelemetryAgent",
    "$env:TEMP\\telemetry_backup"
)
foreach ($d in $dirsToRemove) {{
    if (-not (Test-Path $d)) {{ continue }}
    Write-Host "      Removing: $d" -ForegroundColor Gray
    for ($i = 0; $i -lt 5; $i++) {{
        Remove-Item -Recurse -Force $d -ErrorAction SilentlyContinue
        if (-not (Test-Path $d)) {{ break }}
        Start-Sleep -Seconds 2
    }}
    if (Test-Path $d) {{
        Write-Host "      WARNING: could not fully remove $d" -ForegroundColor Yellow
    }}
}}
Remove-Item -Force "$env:TEMP\\telemetry_agent*.zip" -ErrorAction SilentlyContinue
Remove-Item -Force "$env:TEMP\\telemetry_ui*.zip"    -ErrorAction SilentlyContinue
Remove-Item -Force "$env:TEMP\\updater*.ps1"         -ErrorAction SilentlyContinue
Write-Host "      Done" -ForegroundColor Green

# ── Step 4: Prepare install paths ────────────────────────────────────────────
Write-Host "[4/5] Preparing environment..." -ForegroundColor Cyan
Add-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryAgent"  -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionPath    "C:\\Program Files\\TelemetryUI"     -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionPath    "C:\\ProgramData\\TelemetryAgent"    -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "telemetry_agent.exe"                -ErrorAction SilentlyContinue
Add-MpPreference -ExclusionProcess "telemetry_ui.exe"                   -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "      Done" -ForegroundColor Green

# ── Step 5: Fresh install ─────────────────────────────────────────────────────
Write-Host "[5/5] Running fresh install..." -ForegroundColor Cyan
Write-Host ""

$AgentZip = "$env:TEMP\\telemetry_agent.zip"
$AgentDir = "C:\\Program Files\\TelemetryAgent"
Write-Host "      Downloading agent..." -ForegroundColor Gray
try {{
    Invoke-WebRequest -Uri "$ServerUrl/download-agent-zip" -OutFile $AgentZip -UseBasicParsing
}} catch {{
    Write-Host "ERROR: Failed to download agent: $_" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}}
Unblock-File -Path $AgentZip -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $AgentDir -Force | Out-Null
try {{
    Expand-Archive -Path $AgentZip -DestinationPath $AgentDir -Force
}} catch {{
    Write-Host "ERROR: Failed to extract agent ZIP: $_" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}}
Get-ChildItem -Path $AgentDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $AgentZip -Force -ErrorAction SilentlyContinue

Write-Host "      Installing agent..." -ForegroundColor Gray
& "$AgentDir\\telemetry_agent.exe" --install --server-url $ServerUrl

$UiZip = "$env:TEMP\\telemetry_ui.zip"
$UiDir = "C:\\Program Files\\TelemetryUI"
$UiExe = "$UiDir\\telemetry_ui.exe"
Write-Host "      Downloading UI companion..." -ForegroundColor Gray
try {{
    Invoke-WebRequest -Uri "$ServerUrl/download-ui" -OutFile $UiZip -UseBasicParsing
}} catch {{
    Write-Host "ERROR: Failed to download UI: $_" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}}
Unblock-File -Path $UiZip -ErrorAction SilentlyContinue
New-Item -ItemType Directory -Path $UiDir -Force | Out-Null
try {{
    Expand-Archive -Path $UiZip -DestinationPath $UiDir -Force
}} catch {{
    Write-Host "ERROR: Failed to extract UI ZIP: $_" -ForegroundColor Red
    Read-Host "Press Enter to close"
    exit 1
}}
Get-ChildItem -Path $UiDir -Recurse | Unblock-File -ErrorAction SilentlyContinue
Remove-Item $UiZip -Force -ErrorAction SilentlyContinue

try {{ Unregister-ScheduledTask -TaskName "TelemetryUI" -Confirm:$false }} catch {{}}
$uiAction   = New-ScheduledTaskAction  -Execute $UiExe
$uiTrigger  = New-ScheduledTaskTrigger -AtLogOn
$uiSettings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "TelemetryUI" -Action $uiAction -Trigger $uiTrigger -Settings $uiSettings -RunLevel Limited -Force | Out-Null
Start-Process $UiExe

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Migration complete!" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " Agent  : C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe" -ForegroundColor White
Write-Host " UI     : $UiExe" -ForegroundColor White
Write-Host " Server : $ServerUrl" -ForegroundColor White
Write-Host " Log    : C:\\ProgramData\\TelemetryAgent\\logs.txt" -ForegroundColor Gray
Write-Host ""
Write-Host " Both will start automatically at every Windows login." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="migrate_prodanalytics.ps1"'},
    )
