"""
auth.py — Authentication layer.

Two independent credential systems live side-by-side:

  Agent writes  →  X-API-Key: <AGENT_API_KEY>
                   Used only by POST /ingest.  Read-only risk if leaked.

  Dashboard     →  JWT Bearer token
                   Issued by POST /auth/login (username + password from UserAuth table).
                   Also accepts X-API-Key: <ADMIN_API_KEY> for direct curl access.

Environment variables
---------------------
AGENT_API_KEY   Secret for agents posting telemetry (falls back to API_KEY)
ADMIN_API_KEY   Legacy X-API-Key for curl/scripts (falls back to API_KEY)
API_KEY         Single shared key — fallback when role-specific vars are absent
JWT_SECRET      HMAC signing secret for JWT (falls back to ADMIN_API_KEY, then API_KEY)
JWT_TTL_HOURS   Token lifetime in hours (default 8)
ADMIN_PASSWORD  Password for auto-created admin account on first start (default changeme123)
"""

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

try:
    from jose import jwt, JWTError
except ImportError:
    raise RuntimeError(
        "python-jose[cryptography] is required — add it to requirements.txt "
        "and run: pip install 'python-jose[cryptography]'"
    )

_LOG = logging.getLogger("telemetry.auth")

# ── Key resolution ───────────────────────────────────────────────────────────────

def _env(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v:
            return v
    return ""


AGENT_KEY  = _env("AGENT_API_KEY", "API_KEY")
ADMIN_KEY  = _env("ADMIN_API_KEY",  "API_KEY")
JWT_SECRET = _env("JWT_SECRET", "ADMIN_API_KEY", "API_KEY") or "dev-secret-change-me"
JWT_ALG    = "HS256"
JWT_TTL    = int(os.getenv("JWT_TTL_HOURS", "8"))

if not AGENT_KEY:
    _LOG.warning("AGENT_API_KEY not set — /ingest will return 401 for all requests")
if JWT_SECRET == "dev-secret-change-me":
    _LOG.warning("JWT_SECRET not set — using insecure default. Set JWT_SECRET in production.")


# ── Token helpers ────────────────────────────────────────────────────────────────

def create_token(username: str, role: str) -> str:
    exp = datetime.now(timezone.utc) + timedelta(hours=JWT_TTL)
    return jwt.encode(
        {"sub": username, "role": role, "exp": exp},
        JWT_SECRET,
        algorithm=JWT_ALG,
    )


# ── Dependency: agent write access ───────────────────────────────────────────────

def verify_agent_key(x_api_key: str = Header(default="")) -> None:
    """POST /ingest — validates X-API-Key against AGENT_API_KEY."""
    if not AGENT_KEY or x_api_key != AGENT_KEY:
        raise HTTPException(status_code=401, detail="Invalid or missing agent API key")


# ── Dependency: dashboard / admin access ─────────────────────────────────────────

_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    creds: Optional[HTTPAuthorizationCredentials] = Depends(_bearer),
    x_api_key: str = Header(default=""),
) -> dict:
    """
    Accepts either:
      Authorization: Bearer <jwt>    →  decoded, returns {username, role}
      X-API-Key: <ADMIN_API_KEY>    →  returns {username: "api-key", role: "admin"}
    Raises 401 if neither is valid.
    """
    if creds:
        try:
            payload = jwt.decode(creds.credentials, JWT_SECRET, algorithms=[JWT_ALG])
            return {"username": payload["sub"], "role": payload.get("role", "viewer")}
        except JWTError:
            raise HTTPException(status_code=401, detail="Token invalid or expired — please log in again")

    if ADMIN_KEY and x_api_key == ADMIN_KEY:
        return {"username": "api-key", "role": "admin"}

    raise HTTPException(status_code=401, detail="Authentication required")


def require_admin(user: dict = Depends(get_current_user)) -> dict:
    """Extends get_current_user — additionally enforces admin role."""
    if user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user
