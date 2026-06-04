$env:AZURE_STORAGE_CONNECTION_STRING = "UseDevelopmentStorage=true"
$env:AGENT_API_KEY = "dev-agent-key-123"
$env:ADMIN_API_KEY = "dev-admin-key-123"
$env:JWT_SECRET = "dev-jwt-secret-123"
$env:ALLOWED_ORIGINS = "http://localhost:8000,http://127.0.0.1:8000"
$py = "C:\Users\MohamedAshmar\playground\User-tracking-tool\user-track\Scripts\python.exe"
$wd = "C:\Users\MohamedAshmar\playground\User-tracking-tool"
$log = "C:\Users\MohamedAshmar\playground\User-tracking-tool\backend.log"
Write-Host "Starting FastAPI backend, logging to $log ..."
# Use cmd /c start /B to fully detach
cmd /c "start /B cmd /c `"cd /d $wd && set AZURE_STORAGE_CONNECTION_STRING=UseDevelopmentStorage=true&& set AGENT_API_KEY=dev-agent-key-123&& set ADMIN_API_KEY=dev-admin-key-123&& set JWT_SECRET=dev-jwt-secret-123&& set ALLOWED_ORIGINS=http://localhost:8000,http://127.0.0.1:8000&& $py -m uvicorn backend.main:app --host 127.0.0.1 --port 8000 > $log 2>&1`"" | Out-Null
Start-Sleep -Seconds 6
Write-Host "Done waiting. Listening ports:"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 10000,10001,10002,8000 } |
    Format-Table LocalPort, LocalAddress, State, OwningProcess -AutoSize
Write-Host "Backend log tail:"
if (Test-Path $log) { Get-Content $log -Tail 20 }
