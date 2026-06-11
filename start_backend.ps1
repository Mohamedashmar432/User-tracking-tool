$ErrorActionPreference = "Stop"

$AzuriteJs = "C:\Users\MohamedAshmar\AppData\Roaming\npm\node_modules\azurite\dist\src\azurite.js"
$py = "C:\Users\MohamedAshmar\playground\User-tracking-tool\user-track\Scripts\python.exe"
$wd = "C:\Users\MohamedAshmar\playground\User-tracking-tool"
$log = "C:\Users\MohamedAshmar\playground\User-tracking-tool\backend.log"

function Test-Port($port) {
    $c = New-Object System.Net.Sockets.TcpClient
    try { $c.Connect("127.0.0.1", $port); return $true } catch { return $false } finally { $c.Dispose() }
}

# 1. Azurite
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
    $azInfo.Arguments              = "`"$AzuriteJs`" --location `"$wd`" --silent"
    $azInfo.WorkingDirectory       = $wd
    $azInfo.UseShellExecute        = $false
    $azInfo.RedirectStandardOutput = $true
    $azInfo.RedirectStandardError  = $true
    $azInfo.CreateNoWindow         = $true
    $script:azProc = [System.Diagnostics.Process]::Start($azInfo)
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
    Write-Host "      Azurite UP (PID $($script:azProc.Id)) -- ports 10000/10001/10002" -ForegroundColor Green
}

# 2. FastAPI
$env:AZURE_STORAGE_CONNECTION_STRING = "UseDevelopmentStorage=true"
$env:AGENT_API_KEY = "dev-agent-key-123"
$env:ADMIN_API_KEY = "dev-admin-key-123"
$env:JWT_SECRET = "dev-jwt-secret-123"
$env:ALLOWED_ORIGINS = "http://localhost:8000,http://127.0.0.1:8000"
Write-Host "[2/2] Starting FastAPI backend, logging to $log ..."
cmd /c "start /B cmd /c `"cd /d $wd && set AZURE_STORAGE_CONNECTION_STRING=UseDevelopmentStorage=true&& set AGENT_API_KEY=dev-agent-key-123&& set ADMIN_API_KEY=dev-admin-key-123&& set JWT_SECRET=dev-jwt-secret-123&& set ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000&& $py -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 > $log 2>&1`"" | Out-Null
Start-Sleep -Seconds 6
Write-Host "Done waiting. Listening ports:"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 10000,10001,10002,8000 } |
    Format-Table LocalPort, LocalAddress, State, OwningProcess -AutoSize
Write-Host "Backend log tail:"
if (Test-Path $log) { Get-Content $log -Tail 20 }
