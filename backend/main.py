"""
main.py — FastAPI analytics server.

Responsibilities
----------------
POST /ingest          receive raw batches from agents, write to RawTelemetry as-is
GET  /api/users       list known users from UserIndex
GET  /api/user-summary  aggregate KPIs on demand from raw rows
GET  /api/user-apps     per-app usage totals
GET  /api/user-timeline merged time-series for charts

All aggregation is done at READ time inside aggregator.py.
No enrichment or aggregation happens at write time.

Run
---
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import uvicorn
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from .storage   import TelemetryStorage
from .aggregator import aggregate_summary, aggregate_apps, build_timeline

# ── Version ──────────────────────────────────────────────────────────────────────
# Bump APP_VERSION before every deploy — the dashboard reads this from /api/health
# so you can instantly confirm the new build is live.

APP_VERSION = "2.2"
STARTED_AT  = datetime.now(timezone.utc).isoformat()

# ── App setup ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Telemetry Analytics API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

storage = TelemetryStorage()


# ── URL helper ──────────────────────────────────────────────────────────────────

def _public_base(request: Request) -> str:
    """
    Return the public-facing base URL (scheme + host, no trailing slash).

    Azure App Service terminates TLS at the load balancer and forwards plain
    HTTP to the container, so request.base_url always reports http://.
    The X-Forwarded-Proto header carries the real client-facing scheme (https).
    """
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host  = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


# ── Ingest (write path) ─────────────────────────────────────────────────────────

class IngestPayload(BaseModel):
    user:   str
    device: str = ""
    events: List[Dict[str, Any]]


@app.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload):
    """
    Receive a batch of raw telemetry events from the agent.
    Writes each event as one row in RawTelemetry — no enrichment, no aggregation.
    Idempotent: retried batches produce the same rows (upsert).
    """
    if not payload.events:
        raise HTTPException(status_code=400, detail="Empty event batch")

    written = storage.write_raw_batch(payload.user, payload.device, payload.events)
    return {"accepted": written, "total": len(payload.events)}


# ── Dashboard (read path) ───────────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    """Liveness check used by the agent on startup and during --install."""
    return {"status": "ok", "service": "telemetry-analytics", "version": APP_VERSION, "started_at": STARTED_AT}


@app.get("/agent-config")
async def agent_config(request: Request):
    """
    Return the canonical server URL so the agent can self-configure.
    The agent calls this during --install to write its config.json.
    """
    base = _public_base(request)
    return {"server_url": base, "ingest_url": f"{base}/ingest"}


@app.get("/download-agent")
async def download_agent():
    """
    Serve the pre-built agent EXE.

    Production (Render / Linux): set the AGENT_DOWNLOAD_URL env var to wherever
    the EXE is hosted (Azure Blob, GitHub Releases, etc.) — the server will
    redirect the browser there.

    Local dev: falls back to dist/telemetry_agent.exe built by PyInstaller.
    """
    redirect_url = os.getenv("AGENT_DOWNLOAD_URL")
    if redirect_url:
        return RedirectResponse(url=redirect_url)

    exe = Path("dist/telemetry_agent.exe")
    if not exe.exists():
        raise HTTPException(
            status_code=404,
            detail=(
                "Set AGENT_DOWNLOAD_URL env var to point to the hosted EXE, "
                "or build locally with: pyinstaller telemetry_agent.spec"
            ),
        )
    return FileResponse(
        str(exe),
        media_type="application/octet-stream",
        filename="telemetry_agent.exe",
    )


@app.get("/install-script")
async def install_script(request: Request):
    """
    Returns a PowerShell installer script with THIS server's URL already embedded.

    The script:
      1. Self-elevates to Administrator via UAC if needed
      2. Downloads telemetry_agent.exe from this server
      3. Runs  telemetry_agent.exe --install --server-url <this-server>
      4. Agent writes config, registers the scheduled task, starts at next logon

    Dashboard shows the one-liner:
      powershell -ExecutionPolicy Bypass -Command "irm <server>/install-script | iex"
    """
    base = _public_base(request)

    # IMPORTANT: keep this script ASCII-only and all PowerShell expressions on
    # single lines. Unicode characters (em-dashes, box-drawing) and multi-line
    # expressions break PowerShell's parser when the script is piped through iex.
    script = f"""\
# ============================================================
#  Telemetry Agent - one-click installer
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'
$ExePath   = "$env:TEMP\\telemetry_agent.exe"

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    $cmd = "-NoProfile -ExecutionPolicy Bypass -Command `"irm '$ServerUrl/install-script' | iex`""
    Start-Process PowerShell -ArgumentList $cmd -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== Telemetry Agent Installer ===" -ForegroundColor Cyan
Write-Host "Server : $ServerUrl"
Write-Host ""

# Step 1: Download EXE
Write-Host "[1/2] Downloading agent..." -ForegroundColor Cyan
Invoke-WebRequest -Uri "$ServerUrl/download-agent" -OutFile $ExePath -UseBasicParsing
Write-Host "      Saved to $ExePath" -ForegroundColor Green

# Step 2: Install (dirs + config + scheduled task)
Write-Host "[2/2] Installing..." -ForegroundColor Cyan
& $ExePath --install --server-url $ServerUrl

Write-Host ""
Write-Host "Installation complete!" -ForegroundColor Green
Write-Host "The agent will start automatically at the next Windows login."
Write-Host "Log: C:\\ProgramData\\TelemetryAgent\\agent.log" -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="install_agent.ps1"'},
    )


_INDEX = Path(__file__).parent.parent / "index.html"

@app.get("/")
async def index():
    return FileResponse(str(_INDEX))


@app.get("/api/users")
async def get_users():
    """List all known users. O(users) via UserIndex, not O(events)."""
    try:
        return [{"user": u} for u in storage.get_all_users()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user-summary")
async def get_user_summary(user: str, date: str):
    """
    Fetch raw rows for user+date then aggregate on demand.
    Returns: total_active_time, total_idle_time, productivity_score, top_app.
    """
    try:
        events = storage.get_raw_events(user, date)
        if not events:
            raise HTTPException(status_code=404, detail=f"No data for {user} on {date}")
        return {"user": user, "date": date, **aggregate_summary(events)}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user-apps")
async def get_user_apps(user: str, date: str):
    """Per-app active-time totals with productivity category, sorted by time."""
    try:
        events = storage.get_raw_events(user, date)
        return aggregate_apps(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user-timeline")
async def get_user_timeline(user: str, date: str):
    """Merged time-series of app events for charting."""
    try:
        events = storage.get_raw_events(user, date)
        return build_timeline(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin: data deletion ────────────────────────────────────────────────────────

@app.delete("/api/user")
async def delete_user(user: str):
    """
    Delete ALL data for a user — every event row in RawTelemetry and
    the UserIndex entry.  The user disappears from the dashboard immediately.
    """
    try:
        deleted = storage.delete_user(user)
        return {"user": user, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/user-date")
async def delete_user_date(user: str, date: str):
    """
    Delete all events for a user on a specific date (YYYY-MM-DD).
    Useful for wiping test/bad data while keeping other days intact.
    """
    try:
        deleted = storage.delete_user_date(user, date)
        return {"user": user, "date": date, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Future: Daily summary Azure Function (placeholder) ─────────────────────────
#
# Deploy a Timer-triggered Azure Function (cron: "0 0 0 * * *") that:
#   1. Iterates users from UserIndex
#   2. Calls storage.get_raw_events(user, yesterday) for each
#   3. Calls aggregate_summary() + aggregate_apps() from aggregator.py
#   4. Writes results to UserDailySummary + UserAppSummary tables
#   5. Optionally archives/deletes raw rows older than retention window
#
# This eliminates per-request aggregation for historical dates while
# the FastAPI server continues to aggregate today's live data on demand.


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)
