"""
Analytics read endpoints — admin dashboard and per-device UI companion.

/api/users, /api/user-*, /api/me/*, /api/team-range
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request

from ..aggregator import aggregate_all, aggregate_apps, aggregate_summary, build_timeline
from ..auth import get_current_user
from ..deps import storage, verify_device_key

router = APIRouter()


# ── /api/me/* — per-device read endpoints (device key auth) ────────────────────

@router.get("/api/me/summary")
async def me_summary(request: Request, username: str = Depends(verify_device_key)):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return aggregate_summary(events)


@router.get("/api/me/apps")
async def me_apps(request: Request, username: str = Depends(verify_device_key)):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return aggregate_apps(events)


@router.get("/api/me/timeline")
async def me_timeline(request: Request, username: str = Depends(verify_device_key)):
    date   = request.query_params.get("date", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    events = storage.get_raw_events(username, date)
    return build_timeline(events)


# ── /api/* — admin dashboard endpoints (JWT or admin key) ──────────────────────

@router.get("/api/users")
async def get_users(_: dict = Depends(get_current_user)):
    try:
        return storage.get_users_with_aliases()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/user-summary")
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


@router.get("/api/user-apps")
async def get_user_apps(user: str, date: str, _: dict = Depends(get_current_user)):
    try:
        events = storage.get_raw_events(user, date)
        return aggregate_apps(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/user-timeline")
async def get_user_timeline(user: str, date: str, _: dict = Depends(get_current_user)):
    try:
        events = storage.get_raw_events(user, date)
        return build_timeline(events)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/api/user-data")
async def get_user_data(user: str, date: str, _: dict = Depends(get_current_user)):
    """Combined endpoint: summary + apps + timeline in one request."""
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


@router.get("/api/user-range")
async def get_user_range(
    user:  str,
    start: str,
    end:   str,
    _:     dict = Depends(get_current_user),
):
    """Per-user data over a date range. Max 31 days."""
    try:
        start_date = date.fromisoformat(start)
        end_date   = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end must be >= start")

    delta = (end_date - start_date).days + 1
    if delta > 31:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 31 days")

    day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    daily_list: list = []
    app_totals: dict = {}
    total_productive_secs = 0
    total_active_secs     = 0

    for i in range(delta):
        d     = start_date + timedelta(days=i)
        d_str = d.isoformat()
        events = storage.get_raw_events(user, d_str)

        if events:
            apps      = aggregate_apps(events)
            prod_secs = sum(a["time"] for a in apps if a["category"] == "Productive")
            act_secs  = sum(a["time"] for a in apps)
            score     = round(prod_secs / act_secs * 100, 1) if act_secs > 0 else 0
            total_productive_secs += prod_secs
            total_active_secs     += act_secs
            for a in apps:
                nm = a["app"]
                if nm not in app_totals:
                    app_totals[nm] = {"secs": 0, "category": a["category"]}
                app_totals[nm]["secs"] += a["time"]
        else:
            prod_secs = act_secs = score = 0

        daily_list.append({
            "date":               d_str,
            "day_name":           day_names[d.weekday()],
            "productive_hours":   round(prod_secs / 3600, 2),
            "active_hours":       round(act_secs  / 3600, 2),
            "productivity_score": score,
        })

    top_apps = sorted(
        [
            {"app": app, "hours": round(data["secs"] / 3600, 2), "category": data["category"]}
            for app, data in app_totals.items()
        ],
        key=lambda x: x["hours"],
        reverse=True,
    )[:50]

    avg_score = (
        round(total_productive_secs / total_active_secs * 100, 1)
        if total_active_secs > 0 else 0
    )

    return {
        "daily":    daily_list,
        "top_apps": top_apps,
        "summary":  {
            "total_productive_hours": round(total_productive_secs / 3600, 2),
            "total_active_hours":     round(total_active_secs     / 3600, 2),
            "avg_productivity_score": avg_score,
        },
    }


@router.get("/api/team-range")
async def get_team_range(
    start: str,
    end:   str,
    _:     dict = Depends(get_current_user),
):
    """Aggregate team data over a date range. Max 31 days."""
    try:
        start_date = date.fromisoformat(start)
        end_date   = date.fromisoformat(end)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid date format. Use YYYY-MM-DD.")

    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end must be >= start")

    delta = (end_date - start_date).days + 1
    if delta > 31:
        raise HTTPException(status_code=400, detail="Date range cannot exceed 31 days")

    users = storage.get_all_users()
    if not users:
        return {
            "daily":    [],
            "top_apps": [],
            "summary":  {"total_productive_hours": 0, "avg_productivity_score": 0,
                         "active_employees": 0, "total_employees": 0},
        }

    dates = [(start_date + timedelta(days=i)).isoformat() for i in range(delta)]
    pairs = [(u, d) for u in users for d in dates]

    def _fetch(pair):
        u, d = pair
        return u, d, storage.get_raw_events(u, d)

    loop = asyncio.get_event_loop()
    with ThreadPoolExecutor(max_workers=min(20, len(pairs))) as pool:
        results = await asyncio.gather(*[loop.run_in_executor(pool, _fetch, p) for p in pairs])

    daily: dict = {d: {"productive_secs": 0, "active_secs": 0, "users": set()} for d in dates}
    app_totals: dict = {}
    total_productive_secs = 0
    total_active_secs     = 0
    active_employees: set = set()

    for user, d, events in results:
        if not events:
            continue
        active_employees.add(user)
        apps = aggregate_apps(events)
        prod_secs = sum(a["time"] for a in apps if a["category"] == "Productive")
        act_secs  = sum(a["time"] for a in apps)

        daily[d]["productive_secs"] += prod_secs
        daily[d]["active_secs"]     += act_secs
        daily[d]["users"].add(user)
        total_productive_secs += prod_secs
        total_active_secs     += act_secs

        for a in apps:
            name = a["app"]
            if name not in app_totals:
                app_totals[name] = {"secs": 0, "category": a["category"]}
            app_totals[name]["secs"] += a["time"]

    daily_list = [
        {
            "date":              d,
            "productive_hours":  round(daily[d]["productive_secs"] / 3600, 2),
            "active_hours":      round(daily[d]["active_secs"]     / 3600, 2),
            "users_count":       len(daily[d]["users"]),
        }
        for d in dates
    ]

    top_apps = sorted(
        [
            {"app": app, "hours": round(data["secs"] / 3600, 2), "category": data["category"]}
            for app, data in app_totals.items()
        ],
        key=lambda x: x["hours"],
        reverse=True,
    )[:20]

    avg_score = (
        round(total_productive_secs / total_active_secs * 100, 1)
        if total_active_secs > 0 else 0
    )

    return {
        "daily":    daily_list,
        "top_apps": top_apps,
        "summary":  {
            "total_productive_hours": round(total_productive_secs / 3600, 2),
            "avg_productivity_score": avg_score,
            "active_employees":       len(active_employees),
            "total_employees":        len(users),
        },
    }
