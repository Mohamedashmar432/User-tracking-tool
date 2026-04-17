"""
main.py — FastAPI analytics server.

Responsibilities
----------------
POST /ingest            receive raw batches from agents → RawTelemetry
GET  /api/users         list known tracked users from UserIndex
GET  /api/user-summary  aggregate KPIs on demand from raw rows
GET  /api/user-apps     per-app usage totals
GET  /api/user-timeline merged time-series for charts

POST /auth/login        issue JWT for dashboard login
GET  /auth/me           return current user info
GET  /auth/users        list dashboard accounts (admin)
POST /auth/users        create dashboard account (admin)
PUT  /auth/users/{u}/password  change password (admin)
DELETE /auth/users/{u}  delete dashboard account (admin)

Authentication
--------------
/ingest          → X-API-Key: <AGENT_API_KEY>
all /api/* and /auth/users*
                 → JWT Bearer  OR  X-API-Key: <ADMIN_API_KEY>
/api/health, /install-script, /agent-config, /download-agent
                 → public (no auth required)

Run
---
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import uvicorn
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from .storage   import TelemetryStorage
from .aggregator import aggregate_summary, aggregate_apps, build_timeline
from .auth      import (
    verify_agent_key,
    get_current_user,
    require_admin,
    create_token,
    AGENT_KEY,
)
from .users     import UserStorage
from .groups    import GroupStorage

# ── Version ──────────────────────────────────────────────────────────────────────
APP_VERSION = "2.6"
STARTED_AT  = datetime.now(timezone.utc).isoformat()

# ── App setup ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Telemetry Analytics API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

storage       = TelemetryStorage()
user_storage  = UserStorage(storage.service)   # shares the same TableServiceClient
group_storage = GroupStorage(storage.service)  # shares the same TableServiceClient


# ── URL helper ──────────────────────────────────────────────────────────────────

def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host  = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


# ── Ingest (write path — agent key) ────────────────────────────────────────────

class IngestPayload(BaseModel):
    user:   str
    device: str = ""
    events: List[Dict[str, Any]]


@app.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload, _: None = Depends(verify_agent_key)):
    if not payload.events:
        raise HTTPException(status_code=400, detail="Empty event batch")
    written = storage.write_raw_batch(payload.user, payload.device, payload.events)
    return {"accepted": written, "total": len(payload.events)}


# ── Public endpoints (no auth) ──────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status":     "ok",
        "service":    "telemetry-analytics",
        "version":    APP_VERSION,
        "started_at": STARTED_AT,
    }


@app.get("/agent-config")
async def agent_config(request: Request):
    """
    Public — called by the agent during --install to self-configure.
    Returns the canonical server URL and the agent API key so the agent
    can write both into its config.json automatically.
    The agent key grants write-only access to /ingest; leaking it lets
    someone send fake data, not read real data.
    """
    base = _public_base(request)
    return {
        "server_url":    base,
        "ingest_url":    f"{base}/ingest",
        "agent_api_key": AGENT_KEY,
    }


@app.get("/download-agent")
async def download_agent():
    """Public — referenced by the PowerShell install script."""
    redirect_url = os.getenv("AGENT_DOWNLOAD_URL")
    if redirect_url:
        return RedirectResponse(url=redirect_url)
    exe = Path("dist/telemetry_agent.exe")
    if not exe.exists():
        raise HTTPException(
            status_code=404,
            detail="Set AGENT_DOWNLOAD_URL env var to point to the hosted EXE.",
        )
    return FileResponse(
        str(exe),
        media_type="application/octet-stream",
        filename="telemetry_agent.exe",
    )


@app.get("/install-script")
async def install_script(request: Request):
    """
    Public PowerShell installer.  The agent fetches /agent-config during
    --install and writes the key into its own config — no key needed here.
    """
    base = _public_base(request)
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


# ── Auth routes ─────────────────────────────────────────────────────────────────

class LoginPayload(BaseModel):
    username: str
    password: str


class CreateUserPayload(BaseModel):
    username: str
    password: str
    role:     str = "viewer"


class ChangePasswordPayload(BaseModel):
    password: str


class ChangeRolePayload(BaseModel):
    role: str


@app.post("/auth/login")
async def login(payload: LoginPayload):
    """Public — issue a JWT on valid credentials."""
    user = user_storage.verify_password(payload.username, payload.password)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid username or password")
    token = create_token(user["username"], user["role"])
    return {"token": token, "username": user["username"], "role": user["role"]}


@app.get("/auth/me")
async def me(user: dict = Depends(get_current_user)):
    return user


@app.get("/auth/users")
async def list_auth_users(_: dict = Depends(require_admin)):
    return user_storage.list_users()


@app.post("/auth/users", status_code=201)
async def create_auth_user(payload: CreateUserPayload, _: dict = Depends(require_admin)):
    if payload.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'viewer'")
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    return user_storage.create_user(payload.username, payload.password, payload.role)


@app.put("/auth/users/{username}/password")
async def change_password(
    username: str,
    payload:  ChangePasswordPayload,
    _:        dict = Depends(require_admin),
):
    if len(payload.password) < 8:
        raise HTTPException(status_code=400, detail="Password must be at least 8 characters")
    if not user_storage.update_password(username, payload.password):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.put("/auth/users/{username}/role")
async def change_role(
    username: str,
    payload:  ChangeRolePayload,
    actor:    dict = Depends(require_admin),
):
    if payload.role not in ("admin", "viewer"):
        raise HTTPException(status_code=400, detail="role must be 'admin' or 'viewer'")
    if username == actor["username"]:
        raise HTTPException(status_code=400, detail="Cannot change your own role")
    if not user_storage.update_role(username, payload.role):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


@app.delete("/auth/users/{username}")
async def delete_auth_user(username: str, actor: dict = Depends(require_admin)):
    if username == actor["username"]:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    if not user_storage.delete_user(username):
        raise HTTPException(status_code=404, detail="User not found")
    return {"ok": True}


# ── Analytics (read path — JWT or admin X-API-Key) ──────────────────────────────

@app.get("/api/users")
async def get_users(_: dict = Depends(get_current_user)):
    try:
        return [{"user": u} for u in storage.get_all_users()]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user-summary")
async def get_user_summary(user: str, date: str, _: dict = Depends(get_current_user)):
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
async def get_user_apps(user: str, date: str, _: dict = Depends(get_current_user)):
    try:
        events = storage.get_raw_events(user, date)
        return aggregate_apps(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/user-timeline")
async def get_user_timeline(user: str, date: str, _: dict = Depends(get_current_user)):
    try:
        events = storage.get_raw_events(user, date)
        return build_timeline(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin: data deletion ────────────────────────────────────────────────────────

class RenameUserPayload(BaseModel):
    old_name: str
    new_name: str


@app.put("/api/user/rename")
async def rename_user(payload: RenameUserPayload, _: dict = Depends(require_admin)):
    """Rename a tracked employee — migrates all telemetry to the new username."""
    old = payload.old_name.strip()
    new = payload.new_name.strip()
    if not old or not new:
        raise HTTPException(status_code=400, detail="old_name and new_name are required")
    if old.lower() == new.lower():
        raise HTTPException(status_code=400, detail="New name is the same as the current name")
    try:
        result = storage.rename_user(old, new)
        return result
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/user")
async def delete_user(user: str, _: dict = Depends(require_admin)):
    try:
        deleted = storage.delete_user(user)
        return {"user": user, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/user-date")
async def delete_user_date(user: str, date: str, _: dict = Depends(require_admin)):
    try:
        deleted = storage.delete_user_date(user, date)
        return {"user": user, "date": date, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Groups ──────────────────────────────────────────────────────────────────────

class CreateGroupPayload(BaseModel):
    name: str

class AddMemberPayload(BaseModel):
    username: str


@app.get("/api/groups")
async def list_groups(_: dict = Depends(get_current_user)):
    return group_storage.list_groups()


@app.post("/api/groups", status_code=201)
async def create_group(payload: CreateGroupPayload, actor: dict = Depends(require_admin)):
    if not payload.name.strip():
        raise HTTPException(status_code=400, detail="Group name is required")
    return group_storage.create_group(payload.name, actor["username"])


@app.delete("/api/groups/{group_id}")
async def delete_group(group_id: str, _: dict = Depends(require_admin)):
    if not group_storage.delete_group(group_id):
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


@app.post("/api/groups/{group_id}/members")
async def add_group_member(
    group_id: str,
    payload:  AddMemberPayload,
    _:        dict = Depends(require_admin),
):
    result = group_storage.add_member(group_id, payload.username)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@app.delete("/api/groups/{group_id}/members/{username}")
async def remove_group_member(
    group_id: str,
    username: str,
    _:        dict = Depends(require_admin),
):
    result = group_storage.remove_member(group_id, username)
    if result is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return result


@app.get("/api/groups/{group_id}/summary")
async def get_group_summary(
    group_id: str,
    date:     str,
    _:        dict = Depends(get_current_user),
):
    group = group_storage.get_group(group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    members_data = []
    for username in group["members"]:
        events   = storage.get_raw_events(username, date)
        summary  = aggregate_summary(events) if events else None
        timeline = build_timeline(events)    if events else []
        members_data.append({
            "username": username,
            "summary":  summary,
            "timeline": timeline,
        })

    return {"group": group, "date": date, "members": members_data}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)
