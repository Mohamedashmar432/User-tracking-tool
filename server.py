import os
import json
import platform
import getpass
import re
from datetime import datetime
from typing import List, Dict, Any
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
from azure.data.tables import TableClient, TableServiceClient

app = FastAPI(title="Employee Telemetry Analytics Server")

# Enable CORS for the frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
CONNECTION_STRING = os.getenv("AzureWebJobsStorage", "UseDevelopmentStorageAccount")
APP_USAGE_TABLE = "UserAppUsage"
DAILY_SUMMARY_TABLE = "UserDailySummary"
ACTIVITY_LOGS_TABLE = "UserActivityLogs"

# Categorization Rules
PRODUCTIVE_KEYWORDS = {"code.exe", "codinsiders.exe", "windowsterminal.exe", "powershell.exe", "cmd.exe", "excel", "word", "powerpnt", "github.com", "stackoverflow.com"}
UNPRODUCTIVE_KEYWORDS = {"youtube.com", "netflix.com", "instagram.com", "facebook.com", "twitter.com", "x.com"}

def categorize_app(app_name, domain="N/A"):
    text = (app_name + " " + domain).lower()
    if any(k in text for k in UNPRODUCTIVE_KEYWORDS):
        return "Unproductive"
    if any(k in text for k in PRODUCTIVE_KEYWORDS):
        return "Productive"
    return "Neutral"

class TableService:
    def __init__(self):
        # Attempt to resolve the connection string for local development
        conn_str = CONNECTION_STRING

        # 1. If it's the placeholder, try to load from local.settings.json
        if conn_str == "UseDevelopmentStorageAccount":
            try:
                settings_path = "telemetry-func/local.settings.json"
                if os.path.exists(settings_path):
                    with open(settings_path, "r") as f:
                        settings = json.load(f)
                        conn_str = settings.get("Values", {}).get("AzureWebJobsStorage", conn_str)
            except Exception as e:
                print(f"Warning: Could not load local.settings.json: {e}")

        # 2. Final Fallback: Azurite shorthand
        if conn_str == "UseDevelopmentStorageAccount":
            conn_str = "UseDevelopmentStorage=true"

        self.service_client = TableServiceClient.from_connection_string(conn_str)

    def get_table_client(self, table_name):
        return self.service_client.get_table_client(table_name)

storage = TableService()

@app.get("/")
async def read_index():
    return FileResponse("index.html")

@app.get("/api/users")
async def get_users():
    """Get list of all unique users from the Daily Summary table."""
    try:
        table_client = storage.get_table_client(DAILY_SUMMARY_TABLE)
        entities = table_client.list_entities()
        users = list(set([e['RowKey'] for e in entities]))
        return [{"user": u} for u in sorted(users)]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/user-summary")
async def get_user_summary(user: str, date: str):
    """Retrieve aggregated stats for a specific user and date."""
    try:
        table_client = storage.get_table_client(DAILY_SUMMARY_TABLE)
        entity = table_client.get_entity(partition_key=date, row_key=user)

        active_time = entity.get("total_active_time", 0)
        idle_time = entity.get("total_idle_time", 0)

        # Calculate Productivity Score (requires querying UserAppUsage)
        app_table = storage.get_table_client(APP_USAGE_TABLE)
        prod_time = 0
        # Convert to list so we can iterate twice without exhausting the iterator
        entities = list(app_table.query_entities(f"PartitionKey eq '{user}_{date}'"))
        for e in entities:
            if categorize_app(e['RowKey']) == "Productive":
                prod_time += e.get("total_active_seconds", 0)

        score = (prod_time / active_time * 100) if active_time > 0 else 0

        # Find top app
        top_app = "None"
        max_time = -1
        for e in entities:
            if e.get("total_active_seconds", 0) > max_time:
                max_time = e.get("total_active_seconds", 0)
                top_app = e['RowKey']

        return {
            "user": user,
            "date": date,
            "total_active_time": active_time,
            "total_idle_time": idle_time,
            "productivity_score": round(score, 1),
            "top_app": top_app
        }
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"User/Date not found: {str(e)}")

@app.get("/api/user-apps")
async def get_user_apps(user: str, date: str):
    """List all apps used by a user on a date with categories."""
    try:
        table_client = storage.get_table_client(APP_USAGE_TABLE)
        pk = f"{user}_{date}"
        entities = table_client.query_entities(f"PartitionKey eq '{pk}'")

        apps = []
        for e in entities:
            app_name = e['RowKey']
            apps.append({
                "app": app_name,
                "time": e.get("total_active_seconds", 0),
                "category": categorize_app(app_name)
            })
        return sorted(apps, key=lambda x: x['time'], reverse=True)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/user-timeline")
async def get_user_timeline(user: str, date: str):
    """Fetch chronological activity events from UserActivityLogs."""
    try:
        table_client = storage.get_table_client(ACTIVITY_LOGS_TABLE)
        pk = f"{user}_{date}"
        entities = table_client.query_entities(f"PartitionKey eq '{pk}'")

        timeline = []
        for e in entities:
            timeline.append({
                "timestamp": e.get("timestamp", ""),
                "active": e.get("active", False),
                "app": e.get("app", "Unknown")
            })
        timeline.sort(key=lambda x: x['timestamp'])
        return timeline
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
