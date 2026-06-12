"""
linux_telemetry_dashboard.py — terminal productivity dashboard.

Run via:  user-prod              (live full-screen, auto-refresh 10 s)
          user-prod --once       (one-shot snapshot, print and exit)
          user-prod --date YYYY-MM-DD   (historical date)

No external dependencies — stdlib only.
"""

import argparse
import concurrent.futures
import curses
import datetime
import getpass
import json
import os
import platform
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

DASHBOARD_VERSION  = "3.1"

DATA_DIR           = os.path.expanduser("~/.local/share/telemetry-agent")
CONFIG_PATH        = os.path.join(DATA_DIR, "config.json")
STATUS_PATH        = os.path.join(DATA_DIR, "status.json")
CACHE_PATH         = os.path.join(DATA_DIR, "cache.json")
INGEST_STATUS_PATH = os.path.join(DATA_DIR, "ingest-status.json")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_config() -> Dict[str, Any]:
    """Read ~/.local/share/telemetry-agent/config.json; return {} on error."""
    try:
        with open(CONFIG_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def _fmt_time(secs: int) -> str:
    """Format seconds as '4h 23m', '45m', or '0m'."""
    secs = max(0, int(secs))
    h = secs // 3600
    m = (secs % 3600) // 60
    if h > 0:
        return f"{h}h {m:02d}m"
    return f"{m}m"


def _read_local() -> Tuple[Optional[Dict], Optional[Dict]]:
    """Return (status_dict, cache_dict) from local files; None if missing."""
    status = None
    cache = None
    try:
        with open(STATUS_PATH, "r") as f:
            status = json.load(f)
    except Exception:
        pass
    try:
        with open(CACHE_PATH, "r") as f:
            cache = json.load(f)
    except Exception:
        pass
    return status, cache


def _agent_running() -> bool:
    """True if the agent wrote status.json recently (6× tick_interval, min 30s)."""
    try:
        cfg = _load_config()
        tick = int(cfg.get("tick_interval", 5))
        threshold = max(30, tick * 6)
        mtime = os.path.getmtime(STATUS_PATH)
        return (time.time() - mtime) < threshold
    except Exception:
        return False


def _last_ingest_age() -> Optional[int]:
    """
    Return seconds since the last successful ingest, or None if never.
    The agent writes ingest-status.json on every successful POST /ingest.
    """
    try:
        with open(INGEST_STATUS_PATH) as f:
            data = json.load(f)
        ts = data.get("last_success", "")
        if not ts:
            return None
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        age = int((datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds())
        return max(0, age)
    except Exception:
        return None


def _fetch_server(
    username: str, api_key: str, server_base: str, date_str: str
) -> Tuple[Optional[Dict], Optional[List], bool]:
    """Fetch summary + apps from server for a given date. Returns (summary, apps, conn_ok)."""
    headers = {"X-API-Key": api_key}
    summary = None
    apps = None
    conn_ok = False

    try:
        url_summary = (
            f"{server_base}/api/me/summary?user={username}&date={date_str}"
        )
        req = urllib.request.Request(url_summary, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            summary = json.loads(resp.read().decode())
        conn_ok = True
    except Exception:
        pass

    try:
        url_apps = f"{server_base}/api/me/apps?user={username}&date={date_str}"
        req = urllib.request.Request(url_apps, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            apps = json.loads(resp.read().decode())
        conn_ok = True
    except Exception:
        pass

    return summary, apps, conn_ok


def _fetch_one_day(
    username: str, api_key: str, server_base: str, date_str: str, label: str
) -> Dict[str, Any]:
    """Fetch a single day's summary for the week view."""
    try:
        headers = {"X-API-Key": api_key}
        url = f"{server_base}/api/me/summary?user={username}&date={date_str}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=3) as resp:
            data = json.loads(resp.read().decode())
            return {
                "date": date_str,
                "label": label,
                "active_secs": data.get("total_active_time", 0),
            }
    except Exception:
        return {"date": date_str, "label": label, "active_secs": 0}


def _fetch_week(
    username: str, api_key: str, server_base: str
) -> List[Dict[str, Any]]:
    """Fetch last 7 days concurrently; returns list sorted oldest→newest."""
    today = datetime.date.today()
    days = []
    for i in range(6, -1, -1):
        d = today - datetime.timedelta(days=i)
        days.append((d.strftime("%Y-%m-%d"), d.strftime("%a")))

    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=7) as exe:
        futures = {
            exe.submit(
                _fetch_one_day, username, api_key, server_base, ds, lbl
            ): (ds, lbl)
            for ds, lbl in days
        }
        for fut in concurrent.futures.as_completed(futures):
            try:
                results.append(fut.result())
            except Exception:
                ds, lbl = futures[fut]
                results.append({"date": ds, "label": lbl, "active_secs": 0})

    results.sort(key=lambda x: x["date"])
    return results


# ---------------------------------------------------------------------------
# Data builder
# ---------------------------------------------------------------------------


def build_display_data(date_str: str) -> Dict[str, Any]:
    """Merge local files + server data into a single display dict."""
    cfg = _load_config()
    ingest_url = cfg.get("ingest_url", "")
    api_key = cfg.get("api_key", "")
    server_base = re.sub(r"/ingest$", "", ingest_url).rstrip("/")

    status, cache = _read_local()

    # Defaults
    username = getpass.getuser()
    device = platform.node()
    app = "—"
    active = False
    locked = False
    idle_seconds = 0
    summary = {
        "total_active_time": 0,
        "total_idle_time": 0,
        "total_screen_off_time": 0,
        "productivity_score": 0.0,
        "top_app": "—",
    }
    top_apps: List[Dict] = []
    hourly_active = [0] * 24
    conn_ok = False

    # Read status
    domain = ""
    if status:
        app = status.get("app", "—")
        domain = status.get("domain", "")
        active = status.get("active", False)
        locked = status.get("locked", False)
        idle_seconds = status.get("idle_seconds", 0)

    # Read cache
    if cache:
        username = cache.get("username", username)
        device = cache.get("device", device)
        cached_summary = cache.get("summary", {})
        summary.update(cached_summary)
        top_apps = cache.get("top_apps", [])
        hourly_active = cache.get("hourly_active", [0] * 24)
        if len(hourly_active) < 24:
            hourly_active = hourly_active + [0] * (24 - len(hourly_active))

    # Optionally fetch from server (best-effort, silent failure)
    if server_base and api_key:
        try:
            srv_summary, srv_apps, conn_ok = _fetch_server(
                username, api_key, server_base, date_str
            )
            if srv_summary:
                summary.update(srv_summary)
            if srv_apps:
                top_apps = srv_apps
        except Exception:
            pass

    return {
        "username":        username,
        "device":          device,
        "app":             app,
        "domain":          domain,
        "active":          active,
        "locked":          locked,
        "idle_seconds":    idle_seconds,
        "summary":         summary,
        "top_apps":        top_apps,
        "hourly_active":   hourly_active,
        "conn_ok":         conn_ok,
        "server_base":     server_base,
        "api_key":         api_key,
        "agent_running":   _agent_running(),
        "last_ingest_age": _last_ingest_age(),
    }


# ---------------------------------------------------------------------------
# Curses drawing
# ---------------------------------------------------------------------------

BAR_CHARS = "░▁▂▃▄▅▆▇█"


def _bar(value: float, max_val: float, width: int) -> str:
    """Return a block bar string of given width."""
    if max_val <= 0 or width <= 0:
        return "░" * width
    ratio = min(1.0, value / max_val)
    filled = int(ratio * width)
    return "█" * filled + "░" * (width - filled)


def _mini_bar(value: float, max_val: float) -> str:
    """Return a single block character (8 levels)."""
    if max_val <= 0:
        return "░"
    ratio = min(1.0, value / max_val)
    idx = int(ratio * (len(BAR_CHARS) - 1))
    return BAR_CHARS[idx]


def _safe_addstr(win, y: int, x: int, text: str, attr: int = 0) -> None:
    """Add string, ignore curses errors at boundary."""
    try:
        win.addstr(y, x, text, attr)
    except curses.error:
        pass


def draw_screen(
    stdscr,
    data: Dict[str, Any],
    week_data: List[Dict[str, Any]],
    date_str: str,
    conn_status: bool,
) -> None:
    """Render the full-screen dashboard."""
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()

    if max_x < 80 or max_y < 20:
        _safe_addstr(stdscr, 0, 0, "Terminal too small (need 80x20)", curses.A_BOLD)
        stdscr.refresh()
        return

    # Color pairs
    C_GREEN = curses.color_pair(1)
    C_RED = curses.color_pair(2)
    C_YELLOW = curses.color_pair(3)
    C_CYAN = curses.color_pair(4)
    C_WHITE = curses.color_pair(5)
    C_MAGENTA = curses.color_pair(6)

    W = max_x
    username = data.get("username", getpass.getuser())
    device = data.get("device", platform.node())
    app = data.get("app", "—")
    domain = data.get("domain", "")
    active = data.get("active", False)
    locked = data.get("locked", False)
    idle_secs = data.get("idle_seconds", 0)
    summary = data.get("summary", {})
    top_apps = data.get("top_apps", [])
    hourly = data.get("hourly_active", [0] * 24)
    now_str = datetime.datetime.now().strftime("%H:%M:%S")

    active_time = summary.get("total_active_time", 0)
    idle_time = summary.get("total_idle_time", 0)
    screen_off = summary.get("total_screen_off_time", 0)
    score = summary.get("productivity_score", 0.0)
    productive_time = int(active_time * score / 100.0) if score > 0 else 0

    # Determine status dot color & label
    if locked:
        dot_color = C_RED
        status_label = "Away/Locked"
    elif active:
        dot_color = C_GREEN
        status_label = "Active"
    elif idle_secs > 60:
        dot_color = C_YELLOW
        status_label = "Idle"
    else:
        dot_color = C_GREEN
        status_label = "Active"

    row = 0

    # ── Row 0: Top border ──────────────────────────────────────────────────
    title     = f" TelemetryAgent v{DASHBOARD_VERSION} "
    user_info = f" {username}@{device} "
    time_info = f" {now_str} "
    left  = f"╔═{title}"
    right = f"{user_info}──{time_info}═╗"
    fill  = "─" * max(0, W - len(left) - len(right))
    _safe_addstr(stdscr, row, 0, left + fill + right, C_CYAN | curses.A_BOLD)
    row += 1

    # ── Row 1: Status line ─────────────────────────────────────────────────
    score_pct = f"{score:.0f}%"
    score_bar = _bar(score, 100, 14)
    status_line_left = f"║  "
    status_app = f"{status_label}   {app}"
    score_part = f"Score: {score_pct}  {score_bar}"
    # Reserve space for score on the right (score_x = W-2-len(score_part))
    domain_x = 4 + len(status_app) + 5          # col after "●  Active   Firefox  ›  "
    domain_max_w = max(0, (W - 2 - len(score_part)) - domain_x - 2)
    domain_trunc = domain[:domain_max_w] if domain and domain_max_w > 4 else ""
    _safe_addstr(stdscr, row, 0, "║  ", C_CYAN)
    _safe_addstr(stdscr, row, 2, "● ", dot_color | curses.A_BOLD)
    _safe_addstr(stdscr, row, 4, status_app, C_WHITE | curses.A_BOLD)
    if domain_trunc:
        _safe_addstr(stdscr, row, 4 + len(status_app), "  ›  ", C_CYAN)
        _safe_addstr(stdscr, row, domain_x, domain_trunc, C_YELLOW)
    score_x = W - 2 - len(score_part)
    _safe_addstr(stdscr, row, score_x, score_part, C_MAGENTA | curses.A_BOLD)
    _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
    row += 1

    # ── Row 2: Section divider TODAY / TOP APPS ────────────────────────────
    left_w = W // 2 - 1
    right_w = W - left_w - 3
    today_hdr = "═ TODAY "
    apps_hdr = "═ TOP APPS "
    today_fill = "═" * max(0, left_w - 2 - len(today_hdr))
    apps_fill = "═" * max(0, right_w - len(apps_hdr))
    divider = f"╠{today_hdr}{today_fill}╦{apps_hdr}{apps_fill}╣"
    _safe_addstr(stdscr, row, 0, divider[:W], C_CYAN | curses.A_BOLD)
    row += 1

    # ── Rows 3-6: TODAY stats + TOP APPS (side-by-side) ───────────────────
    today_rows = [
        ("Active    ", active_time, C_GREEN),
        ("Productive", productive_time, C_GREEN),
        ("Idle      ", idle_time, C_YELLOW),
        ("Screen Off", screen_off, C_RED),
    ]
    max_stat = max(active_time, 1)
    bar_w = 5

    for i, (label, secs, color) in enumerate(today_rows):
        bar_str = _bar(secs, max_stat, bar_w)
        time_str = _fmt_time(secs)
        left_text = f"  {label}: {time_str:>7}  {bar_str}"
        left_text = left_text[:left_w]
        left_text = left_text.ljust(left_w)

        _safe_addstr(stdscr, row, 0, "║", C_CYAN)
        _safe_addstr(stdscr, row, 1, f"  {label}: ", C_WHITE)
        _safe_addstr(stdscr, row, 1 + 2 + len(label) + 2, f"{time_str:>7}", color | curses.A_BOLD)
        _safe_addstr(stdscr, row, 1 + 2 + len(label) + 2 + 7 + 2, bar_str, color)
        _safe_addstr(stdscr, row, left_w + 1, "║", C_CYAN)

        # Right: top app
        if i < len(top_apps):
            a = top_apps[i]
            aname = a.get("app", "—")[:14]
            atime = _fmt_time(a.get("time", 0))
            acat = a.get("category", "")
            cat_color = C_GREEN if "Prod" in acat else (C_RED if "Unprod" in acat else C_WHITE)
            right_content = f"  ● {aname:<14} {atime:>7}  {acat}"
            right_content = right_content[:right_w]
            _safe_addstr(stdscr, row, left_w + 2, "  ● ", C_WHITE)
            _safe_addstr(stdscr, row, left_w + 6, f"{aname:<14}", C_WHITE | curses.A_BOLD)
            _safe_addstr(stdscr, row, left_w + 21, f" {atime:>7}", color)
            _safe_addstr(stdscr, row, left_w + 30, f"  {acat}", cat_color)
        _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
        row += 1

    # ── 24h activity divider ───────────────────────────────────────────────
    act_hdr = "═ 24-HOUR ACTIVITY "
    act_fill = "═" * max(0, W - 2 - len(act_hdr))
    _safe_addstr(stdscr, row, 0, f"╠{act_hdr}{act_fill}╣"[:W], C_CYAN | curses.A_BOLD)
    row += 1

    # ── 24h bar chart ──────────────────────────────────────────────────────
    if row < max_y - 6:
        _safe_addstr(stdscr, row, 0, "║  ", C_CYAN)
        max_hour = max(max(hourly), 1)
        chart_str = "".join(_mini_bar(v, max_hour) for v in hourly)
        _safe_addstr(stdscr, row, 3, chart_str, C_GREEN | curses.A_BOLD)
        _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
        row += 1

    if row < max_y - 5:
        _safe_addstr(stdscr, row, 0, "║", C_CYAN)
        time_labels = "  12am       6am         12pm        6pm         11pm"
        _safe_addstr(stdscr, row, 1, time_labels[:W - 2], C_WHITE)
        _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
        row += 1

    # ── 7-day activity divider ─────────────────────────────────────────────
    if row < max_y - 4:
        wk_hdr = "═ 7-DAY ACTIVITY "
        wk_fill = "═" * max(0, W - 2 - len(wk_hdr))
        _safe_addstr(stdscr, row, 0, f"╠{wk_hdr}{wk_fill}╣"[:W], C_CYAN | curses.A_BOLD)
        row += 1

    # ── 7-day chart ────────────────────────────────────────────────────────
    if row < max_y - 3 and week_data:
        max_week = max((d.get("active_secs", 0) for d in week_data), default=1)
        if max_week == 0:
            max_week = 1
        col_w = 8
        labels_row = "║  "
        bars_row = "║  "
        vals_row = "║  "
        for wd in week_data:
            lbl = wd.get("label", "---")[:3]
            secs = wd.get("active_secs", 0)
            bar_ch = _mini_bar(secs, max_week) if secs > 0 else "░"
            h_val = secs / 3600.0
            labels_row += f"{lbl:^{col_w}}"
            bars_row += f"{bar_ch:^{col_w}}"
            vals_row += f"{h_val:^{col_w}.1f}"

        _safe_addstr(stdscr, row, 0, labels_row[:W - 1], C_WHITE)
        _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
        row += 1
        if row < max_y - 2:
            _safe_addstr(stdscr, row, 0, bars_row[:W - 1], C_CYAN | curses.A_BOLD)
            _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
            row += 1
        if row < max_y - 2:
            _safe_addstr(stdscr, row, 0, vals_row[:W - 1], C_WHITE)
            _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
            row += 1

    # ── Status bar ─────────────────────────────────────────────────────────
    # Fill any gap rows
    while row < max_y - 2:
        _safe_addstr(stdscr, row, 0, "║" + " " * (W - 2) + "║", C_CYAN)
        row += 1

    if row < max_y - 1:
        _safe_addstr(stdscr, row, 0, "╠" + "═" * (W - 2) + "╣", C_CYAN | curses.A_BOLD)
        row += 1

    if row < max_y - 1:
        agent_running   = data.get("agent_running", False)
        last_ingest_age = data.get("last_ingest_age")   # int seconds or None

        # Agent indicator
        agent_dot   = "●" if agent_running else "○"
        agent_label = "Agent: "
        agent_state = "running" if agent_running else "stopped"
        agent_color = C_GREEN if agent_running else C_RED

        # Ingest indicator
        if last_ingest_age is None:
            ingest_label = "Ingest: never"
            ingest_color = C_RED
        elif last_ingest_age < 300:
            m, s = divmod(last_ingest_age, 60)
            ingest_label = f"Ingest: {m}m{s:02d}s ago" if m else f"Ingest: {s}s ago"
            ingest_color = C_GREEN
        else:
            h, rem = divmod(last_ingest_age, 3600)
            m = rem // 60
            ingest_label = f"Ingest: {h}h{m:02d}m ago" if h else f"Ingest: {m}m ago"
            ingest_color = C_YELLOW

        # Server indicator
        server_label = f"Server: {'✓' if conn_status else '✗'}"
        server_color = C_GREEN if conn_status else C_RED

        keys_str = "  [q]uit [r]efresh [←][→]date  "

        _safe_addstr(stdscr, row, 0, "║", C_CYAN)
        x = 1
        _safe_addstr(stdscr, row, x, keys_str, C_WHITE)
        x += len(keys_str)
        _safe_addstr(stdscr, row, x, agent_label, C_WHITE)
        x += len(agent_label)
        _safe_addstr(stdscr, row, x, agent_dot, agent_color | curses.A_BOLD)
        x += len(agent_dot)
        _safe_addstr(stdscr, row, x, agent_state + "  ", agent_color)
        x += len(agent_state) + 2
        _safe_addstr(stdscr, row, x, ingest_label + "  ", ingest_color | curses.A_BOLD)
        x += len(ingest_label) + 2
        _safe_addstr(stdscr, row, x, server_label, server_color | curses.A_BOLD)
        _safe_addstr(stdscr, row, W - 1, "║", C_CYAN)
        row += 1

    if row < max_y:
        _safe_addstr(stdscr, row, 0, "╚" + "═" * (W - 2) + "╝", C_CYAN | curses.A_BOLD)

    stdscr.refresh()


# ---------------------------------------------------------------------------
# --once mode
# ---------------------------------------------------------------------------


def run_once(date_str: str) -> None:
    """Print a plain-text snapshot and exit."""
    data = build_display_data(date_str)
    username = data.get("username", getpass.getuser())
    device = data.get("device", platform.node())
    app = data.get("app", "—")
    active = data.get("active", False)
    locked = data.get("locked", False)
    summary = data.get("summary", {})
    top_apps = data.get("top_apps", [])
    conn_ok = data.get("conn_ok", False)

    active_time = summary.get("total_active_time", 0)
    idle_time = summary.get("total_idle_time", 0)
    screen_off = summary.get("total_screen_off_time", 0)
    score = summary.get("productivity_score", 0.0)
    top_app = summary.get("top_app", "—")
    productive_time = int(active_time * score / 100.0) if score > 0 else 0

    if locked:
        status_label = "Away/Locked"
    elif active:
        status_label = f"Active ({app})"
    else:
        status_label = f"Idle ({app})"

    agent_running   = data.get("agent_running", False)
    last_ingest_age = data.get("last_ingest_age")

    if last_ingest_age is None:
        ingest_str = "never"
    elif last_ingest_age < 60:
        ingest_str = f"{last_ingest_age}s ago"
    elif last_ingest_age < 3600:
        ingest_str = f"{last_ingest_age // 60}m ago"
    else:
        ingest_str = f"{last_ingest_age // 3600}h ago"

    print(f"TelemetryAgent v{DASHBOARD_VERSION} — {username}@{device} — {date_str}")
    print(f"Agent    : {'● running' if agent_running else '○ stopped'}")
    print(f"Ingest   : {ingest_str}")
    print(f"Server   : {'connected' if conn_ok else 'offline'}")
    print(f"Status   : {status_label}")
    print(f"Score    : {score:.1f}%")
    print(f"Active   : {_fmt_time(active_time)}")
    print(f"Productive: {_fmt_time(productive_time)}")
    print(f"Idle     : {_fmt_time(idle_time)}")
    print(f"Screen Off: {_fmt_time(screen_off)}")
    print(f"Top App  : {top_app}")
    print()
    if top_apps:
        print("Top Apps:")
        for a in top_apps:
            aname = a.get("app", "—")
            atime = _fmt_time(a.get("time", 0))
            acat = a.get("category", "")
            print(f"  ● {aname:<16} {atime}  {acat}")
        print()


# ---------------------------------------------------------------------------
# Curses main loop
# ---------------------------------------------------------------------------


def _curses_main(stdscr, args) -> None:
    """Inner curses loop."""
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_RED, -1)
    curses.init_pair(3, curses.COLOR_YELLOW, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_WHITE, -1)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)
    curses.curs_set(0)
    stdscr.timeout(500)

    max_y, max_x = stdscr.getmaxyx()
    if max_x < 80 or max_y < 20:
        stdscr.addstr(0, 0, "Terminal too small (need 80x20)")
        stdscr.refresh()
        time.sleep(2)
        return

    today = datetime.date.today()
    if args.date:
        try:
            current_date = datetime.date.fromisoformat(args.date)
        except ValueError:
            current_date = today
    else:
        current_date = today

    # Shared mutable containers for background thread results
    display_data_box: List[Optional[Dict]] = [None]
    week_data_box: List[Optional[List]] = [None]
    conn_status_box: List[bool] = [False]
    fetching_box: List[bool] = [False]

    def _background_fetch(date_str: str) -> None:
        fetching_box[0] = True
        try:
            d = build_display_data(date_str)
            display_data_box[0] = d
            conn_status_box[0] = d.get("conn_ok", False)

            cfg = _load_config()
            ingest_url = cfg.get("ingest_url", "")
            api_key = cfg.get("api_key", "")
            server_base = re.sub(r"/ingest$", "", ingest_url).rstrip("/")
            username = d.get("username", getpass.getuser())
            if server_base and api_key:
                week = _fetch_week(username, api_key, server_base)
                week_data_box[0] = week
        except Exception:
            pass
        finally:
            fetching_box[0] = False

    # Empty placeholder shown while the first background fetch is in progress.
    # Using build_display_data() as a fallback would block the draw loop for up
    # to 6 s (two urlopen calls × 3 s timeout) on the very first render.
    _empty: dict[str, Any] = {
        "username": getpass.getuser(), "device": platform.node(),
        "app": "—", "active": False, "locked": False, "idle_seconds": 0,
        "summary": {}, "top_apps": [], "hourly_active": [0] * 24,
        "conn_ok": False, "agent_running": _agent_running(),
        "last_ingest_age": _last_ingest_age(),
    }

    # Initial load
    _background_fetch(current_date.isoformat())

    ticks = 0
    REFRESH_TICKS = 20  # 20 × 500ms = 10 s

    while True:
        # Draw with whatever data is available; never block the draw loop
        d = display_data_box[0] or _empty
        w = week_data_box[0] or []
        draw_screen(stdscr, d, w, current_date.isoformat(), conn_status_box[0])

        ch = stdscr.getch()
        if ch in (ord("q"), ord("Q")):
            break
        elif ch == ord("r"):
            ticks = REFRESH_TICKS  # force refresh on next tick
        elif ch == curses.KEY_LEFT:
            current_date -= datetime.timedelta(days=1)
            display_data_box[0] = None
            t = threading.Thread(
                target=_background_fetch,
                args=(current_date.isoformat(),),
                daemon=True,
            )
            t.start()
            ticks = 0
        elif ch == curses.KEY_RIGHT:
            if current_date < today:
                current_date += datetime.timedelta(days=1)
                display_data_box[0] = None
                t = threading.Thread(
                    target=_background_fetch,
                    args=(current_date.isoformat(),),
                    daemon=True,
                )
                t.start()
                ticks = 0
        elif ch == curses.KEY_RESIZE:
            stdscr.clear()

        ticks += 1
        if ticks >= REFRESH_TICKS and not fetching_box[0]:
            ticks = 0
            t = threading.Thread(
                target=_background_fetch,
                args=(current_date.isoformat(),),
                daemon=True,
            )
            t.start()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="user-prod",
        description="Terminal productivity dashboard (stdlib only).",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Print one-shot snapshot and exit (no curses).",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Show data for a historical date.",
    )
    args = parser.parse_args()

    date_str = args.date or datetime.date.today().isoformat()

    if args.once:
        run_once(date_str)
        return

    try:
        curses.wrapper(_curses_main, args)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
