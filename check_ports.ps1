Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 10000,10001,10002,8000,5000,5001,5002,5003 } |
    Format-Table LocalPort, LocalAddress, State, OwningProcess -AutoSize
