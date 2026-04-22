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

import logging
import os
import secrets
from datetime import datetime, timezone

_LOG = logging.getLogger("telemetry.api")
from pathlib import Path
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel

from .storage   import TelemetryStorage
from .aggregator import aggregate_summary, aggregate_apps, build_timeline, aggregate_all
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
APP_VERSION = "2.8"
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


# ── Per-user device key auth helpers ───────────────────────────────────────────
# These are defined here (not in auth.py) because they need access to `storage`.
#
# Security model:
#   Global AGENT_KEY (env var)  → write access for legacy / shared agents
#   Per-user device key         → write access to /ingest + read access to /api/me/*
#   Admin key / JWT             → full read/write access to /api/*
#
# The admin key is NEVER stored on the device.  It is passed once (--admin-key)
# during install, used to call POST /api/register-device, and then discarded.

def _resolve_device_user(x_api_key: str) -> Optional[str]:
    """Look up which user owns this per-user key. Returns None if not found."""
    key_map = storage.get_device_key_map()
    return key_map.get(x_api_key)


def verify_ingest_key(x_api_key: str = Header(default="")) -> str:
    """
    POST /ingest auth — accepts either:
      • global AGENT_KEY (env var, backward-compatible)
      • per-user device key (generated at install, scoped to one user)
    Returns the resolved username ("*" for global key — payload.user is used instead).
    """
    if AGENT_KEY and x_api_key == AGENT_KEY:
        return "*"   # global key — caller uses payload.user
    user = _resolve_device_user(x_api_key)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Invalid or missing agent API key")


def verify_device_key(x_api_key: str = Header(default="")) -> str:
    """
    GET /api/me/* auth — only accepts per-user device keys.
    Returns the username the key belongs to.
    The endpoint then only returns data for that user — no cross-user access.
    """
    user = _resolve_device_user(x_api_key)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Invalid device key")


@app.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload, resolved_user: str = Depends(verify_ingest_key)):
    if not payload.events:
        raise HTTPException(status_code=400, detail="Empty event batch")
    # Per-user key: enforce that agent can only write its own data
    target_user = payload.user if resolved_user == "*" else resolved_user
    written = storage.write_raw_batch(target_user, payload.device, payload.events)
    return {"accepted": written, "total": len(payload.events)}


# ── Device registration (admin-only, called once at install time) ───────────────

class RegisterDevicePayload(BaseModel):
    username: str

class SettingsPayload(BaseModel):
    retention_days:    int
    retention_enabled: bool = True


@app.post("/api/register-device", status_code=201)
async def register_device(
    payload: RegisterDevicePayload,
    _: dict = Depends(require_admin),
):
    """
    Generate a unique per-user agent key and store it in UserIndex.
    Called ONCE by the installer (with admin credentials).
    Returns the key — installer writes it to config.json on the device.
    The admin key is never returned here and never touches the device.
    """
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    key = secrets.token_urlsafe(32)   # 256-bit URL-safe token
    storage.register_device_key(username, key)
    return {"username": username, "agent_key": key}


# ── Global settings (admin-only) ─────────────────────────────────────────────────

@app.get("/api/settings")
async def get_settings(_: dict = Depends(require_admin)):
    return storage.get_settings()


@app.put("/api/settings")
async def update_settings(payload: SettingsPayload, _: dict = Depends(require_admin)):
    if payload.retention_days < 1:
        raise HTTPException(status_code=400, detail="retention_days must be >= 1")
    current = storage.get_settings()
    current["retention_days"]    = payload.retention_days
    current["retention_enabled"] = payload.retention_enabled
    storage.save_settings(current)
    return storage.get_settings()


@app.post("/api/purge-old-data")
async def purge_old_data(_: dict = Depends(require_admin)):
    """Delete all RawTelemetry rows older than the configured retention period."""
    settings = storage.get_settings()
    if not settings.get("retention_enabled", True):
        raise HTTPException(status_code=409, detail="Retention policy is disabled. Enable it before purging.")
    days    = settings.get("retention_days", 90)
    deleted = storage.purge_old_events(days)
    return {
        "deleted_events": deleted,
        "retention_days": days,
        "purged_at":      storage.get_settings().get("last_purge", ""),
    }


@app.get("/api/notifications")
async def get_notifications(_: dict = Depends(require_admin)):
    """Return admin notifications: new users and retention warnings."""
    from datetime import date, timedelta
    today      = datetime.now(timezone.utc).date()
    now_iso    = datetime.now(timezone.utc).isoformat()
    notifs     = []

    # ── New user notifications (onboarded in last 14 days) ────────────────
    try:
        users = storage.get_users_with_details()
        for u in users:
            created_at = u.get("created_at", "")
            if not created_at:
                continue
            try:
                created_date = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                ).date()
                days_ago = (today - created_date).days
                if 0 <= days_ago <= 14:
                    label = "today" if days_ago == 0 else f"{days_ago} day{'s' if days_ago != 1 else ''} ago"
                    notifs.append({
                        "id":        f"new_user_{u['username']}_{created_date.isoformat()}",
                        "type":      "new_user",
                        "title":     "New user onboarded",
                        "message":   f"{u['username']} joined {label}",
                        "timestamp": created_at,
                        "icon":      "user-plus",
                        "color":     "blue",
                    })
            except Exception:
                pass
    except Exception as exc:
        _LOG.warning("notifications: user fetch failed: %s", exc)

    # ── Retention warnings (oldest data within 5 days of purge cutoff) ───
    try:
        settings = storage.get_settings()
        if settings.get("retention_enabled", True):
            retention_days = settings.get("retention_days", 90)
            cutoff         = today - timedelta(days=retention_days)
            oldest_str     = storage.get_oldest_data_date()
            if oldest_str:
                oldest_date      = date.fromisoformat(oldest_str)
                days_until_purge = (oldest_date - cutoff).days
                if 0 <= days_until_purge <= 5:
                    label = "today" if days_until_purge == 0 else f"in {days_until_purge} day{'s' if days_until_purge != 1 else ''}"
                    notifs.append({
                        "id":        f"retention_{today.isoformat()}",
                        "type":      "retention_warning",
                        "title":     "Data retention alert",
                        "message":   (
                            f"Oldest data ({oldest_str}) will be purged {label}. "
                            f"Retention: {retention_days} days."
                        ),
                        "timestamp": now_iso,
                        "icon":      "alert-triangle",
                        "color":     "yellow",
                    })
    except Exception as exc:
        _LOG.warning("notifications: retention check failed: %s", exc)

    notifs.sort(key=lambda x: x["timestamp"], reverse=True)
    return notifs


# ── /api/me/* — per-user read endpoints (device key auth) ──────────────────────
# These mirror /api/summary, /api/apps, /api/timeline but:
#   • Authenticated by per-user device key (not admin key / JWT)
#   • Always scoped to the key owner — no username parameter accepted
#   • Used by the UI companion running on the same device as the agent

@app.get("/api/me/summary")
async def me_summary(
    request: Request,
    username: str = Depends(verify_device_key),
):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return aggregate_summary(events)


@app.get("/api/me/apps")
async def me_apps(
    request: Request,
    username: str = Depends(verify_device_key),
):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return aggregate_apps(events)


@app.get("/api/me/timeline")
async def me_timeline(
    request: Request,
    username: str = Depends(verify_device_key),
):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return build_timeline(events)


# ── Public endpoints (no auth) ──────────────────────────────────────────────────

@app.get("/api/health")
async def health(request: Request):
    base = _public_base(request)
    return {
        "status":               "ok",
        "service":              "telemetry-analytics",
        "version":              APP_VERSION,
        "started_at":           STARTED_AT,
        "agent_download_url":   f"{base}/download-agent",
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
    Public PowerShell one-liner installer.
    Installs both the telemetry agent AND the UI companion in one shot.
    The agent fetches /agent-config during --install and writes its own key.
    """
    base = _public_base(request)
    script = f"""\
# ============================================================
#  ProdAnalytics - Full Installer (Agent + UI Companion)
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'Stop'
$ServerUrl = '{base}'

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    Write-Host "Requesting administrator privileges..." -ForegroundColor Yellow
    $cmd = "-NoProfile -ExecutionPolicy Bypass -Command `"irm '$ServerUrl/install-script' | iex`""
    Start-Process PowerShell -ArgumentList $cmd -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== ProdAnalytics Installer ===" -ForegroundColor Cyan
Write-Host "Server : $ServerUrl"
Write-Host ""

# ── Step 1: Download agent ────────────────────────────────────────────────
Write-Host "[1/4] Downloading agent..." -ForegroundColor Cyan
$AgentTmp = "$env:TEMP\\telemetry_agent.exe"
Invoke-WebRequest -Uri "$ServerUrl/download-agent" -OutFile $AgentTmp -UseBasicParsing
Write-Host "      OK" -ForegroundColor Green

# ── Step 2: Install agent (config + scheduled task + immediate start) ─────
Write-Host "[2/4] Installing agent..." -ForegroundColor Cyan
& $AgentTmp --install --server-url $ServerUrl
Write-Host "      Agent installed and started" -ForegroundColor Green

# ── Step 3: Download UI companion ─────────────────────────────────────────
Write-Host "[3/4] Downloading UI companion..." -ForegroundColor Cyan
$UiDir  = "C:\\Program Files\\TelemetryUI"
$UiExe  = "$UiDir\\telemetry_ui.exe"
if (-not (Test-Path $UiDir)) {{ New-Item -ItemType Directory -Path $UiDir | Out-Null }}
Invoke-WebRequest -Uri "$ServerUrl/download-ui" -OutFile $UiExe -UseBasicParsing
Write-Host "      Saved to $UiExe" -ForegroundColor Green

# ── Step 4: Register UI autostart task + launch now ───────────────────────
Write-Host "[4/4] Setting up UI companion autostart..." -ForegroundColor Cyan
schtasks /delete /tn "TelemetryUI" /f 2>$null
$action  = New-ScheduledTaskAction  -Execute $UiExe
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit 0 -MultipleInstances IgnoreNew
Register-ScheduledTask -TaskName "TelemetryUI" -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force | Out-Null
Write-Host "      Registered startup task 'TelemetryUI'" -ForegroundColor Green

# Launch the UI for the current session immediately (no logout needed)
Start-Process $UiExe
Write-Host "      UI companion launched" -ForegroundColor Green

Write-Host ""
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host " Installation complete!" -ForegroundColor Green
Write-Host "=====================================================" -ForegroundColor Cyan
Write-Host ""
Write-Host " Agent  : C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe" -ForegroundColor White
Write-Host " UI     : $UiExe" -ForegroundColor White
Write-Host " Server : $ServerUrl" -ForegroundColor White
Write-Host " Log    : C:\\ProgramData\\TelemetryAgent\\logs.txt" -ForegroundColor Gray
Write-Host ""
Write-Host " Both will start automatically at every Windows login." -ForegroundColor Yellow
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="install_prodanalytics.ps1"'},
    )


@app.get("/uninstall-script")
async def uninstall_script(request: Request):
    """Public — generates a PowerShell uninstall script for the agent."""
    base = _public_base(request)
    script = f"""\
# ============================================================
#  Telemetry Agent - Uninstaller
#  Server: {base}
# ============================================================
$ErrorActionPreference = 'SilentlyContinue'

# Self-elevate to Administrator if needed
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{
    $cmd = "-NoProfile -ExecutionPolicy Bypass -Command `"irm '$base/uninstall-script' | iex`""
    Start-Process PowerShell -ArgumentList $cmd -Verb RunAs
    exit
}}

Write-Host ""
Write-Host "=== Telemetry Agent Uninstaller ===" -ForegroundColor Cyan
Write-Host ""

$AgentExe = "C:\\Program Files\\TelemetryAgent\\telemetry_agent.exe"

if (Test-Path $AgentExe) {{
    Write-Host "[1/3] Running built-in uninstall..." -ForegroundColor Cyan
    & $AgentExe --uninstall
}} else {{
    Write-Host "[1/3] Agent EXE not found — cleaning up manually..." -ForegroundColor Yellow
    schtasks /delete /tn TelemetryAgent /f 2>$null
    Stop-Process -Name telemetry_agent -Force 2>$null
    Remove-Item -Recurse -Force "C:\\Program Files\\TelemetryAgent" 2>$null
    Remove-Item -Recurse -Force "C:\\ProgramData\\TelemetryAgent" 2>$null
    $tmp = [System.IO.Path]::GetTempPath()
    Remove-Item -Recurse -Force "$tmp\\TelemetryAgent" 2>$null
    Remove-Item -Recurse -Force "$tmp\\telemetry_backup" 2>$null
}}

Write-Host ""
Write-Host "[2/3] Removing UI companion..." -ForegroundColor Cyan
schtasks /delete /tn TelemetryUI /f 2>$null
Stop-Process -Name telemetry_ui -Force 2>$null
$uiPaths = @(
    "C:\\Program Files\\TelemetryUI",
    "$env:APPDATA\\TelemetryUI",
    "$env:LOCALAPPDATA\\TelemetryUI"
)
foreach ($p in $uiPaths) {{ Remove-Item -Recurse -Force $p 2>$null }}

Write-Host "[3/3] Done." -ForegroundColor Green
Write-Host ""
Write-Host "Agent and UI companion have been removed from this machine." -ForegroundColor Green
Write-Host "Employee data in the cloud is not affected." -ForegroundColor Gray
Write-Host ""
Read-Host "Press Enter to close"
"""
    return PlainTextResponse(
        content=script,
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="uninstall_agent.ps1"'},
    )


@app.get("/download-ui")
async def download_ui():
    """Download the UI companion EXE (served locally or redirected to blob)."""
    redirect_url = os.getenv("UI_DOWNLOAD_URL")
    if redirect_url:
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=redirect_url)
    exe = Path(__file__).parent.parent / "dist" / "telemetry_ui.exe"
    if not exe.exists():
        raise HTTPException(
            status_code=404,
            detail="Set UI_DOWNLOAD_URL env var or build dist/telemetry_ui.exe.",
        )
    return FileResponse(
        str(exe),
        media_type="application/octet-stream",
        filename="telemetry_ui.exe",
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
        return storage.get_users_with_aliases()
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


@app.get("/api/user-data")
async def get_user_data(user: str, date: str, _: dict = Depends(get_current_user)):
    """
    Combined endpoint: returns summary + apps + timeline in one request.
    Calls get_raw_events once and _merge_consecutive once — faster than the
    three individual endpoints when the dashboard needs all three.
    """
    try:
        events = storage.get_raw_events(user, date)
        if not events:
            raise HTTPException(status_code=404, detail=f"No data for {user} on {date}")
        result = aggregate_all(events)
        return {"user": user, "date": date, **result}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ── Admin: data deletion ────────────────────────────────────────────────────────

class RenameUserPayload(BaseModel):
    old_name: str
    new_name: str

class MergeUsersPayload(BaseModel):
    source: str   # user whose data gets moved (will be deleted)
    target: str   # user who receives all the data


@app.put("/api/user/rename")
async def rename_user(payload: RenameUserPayload, _: dict = Depends(require_admin)):
    """
    Set a display alias for a tracked employee.
    Only updates the dashboard label — RawTelemetry stays under the original
    username so the agent can keep sending data without creating duplicates.
    """
    username  = payload.old_name.strip()
    new_alias = payload.new_name.strip()
    if not username or not new_alias:
        raise HTTPException(status_code=400, detail="old_name and new_name are required")
    try:
        ok = storage.set_alias(username, new_alias)
        if not ok:
            raise HTTPException(status_code=404, detail=f"Employee '{username}' not found")
        return {"user": username, "alias": new_alias}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/user/merge")
async def merge_users(payload: MergeUsersPayload, _: dict = Depends(require_admin)):
    """
    Merge all telemetry from `source` into `target`, then delete `source`.
    Use when a re-installed agent created a duplicate employee profile.
    """
    src = payload.source.strip()
    tgt = payload.target.strip()
    if not src or not tgt:
        raise HTTPException(status_code=400, detail="source and target are required")
    try:
        return storage.merge_users(src, tgt)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
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
    # Resolve the canonical username (exact case as stored in UserIndex / RawTelemetry).
    # add_member() must receive the canonical name so get_raw_events() can find the data.
    all_users = storage.get_all_users()
    canonical = next((u for u in all_users if u.lower() == payload.username.lower()), None)
    if canonical is None:
        raise HTTPException(status_code=404, detail=f"Employee '{payload.username}' not found")
    result = group_storage.add_member(group_id, canonical)
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

    # Build a lowercase → canonical map so old groups (whose members were stored
    # in lowercase) still resolve correctly to the real RawTelemetry partition keys.
    all_users  = storage.get_all_users()
    canon_map  = {u.lower(): u for u in all_users}

    members_data = []
    for stored_name in group["members"]:
        canonical = canon_map.get(stored_name.lower(), stored_name)
        events    = storage.get_raw_events(canonical, date)
        if events:
            agg = aggregate_all(events)
            members_data.append({
                "username": canonical,
                "summary":  agg["summary"],
                "timeline": agg["timeline"],
            })
        else:
            members_data.append({"username": canonical, "summary": None, "timeline": []})

    return {"group": group, "date": date, "members": members_data}


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)
