# One-liner uninstall — can be run as:
# powershell -ExecutionPolicy Bypass -File uninstall-agent.ps1
# or copy the inner script as a single line for the one-liner format.

#Requires -RunAsAdministrator

$ErrorActionPreference = 'SilentlyContinue'

Write-Host "Uninstalling TelemetryAgent..." -ForegroundColor Cyan

# Stop process
Get-Process -Name "TelemetryAgent","telemetry_agent" | Stop-Process -Force
Write-Host "  Stopped agent process"

# Remove scheduled task
schtasks /delete /tn "TelemetryAgent" /f 2>$null
Write-Host "  Removed scheduled task"

# Remove program files
Remove-Item "C:\Program Files\TelemetryAgent" -Recurse -Force
Remove-Item "C:\ProgramData\TelemetryAgent"   -Recurse -Force
Write-Host "  Removed program files and config"

# Remove offline backup cache
Remove-Item "$env:TEMP\telemetry_backup" -Recurse -Force
Write-Host "  Removed backup cache"

Write-Host "TelemetryAgent uninstalled successfully." -ForegroundColor Green
