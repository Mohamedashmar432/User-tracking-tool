$ErrorActionPreference = "Continue"

function Test-Endpoint($name, $script) {
    Write-Host ""
    Write-Host "=== $name ===" -ForegroundColor Cyan
    try {
        & $script
    } catch {
        Write-Host "  FAIL: $($_.Exception.Message)" -ForegroundColor Red
    }
}

Test-Endpoint "GET /api/health (no auth)" {
    $h = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/health" -TimeoutSec 10
    $h | Format-List
    Write-Host "  version field present: $($h.ContainsKey('version'))" -ForegroundColor Yellow
    Write-Host "  agent_zip_download_url: $($h.agent_zip_download_url)" -ForegroundColor Yellow
    Write-Host "  ui_zip_download_url:    $($h.ui_zip_download_url)" -ForegroundColor Yellow
    Write-Host "  linux_agent_download_url: $($h.linux_agent_download_url)" -ForegroundColor Yellow
    Write-Host "  linux_ui_download_url:    $($h.linux_ui_download_url)" -ForegroundColor Yellow
}

Test-Endpoint "POST /ingest (valid)" {
    $today = (Get-Date -AsUTC).ToString("yyyy-MM-dd")
    $body = @{
        user   = "testuser"
        device = "E813-test"
        events = @(
            @{ app = "code.exe"; domain = "";       active = $true;  duration = 600; timestamp = (Get-Date -AsUTC).AddMinutes(-30).ToString("o"); locked = $false }
            @{ app = "code.exe"; domain = "";       active = $true;  duration = 300; timestamp = (Get-Date -AsUTC).AddMinutes(-25).ToString("o"); locked = $false }
            @{ app = "brave.exe"; domain = "youtube.com"; active = $true; duration = 180; timestamp = (Get-Date -AsUTC).AddMinutes(-20).ToString("o"); locked = $false }
        )
    } | ConvertTo-Json -Depth 5
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/ingest" -Method Post -Body $body -ContentType "application/json" -Headers @{"X-API-Key"="dev-agent-key-123"} -TimeoutSec 15
    $r | Format-List
}

Test-Endpoint "GET /api/me/summary (no auth -> 401)" {
    try {
        $null = Invoke-WebRequest -Uri "http://127.0.0.1:8000/api/me/summary?user=testuser&date=2026-06-03" -TimeoutSec 10
        Write-Host "  UNEXPECTED 2xx" -ForegroundColor Red
    } catch {
        Write-Host "  status: $($_.Exception.Response.StatusCode.value__) (expected 401)" -ForegroundColor Yellow
    }
}

Test-Endpoint "GET /api/me/summary (admin key + user)" {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/me/summary?user=testuser&date=2026-06-03" -Headers @{"X-API-Key"="dev-admin-key-123"} -TimeoutSec 10
    $r | Format-List
}

Test-Endpoint "GET /api/me/apps (admin key)" {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/me/apps?user=testuser&date=2026-06-03" -Headers @{"X-API-Key"="dev-admin-key-123"} -TimeoutSec 10
    $r | Format-Table app, time, category -AutoSize | Out-String | Write-Host
}

Test-Endpoint "GET /api/me/timeline (admin key)" {
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/me/timeline?user=testuser&date=2026-06-03" -Headers @{"X-API-Key"="dev-admin-key-123"} -TimeoutSec 10
    Write-Host "  events returned: $($r.Count)"
    if ($r.Count -gt 0) { $r[0] | Format-List }
}

Test-Endpoint "GET /api/user-range (admin key) - 7 days" {
    $end   = (Get-Date).ToString("yyyy-MM-dd")
    $start = (Get-Date).AddDays(-6).ToString("yyyy-MM-dd")
    $r = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/user-range?user=testuser&start=$start&end=$end" -Headers @{"X-API-Key"="dev-admin-key-123"} -TimeoutSec 15
    Write-Host "  daily count: $($r.daily.Count)" -ForegroundColor Yellow
    $r.daily | Format-Table date, day_name, active_hours, productive_hours, productivity_score -AutoSize | Out-String | Write-Host
    Write-Host "  summary: $($r.summary | ConvertTo-Json -Compress)" -ForegroundColor Yellow
}

Test-Endpoint "GET /download-agent-zip (no env override, local file)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/download-agent-zip" -Method Head -TimeoutSec 10
    Write-Host "  status: $($r.StatusCode), content-length: $($r.Headers['Content-Length'])" -ForegroundColor Yellow
}

Test-Endpoint "GET /download-ui (no env override, local file)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/download-ui" -Method Head -TimeoutSec 10
    Write-Host "  status: $($r.StatusCode), content-length: $($r.Headers['Content-Length'])" -ForegroundColor Yellow
}

Test-Endpoint "GET /download-linux-agent (no env override, local file)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/download-linux-agent" -Method Head -TimeoutSec 10
    Write-Host "  status: $($r.StatusCode), content-length: $($r.Headers['Content-Length'])" -ForegroundColor Yellow
}

Test-Endpoint "GET /install-script-linux (bash script)" {
    $r = Invoke-WebRequest -Uri "http://127.0.0.1:8000/install-script-linux" -TimeoutSec 10
    Write-Host "  status: $($r.StatusCode), length: $($r.Content.Length), first line: $((($r.Content -split "`n")[0]).Substring(0, [Math]::Min(80, $r.Content.Length)))" -ForegroundColor Yellow
}
