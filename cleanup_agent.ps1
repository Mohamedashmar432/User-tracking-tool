# Telemetry Agent - Manual Cleanup Script
# Run this on the target machine to completely remove all agent components.
# Self-elevates to Administrator automatically.

$ErrorActionPreference = 'SilentlyContinue'

if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    Start-Process powershell -ArgumentList "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`"" -Verb RunAs
    exit
}

Write-Host ""
Write-Host "=== Telemetry Agent Manual Cleanup ===" -ForegroundColor Cyan
Write-Host ""

Write-Host "[1/4] Removing scheduled tasks..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryAgent         /f 2>$null
schtasks /delete /tn TelemetryAgentWatchdog /f 2>$null
schtasks /delete /tn TelemetryUI            /f 2>$null
Write-Host "    Done" -ForegroundColor Green

Write-Host "[2/4] Killing processes..." -ForegroundColor Cyan
Stop-Process -Name telemetry_agent -Force -ErrorAction SilentlyContinue
Stop-Process -Name telemetry_ui    -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Write-Host "    Done" -ForegroundColor Green

Write-Host "[3/4] Removing registry autostart entries..." -ForegroundColor Cyan
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TelemetryAgent" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKCU:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TelemetryUI"    -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TelemetryAgent" -ErrorAction SilentlyContinue
Remove-ItemProperty -Path "HKLM:\Software\Microsoft\Windows\CurrentVersion\Run" -Name "TelemetryUI"    -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host "[4/4] Removing files and directories..." -ForegroundColor Cyan
Remove-Item -Recurse -Force "C:\Program Files\TelemetryAgent"  -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\ProgramData\TelemetryAgent"    -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "C:\Program Files\TelemetryUI"     -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:APPDATA\TelemetryUI"         -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\TelemetryUI"    -ErrorAction SilentlyContinue
$tmp = [System.IO.Path]::GetTempPath()
Remove-Item -Recurse -Force ($tmp + "TelemetryAgent")          -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force ($tmp + "telemetry_backup")        -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force ($tmp + "pa_install.ps1")          -ErrorAction SilentlyContinue
Remove-Item -Recurse -Force ($tmp + "pa_uninstall.ps1")        -ErrorAction SilentlyContinue
Write-Host "    Done" -ForegroundColor Green

Write-Host ""
Write-Host "==================================================" -ForegroundColor Green
Write-Host " Cleanup complete. All agent files removed."        -ForegroundColor Green
Write-Host "==================================================" -ForegroundColor Green
Write-Host ""
Read-Host "Press Enter to close"
