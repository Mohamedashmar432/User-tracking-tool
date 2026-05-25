"""POST /ingest and /api/register-device."""

import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_admin
from ..deps import storage, verify_ingest_key

router = APIRouter()

_MAX_EVENT_DURATION  = 86_400    # 24 h — single event cap
_MAX_TOTAL_BATCH_DUR = 86_400    # 24 h — batch total cap


def _validate_events(events: list) -> list:
    """
    Sanitise event list before writing to storage:
    - Skip zero/negative-duration events.
    - Clamp individual event duration to [1, _MAX_EVENT_DURATION].
    - If the batch total exceeds _MAX_TOTAL_BATCH_DUR, scale all proportionally.
    """
    cleaned = []
    for ev in events:
        dur = int(ev.get("duration", 0))
        if dur < 1:
            continue
        dur = min(dur, _MAX_EVENT_DURATION)
        cleaned.append({**ev, "duration": dur})

    total = sum(e["duration"] for e in cleaned)
    if total > _MAX_TOTAL_BATCH_DUR and total > 0:
        scale = _MAX_TOTAL_BATCH_DUR / total
        cleaned = [{**e, "duration": max(1, int(e["duration"] * scale))}
                   for e in cleaned]

    return cleaned


class IngestPayload(BaseModel):
    user:   str
    device: str = ""
    events: List[Dict[str, Any]]


class RegisterDevicePayload(BaseModel):
    username: str


@router.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload, resolved_user: str = Depends(verify_ingest_key)):
    if not payload.events:
        raise HTTPException(status_code=400, detail="Empty event batch")

    clean_events = _validate_events(payload.events)
    if not clean_events:
        raise HTTPException(status_code=400, detail="Batch contains no valid events")

    target_user = payload.user if resolved_user == "*" else resolved_user
    written = storage.write_raw_batch(target_user, payload.device, clean_events)
    return {"accepted": written, "total": len(payload.events)}


@router.post("/api/register-device", status_code=201)
async def register_device(
    payload: RegisterDevicePayload,
    _: dict = Depends(require_admin),
):
    """Generate a per-user agent key. Called once by the installer with admin credentials."""
    username = payload.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="username required")
    key = secrets.token_urlsafe(32)
    storage.register_device_key(username, key)
    return {"username": username, "agent_key": key}
