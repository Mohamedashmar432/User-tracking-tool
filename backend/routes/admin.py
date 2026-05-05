"""
Admin-only endpoints.

/api/settings, /api/purge-old-data, /api/notifications,
/api/user/rename, /api/user/merge, /api/user, /api/user-date
"""

import logging
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_admin
from ..deps import storage

_LOG = logging.getLogger("telemetry.api")

router = APIRouter()


class SettingsPayload(BaseModel):
    retention_days:    int
    retention_enabled: bool = True


class RenameUserPayload(BaseModel):
    old_name: str
    new_name: str


class MergeUsersPayload(BaseModel):
    source: str
    target: str


@router.get("/api/settings")
async def get_settings(_: dict = Depends(require_admin)):
    return storage.get_settings()


@router.put("/api/settings")
async def update_settings(payload: SettingsPayload, _: dict = Depends(require_admin)):
    if payload.retention_days < 1:
        raise HTTPException(status_code=400, detail="retention_days must be >= 1")
    current = storage.get_settings()
    current["retention_days"]    = payload.retention_days
    current["retention_enabled"] = payload.retention_enabled
    storage.save_settings(current)
    return storage.get_settings()


@router.post("/api/purge-old-data")
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


@router.get("/api/notifications")
async def get_notifications(_: dict = Depends(require_admin)):
    """Return admin notifications: new users, inactive agents, and retention warnings."""
    today   = datetime.now(timezone.utc).date()
    now_iso = datetime.now(timezone.utc).isoformat()
    notifs  = []
    users   = []

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

    # ── Inactive agent notifications (no data for 3+ days) ───────────────
    INACTIVE_THRESHOLD_DAYS = 3
    try:
        for u in users:
            last_seen_str = u.get("last_seen", "")
            if not last_seen_str:
                continue
            try:
                last_seen_date = datetime.fromisoformat(
                    last_seen_str.replace("Z", "+00:00")
                ).date()
                days_inactive = (today - last_seen_date).days
                if days_inactive >= INACTIVE_THRESHOLD_DAYS:
                    label = f"{days_inactive} day{'s' if days_inactive != 1 else ''}"
                    notifs.append({
                        "id":        f"inactive_{u['username']}",
                        "type":      "agent_inactive",
                        "title":     "Agent inactive",
                        "message":   f"{u['username']} has not sent data for {label}",
                        "timestamp": now_iso,
                        "icon":      "wifi-off",
                        "color":     "red",
                    })
            except Exception:
                pass
    except Exception as exc:
        _LOG.warning("notifications: inactive agent check failed: %s", exc)

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


@router.put("/api/user/rename")
async def rename_user(payload: RenameUserPayload, _: dict = Depends(require_admin)):
    """Set a display alias — only updates the dashboard label, not RawTelemetry keys."""
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


@router.post("/api/user/merge")
async def merge_users(payload: MergeUsersPayload, _: dict = Depends(require_admin)):
    """Merge all telemetry from source into target, then delete source."""
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


@router.delete("/api/user")
async def delete_user(user: str, _: dict = Depends(require_admin)):
    try:
        deleted = storage.delete_user(user)
        return {"user": user, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.delete("/api/user-date")
async def delete_user_date(user: str, date: str, _: dict = Depends(require_admin)):
    try:
        deleted = storage.delete_user_date(user, date)
        return {"user": user, "date": date, "deleted_events": deleted}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
