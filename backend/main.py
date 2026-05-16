"""
main.py — FastAPI app entry point.

Wires together middleware, routers, and the two public top-level endpoints
(health, dashboard root). All route logic lives in backend/routes/.

Run:
    python -m uvicorn backend.main:app --host 0.0.0.0 --port 8000 --reload
"""

import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .deps import APP_VERSION, STARTED_AT, _public_base
from .routes import admin, analytics, auth_routes, groups, ingest, scripts

app = FastAPI(title="Telemetry Analytics API", version=APP_VERSION)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

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
        "linux_agent_download_url": f"{base}/download-linux-agent",
        "linux_ui_download_url":    f"{base}/download-linux-ui",
    }


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("backend.main:app", host="0.0.0.0", port=port)
