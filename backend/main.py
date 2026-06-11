"""
main.py — FastAPI app entry point.

Wires together middleware, routers, and the two public top-level endpoints
(health, dashboard root). All route logic lives in backend/routes/.

Run:
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""

import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from .deps import APP_VERSION, STARTED_AT, _public_base
from .routes import admin, analytics, auth_routes, groups, ingest, scripts

_LOG = logging.getLogger("telemetry.api")

app = FastAPI(title="Telemetry Analytics API", version=APP_VERSION)

# ── CORS ──────────────────────────────────────────────────────────────────────
# Set ALLOWED_ORIGINS=https://your-domain.com in production (comma-separated).
# Leaving it unset keeps "*" for local dev only — credentials are disabled in
# that case so browsers won't send cookies/auth headers cross-origin.
_raw_origins = os.getenv("ALLOWED_ORIGINS", "").strip()
_allowed_origins: list = [o.strip() for o in _raw_origins.split(",") if o.strip()]

if _allowed_origins:
    _allow_credentials = True
else:
    _allowed_origins   = ["*"]
    _allow_credentials = False
    _LOG.warning(
        "ALLOWED_ORIGINS not configured — CORS open to all origins. "
        "Set ALLOWED_ORIGINS=https://your-domain.com in production."
    )

app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "X-API-Key", "Content-Type"],
)


@app.exception_handler(ValueError)
async def _value_error_handler(_req: Request, exc: ValueError) -> JSONResponse:
    """Turn input-validation ValueErrors into HTTP 400 responses."""
    return JSONResponse(status_code=400, content={"detail": str(exc)})

app.include_router(ingest.router)
app.include_router(scripts.router)
app.include_router(analytics.router)
app.include_router(auth_routes.router)
app.include_router(admin.router)
app.include_router(groups.router)

_INDEX = Path(__file__).parent.parent / "index.html"


@app.get("/")
async def index():
    return FileResponse(str(_INDEX))


@app.get("/api/health")
async def health(request: Request):
    base = _public_base(request)
    return {
        "status":               "ok",
        "service":              "telemetry-analytics",
        "version":              APP_VERSION,
        "started_at":           STARTED_AT,
        "agent_download_url":       f"{base}/download-agent",
        "agent_zip_download_url":   f"{base}/download-agent-zip",
        "ui_zip_download_url":      f"{base}/download-ui",
        "linux_agent_download_url":     f"{base}/download-linux-agent",
        "linux_ui_download_url":        f"{base}/download-linux-ui",
        "linux_dashboard_download_url": f"{base}/download-linux-dashboard",
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)
