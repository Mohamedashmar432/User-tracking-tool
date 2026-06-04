$az = "C:\Users\MohamedAshmar\AppData\Roaming\npm\node_modules\azurite\dist\src\azurite.js"
$wd = "C:\Users\MohamedAshmar\playground\User-tracking-tool"
if (-not (Test-Path $az)) { Write-Error "azurite.js not found at $az"; exit 1 }
Write-Host "Starting Azurite from $az in $wd ..."
# Use cmd's `start /B` so the node process fully detaches and the script can return
cmd /c "start /B node `"$az`" --location `"$wd`" --silent --debug C:\Users\MohamedAshmar\playground\User-tracking-tool\azurite.log" | Out-Null
Start-Sleep -Seconds 4
Write-Host "Done waiting. Listening ports:"
Get-NetTCPConnection -State Listen -ErrorAction SilentlyContinue |
    Where-Object { $_.LocalPort -in 10000,10001,10002 } |
    Format-Table LocalPort, LocalAddress, State, OwningProcess -AutoSize
