"""
aggregator.py — Pure functions that turn raw event lists into dashboard-ready data.

No I/O.  No state.  Receives a list of dicts, returns computed results.
All aggregation that was previously done at write-time in the Azure Function
now happens here at read-time, on demand.

Enrichment rules
----------------
- Domain overrides app for categorisation (per spec)
- Consecutive events with the same app + active state are merged before counting
  (prevents double-counting when agent sends many short windows for the same app)
- Idle events (active=False) count towards total_idle_time but not productivity
"""

from typing import Any, Dict, List, Tuple

# ── Category vocabulary ─────────────────────────────────────────────────────────

BROWSER_APPS: set = {
    "chrome.exe",
    "msedge.exe",
    "firefox.exe",
    "brave.exe",
}

PRODUCTIVE_APPS: set = {
    "code.exe",
    "code - insiders.exe",
    "windowsterminal.exe",
    "powershell.exe",
    "cmd.exe",
    "excel.exe",
    "winword.exe",
    "powerpnt.exe",
}

PRODUCTIVE_DOMAINS: set = {
    "github.com",
    "stackoverflow.com",
    "docs.microsoft.com",
}

UNPRODUCTIVE_DOMAINS: set = {
    "youtube.com",
    "netflix.com",
    "instagram.com",
    "facebook.com",
    "twitter.com",
    "x.com",
}


# ── Core categorisation ─────────────────────────────────────────────────────────

def categorize(app: str, domain: str) -> str:
    """
    Assign Productive / Unproductive / Neutral to a single event.

    Domain takes priority over app name (per spec):
        if domain exists → use domain for categorisation first.
    Unproductive check runs before Productive so a YouTube tab on a work browser
    isn't accidentally called Productive.
    """
    app_l    = (app    or "").lower()
    domain_l = (domain or "").lower()

    if domain_l:
        if any(k in domain_l for k in UNPRODUCTIVE_DOMAINS):
            return "Unproductive"
        if any(k in domain_l for k in PRODUCTIVE_DOMAINS):
            return "Productive"

    if any(k in app_l for k in PRODUCTIVE_APPS):
        return "Productive"

    return "Neutral"


# ── Merge helper ────────────────────────────────────────────────────────────────

def _merge_consecutive(events: List[Dict]) -> List[Dict]:
    """
    Collapse back-to-back entries that share the same app AND active state.
    This is the "merge consecutive same-app events" rule from the spec.

    Example: three 30-second Code.exe events → one 90-second Code.exe event.
    Keeps the timestamp of the first event in each run.
    """
    if not events:
        return []

    merged = [dict(events[0])]
    for ev in events[1:]:
        last = merged[-1]
        if (ev["app"] == last["app"]
                and ev["active"] == last["active"]
                and ev.get("locked", False) == last.get("locked", False)):
            last["duration"] += ev["duration"]
        else:
            merged.append(dict(ev))
    return merged


# ── Public aggregation functions ────────────────────────────────────────────────

def aggregate_summary(events: List[Dict]) -> Dict[str, Any]:
    """
    Compute the daily KPI card data from raw events.

    Returns
    -------
    total_active_time   : int    seconds the user was active
    total_idle_time     : int    seconds the user was idle
    productivity_score  : float  productive_seconds / active_seconds * 100
    top_app             : str    app with the most active time
    """
    merged = _merge_consecutive(events)

    total_active    = 0
    total_idle      = 0   # screen on, no input (at desk but not interacting)
    total_locked    = 0   # screen locked / workstation away
    productive_secs = 0
    app_times: Dict[str, int] = {}

    for ev in merged:
        dur    = ev["duration"]
        locked = ev.get("locked", False)
        if ev["active"]:
            total_active            += dur
            app_times[ev["app"]]     = app_times.get(ev["app"], 0) + dur
            if categorize(ev["app"], ev.get("domain", "")) == "Productive":
                productive_secs += dur
        elif locked:
            total_locked += dur
        else:
            total_idle += dur

    top_app = max(app_times, key=app_times.get) if app_times else "None"
    score   = (productive_secs / total_active * 100) if total_active else 0.0

    return {
        "total_active_time":   total_active,
        "total_idle_time":     total_idle,
        "total_screen_off_time": total_locked,
        "productivity_score":  round(score, 1),
        "top_app":             top_app,
    }


def aggregate_apps(events: List[Dict]) -> List[Dict[str, Any]]:
    """
    Per-app usage totals with category, sorted by time descending.
    Idle events are excluded (we only count active app time).

    For browser apps, includes a `tabs` field: list of
    {"title": str, "time": int, "category": str} sorted by time desc.
    Non-browser apps have tabs=[].

    Tab times are aggregated from raw (unmerged) events so that switching
    between tabs within the same browser process is correctly split.
    """
    merged: List[Dict] = _merge_consecutive(events)

    app_data: Dict[str, Dict] = {}
    # tab_data[app][title] = seconds
    tab_data: Dict[str, Dict[str, int]] = {}

    for ev in merged:
        if not ev["active"]:
            continue
        app    = ev["app"]
        domain = ev.get("domain", "")
        if app not in app_data:
            app_data[app] = {"time": 0, "category": categorize(app, domain)}
        app_data[app]["time"] += ev["duration"]

    # Build per-tab totals from raw (unmerged) events so tab switches are counted
    for ev in events:
        if not ev["active"]:
            continue
        app    = ev["app"]
        domain = ev.get("domain", "")
        if app.lower() not in BROWSER_APPS:
            continue
        title = domain if domain else "(no title)"
        if app not in tab_data:
            tab_data[app] = {}
        tab_data[app][title] = tab_data[app].get(title, 0) + ev["duration"]

    result = []
    for app, data in app_data.items():
        tabs: List[Dict[str, Any]] = []
        if app.lower() in BROWSER_APPS and app in tab_data:
            tabs = sorted(
                [
                    {"title": title, "time": secs, "category": categorize(app, title)}
                    for title, secs in tab_data[app].items()
                ],
                key=lambda x: x["time"],
                reverse=True,
            )
        result.append({"app": app, "tabs": tabs, **data})

    return sorted(result, key=lambda x: x["time"], reverse=True)


def build_timeline(events: List[Dict]) -> List[Dict[str, Any]]:
    """
    Time-series for charting — consecutive same-app entries merged.

    Returns list of {"timestamp": str, "app": str, "active": bool, "duration": int}
    """
    return [
        {
            "timestamp": ev["timestamp"],
            "app":       ev["app"],
            "active":    ev["active"],
            "duration":  ev["duration"],
        }
        for ev in _merge_consecutive(events)
    ]
