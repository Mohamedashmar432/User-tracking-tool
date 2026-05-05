"""
deps.py — Shared application-wide singletons and helpers.

Imported by route modules to access storage, auth helpers, and utilities
without re-initialising connections on each import.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import Header, HTTPException, Query, Request

from .storage import TelemetryStorage
from .users   import UserStorage
from .groups  import GroupStorage
from .auth    import AGENT_KEY

_LOG = logging.getLogger("telemetry.api")

APP_VERSION = "2.9"
STARTED_AT  = datetime.now(timezone.utc).isoformat()

storage       = TelemetryStorage()
user_storage  = UserStorage(storage.service)
group_storage = GroupStorage(storage.service)


def _public_base(request: Request) -> str:
    proto = request.headers.get("x-forwarded-proto", request.url.scheme)
    host  = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _resolve_device_user(x_api_key: str) -> Optional[str]:
    """Return the username that owns this per-device key, or None."""
    key_map = storage.get_device_key_map()
    return key_map.get(x_api_key)


def verify_ingest_key(x_api_key: str = Header(default="")) -> str:
    """
    POST /ingest auth — accepts global AGENT_KEY or a per-user device key.
    Returns "*" for global key (caller uses payload.user) or the bound username.
    """
    if AGENT_KEY and x_api_key == AGENT_KEY:
        return "*"
    user = _resolve_device_user(x_api_key)
    if user:
        return user
    raise HTTPException(status_code=401, detail="Invalid or missing agent API key")


def verify_device_key(
    x_api_key: str = Header(default=""),
    user:      str = Query(default=""),
) -> str:
    """
    GET /api/me/* auth — per-device key, or global AGENT_KEY + ?user=<username>.
    """
    device_user = _resolve_device_user(x_api_key)
    if device_user:
        return device_user
    if AGENT_KEY and x_api_key == AGENT_KEY and user:
        return user
    raise HTTPException(status_code=401, detail="Invalid device key")
