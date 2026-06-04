Stop-Process -Name uvicorn -Force -ErrorAction SilentlyContinue
Stop-Process -Name python -Force -ErrorAction SilentlyContinue
Start-Sleep -Seconds 2
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 10000,10001,10002,8000 } |
    Format-Table LocalPort, OwningProcess -AutoSize
Write-Host "Stop complete."
