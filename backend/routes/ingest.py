"""POST /ingest and /api/register-device."""

import secrets
from typing import Any, Dict, List

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..auth import require_admin
from ..deps import storage, verify_ingest_key

router = APIRouter()


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
    target_user = payload.user if resolved_user == "*" else resolved_user
    written = storage.write_raw_batch(target_user, payload.device, payload.events)
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
