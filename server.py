import os
import json
from datetime import datetime
from typing import List, Dict, Any
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

app = FastAPI(title="Employee Telemetry API")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

LOG_FILE = "logs.txt"
LOG_FILE_OLD = "logs.txt.old"

# App Categorization Maps
PRODUCTIVE_APPS = {
    "code.exe", "codinsiders.exe", "windowsterminal.exe", "powershell.exe", "cmd.exe",
    "excel.exe", "winword.exe", "powerpnt.exe", "slack.exe", "teams.exe", "zoom.exe"
}
PRODUCTIVE_DOMAINS = {
    "github.com", "stackoverflow.com", "docs.microsoft.com", "docs.python.org",
    "linkedin.com", "medium.com", "jira.atlassian.com", "confluence.atlassian.com"
}
DISTRACTION_DOMAINS = {
    "youtube.com", "facebook.com", "twitter.com", "x.com", "instagram.com",
    "reddit.com", "netflix.com", "twitch.tv", "tiktok.com"
}

def categorize_activity(app_name: str, domain: str) -> str:
    app_lower = app_name.lower()
    domain_lower = domain.lower()

    # Check if it's a browser with a distraction domain
    if any(browser in app_lower for browser in ["chrome", "edge", "firefox", "brave"]):
        if any(d in domain_lower for d in DISTRACTION_DOMAINS):
            return "Distraction"
        if any(d in domain_lower for d in PRODUCTIVE_DOMAINS):
            return "Productive"
        return "Neutral"

    # Check if the process itself is productive
    if any(p in app_lower for p in PRODUCTIVE_APPS):
        return "Productive"

    # Fallback based on process name for common distractions (games etc)
    if any(word in app_lower for word in ["steam", "epicgames", "origin", "battle.net"]):
        return "Distraction"

    return "Neutral"

def read_logs() -> List[Dict]:
    data = []
    # Read rotated log first, then current log
    for path in [LOG_FILE_OLD, LOG_FILE]:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        try:
                            data.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
    return data

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.get("/api/stats")
async def get_stats():
    logs = read_logs()
    if not logs:
        return {"error": "No logs found"}

    last_entry = logs[-1]

    # 1. Calculate Total Times
    usage_total = last_entry.get("app_usage_total", {})
    total_active_seconds = sum(usage_total.values())

    # 2. Categorize Usage
    category_totals = {"Productive": 0, "Distraction": 0, "Neutral": 0}
    for app, seconds in usage_total.items():
        # We don't have the domain for total aggregated time (only for the current snapshot)
        # So we categorize the process name
        cat = categorize_activity(app, "N/A")
        category_totals[cat] += seconds

    # 3. Productivity Score
    productivity_score = 0
    if total_active_seconds > 0:
        productivity_score = (category_totals["Productive"] / total_active_seconds) * 100

    # 4. Timeline Data (Last 100 entries)
    timeline = []
    for entry in logs[-100:]:
        timeline.append({
            "t": entry.get("timestamp"),
            "idle": entry.get("idle_seconds", 0),
            "active": entry.get("active", False),
            "app": entry.get("current_app", "Unknown")
        })

    # 5. Recent Activity Feed
    feed = []
    for entry in reversed(logs[-20:]):
        cat = categorize_activity(entry.get("current_app", ""), entry.get("domain", ""))
        feed.append({
            "timestamp": entry.get("timestamp"),
            "app": entry.get("current_app"),
            "domain": entry.get("domain"),
            "category": cat,
            "duration": entry.get("session_duration", 0)
        })

    return {
        "summary": {
            "total_active_time": total_active_seconds,
            "productivity_score": round(productivity_score, 1),
            "current_app": last_entry.get("current_app"),
            "current_domain": last_entry.get("domain"),
            "active_status": last_entry.get("active"),
            "idle_seconds": last_entry.get("idle_seconds")
        },
        "categories": category_totals,
        "timeline": timeline,
        "feed": feed,
        "app_totals": usage_total
    }

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
