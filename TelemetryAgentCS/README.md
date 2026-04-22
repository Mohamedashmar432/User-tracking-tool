# TelemetryAgent — C# WPF Edition

## Build

```powershell
cd TelemetryAgentCS\TelemetryAgent
dotnet publish -c Release -r win-x64 --self-contained true /p:PublishSingleFile=true
# Output: bin\Release\net8.0-windows\win-x64\publish\TelemetryAgent.exe
```

## Install

```powershell
# Run as Administrator
.\TelemetryAgent.exe --install https://your-server.azurewebsites.net
```

## Uninstall (one-liner)

```powershell
powershell -ExecutionPolicy Bypass -Command "Stop-Process -Name TelemetryAgent -Force -EA SilentlyContinue; schtasks /delete /tn TelemetryAgent /f 2>$null; Remove-Item 'C:\Program Files\TelemetryAgent','C:\ProgramData\TelemetryAgent' -Recurse -Force -EA SilentlyContinue; Remove-Item \"$env:TEMP\telemetry_backup\" -Recurse -Force -EA SilentlyContinue; Write-Host 'Uninstalled.'"
```

## Architecture

```
App.xaml.cs          — startup, single-instance mutex, wires all components
Config.cs            — loads C:\ProgramData\TelemetryAgent\config.json
Tracking/
  Win32.cs           — all P/Invoke declarations (no external DLLs)
  WindowTracker.cs   — SetWinEventHook for instant app-switch (0ms latency)
  IdleDetector.cs    — GetLastInputInfo idle seconds
  LockDetector.cs    — SystemEvents.SessionSwitch (instant lock/unlock)
Data/
  TelemetryEvent.cs  — event + API response data classes
  EventBuffer.cs     — thread-safe buffer, flush on state change + every 30s
  ApiClient.cs       — HttpClient posting to /ingest, GET /api/* for popup
  BackupManager.cs   — offline disk backup/replay (%TEMP%/telemetry_backup)
SystemTray/
  TrayManager.cs     — NotifyIcon, context menu, tooltip with live status
UI/
  PopupWindow.xaml   — dark WPF popup: score, donut chart, 24h bar chart
  PopupWindow.xaml.cs— code-behind, chart drawing, auto-refresh every 30s
Install/
  Installer.cs       — install (dirs + config + task + launch) / uninstall
```

## Key Improvements Over Python Agent

| Area | Python | C# |
|---|---|---|
| App-switch detection | Poll every 5s | SetWinEventHook (~50ms) |
| Event interval | 60s | 15s |
| Flush interval | 120s | 30s + on every state change |
| Max status lag | ~3 min | ~30s |
| Lock detection | 3 Win32 checks on timer | SessionSwitch event (instant) |
| UI | None | System tray + popup window |
| Idle detection | Same (GetLastInputInfo) | Same |

## Config (C:\ProgramData\TelemetryAgent\config.json)

```json
{
  "ingest_url":     "https://your-server.azurewebsites.net/ingest",
  "api_key":        "your-agent-api-key",
  "idle_threshold": 300,
  "event_interval": 15,
  "flush_interval": 30,
  "batch_size":     20
}
```
