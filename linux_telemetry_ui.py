"""
linux_telemetry_ui.py — Linux system-tray UI companion for the Linux telemetry agent.

Mirrors telemetry_ui.py (Windows) exactly in layout, data flow, and feature set.

Architecture
------------
  Agent (linux_telemetry_agent.py)
    └─ writes  ~/.local/share/telemetry-agent/status.json   every ~5 s
    └─ writes  ~/.local/share/telemetry-agent/cache.json    every ~30 s

  This process
    └─ reads status.json + cache.json  → primary data source (offline-safe)
    └─ calls server /api/me/*          → richer data when reachable
    └─ pystray tray icon + tkinter popup  (same layout as Windows UI)

Dependencies:
    pip install pystray pillow requests
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
except ModuleNotFoundError:
    print(
        "[error] tkinter is not installed.\n"
        "  tkinter is a system package and cannot be installed via pip.\n"
        "  Fix with:\n"
        "    sudo apt-get install -y python3-tk          # Debian / Ubuntu / MATE\n"
        "    sudo dnf install -y python3-tkinter          # Fedora / RHEL\n"
        "    sudo pacman -S --needed tk                   # Arch\n"
        "  Then restart the UI:  ~/.local/bin/telemetry-ui",
        file=sys.stderr,
    )
    sys.exit(1)

import pystray
import requests
from PIL import Image, ImageDraw

_DESKTOP = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()

# ── XDG paths ─────────────────────────────────────────────────────────────────────
_XDG_DATA   = os.environ.get("XDG_DATA_HOME",  os.path.expanduser("~/.local/share"))
_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))

DATA_DIR    = os.path.join(_XDG_DATA,   "telemetry-agent")
CONFIG_DIR  = os.path.join(_XDG_CONFIG, "telemetry-agent")

STATUS_PATH     = os.path.join(DATA_DIR,   "status.json")
CACHE_PATH      = os.path.join(DATA_DIR,   "cache.json")
BACKUP_DB_PATH  = os.path.join(DATA_DIR,   "backup.db")
# Agent writes config.json to DATA_DIR (moved from CONFIG_DIR to avoid ~/.config permission issues)
CONFIG_PATH     = os.path.join(DATA_DIR,   "config.json")
CONFIG_PATH_OLD = os.path.join(CONFIG_DIR, "config.json")   # legacy path, kept for read fallback
SYSTEM_CONFIG   = "/etc/telemetry-agent/config.json"

AUTOSTART_DIR   = os.path.join(_XDG_CONFIG, "autostart")

# ── Config ────────────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    for path in [SYSTEM_CONFIG, CONFIG_PATH, CONFIG_PATH_OLD,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.config.json")]:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            continue
    return {}

_cfg          = _load_config()
_SERVER_BASE  = (_cfg.get("ingest_url", "") or "").replace("/ingest", "").rstrip("/")
_DEVICE_KEY   = _cfg.get("api_key", "")
_AUTO_REFRESH = 30   # seconds

UI_VERSION = "3.1"

# ── Update state (set by check_for_update, read by tray menu) ──────────────────
_update_available     = False
_server_new_version   = ""
_server_download_urls: dict = {}

# ── Theme ─────────────────────────────────────────────────────────────────────────
def _detect_dark_mode() -> bool:
    """Detect system dark mode via gsettings (GNOME) or KDE config."""
    def _run(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=1).stdout.strip()
        except Exception:
            return ""

    # GNOME (new)
    out = _run(["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"])
    if "dark" in out.lower():
        return True

    # GNOME (legacy GTK theme name)
    out = _run(["gsettings", "get", "org.gnome.desktop.interface", "gtk-theme"])
    if "dark" in out.lower():
        return True

    # KDE
    kconf = Path.home() / ".config" / "kdeglobals"
    try:
        if kconf.exists():
            text = kconf.read_text(errors="replace").lower()
            if "colorscheme=breezedark" in text or "colorscheme=oxygen cold" in text:
                return True
            if re.search(r"colorscheme=\w*dark", text):
                return True
    except Exception:
        pass

    # Fallback: check GTK_THEME env var
    gtk_theme = os.environ.get("GTK_THEME", "")
    if "dark" in gtk_theme.lower():
        return True

    return False


def _make_theme(dark: bool) -> dict:
    if dark:
        return dict(
            BG="#1e1e2e", BG2="#181825", BG3="#24243e",
            BORDER="#313244", TEXT="#cdd6f4", MUTED="#6c7086",
            GREEN="#a6e3a1", RED="#f38ba8", BLUE="#89b4fa",
            YELLOW="#f9e2af", INDIGO="#b4befe",
        )
    return dict(
        BG="#ffffff", BG2="#f8fafc", BG3="#f1f5f9",
        BORDER="#e2e8f0", TEXT="#1e293b", MUTED="#64748b",
        GREEN="#16a34a", RED="#dc2626", BLUE="#3b82f6",
        YELLOW="#d97706", INDIGO="#6366f1",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────────
def _get_username() -> str:
    """Safe username lookup — os.getlogin() fails in non-TTY environments."""
    return (
        os.environ.get("USER")
        or os.environ.get("LOGNAME")
        or os.environ.get("USERNAME")
        or (lambda: __import__("getpass").getuser())()
    )


def _fmt_time(secs: int) -> str:
    if secs <= 0:
        return "0m"
    h, m = divmod(int(secs), 3600)
    m = m // 60
    return f"{h}h {m}m" if h else f"{m}m"


def _bind_hover(button: tk.Widget, hover_fg: str, hover_bg: str | None = None) -> None:
    """
    Attach <Enter>/<Leave> handlers that tint the widget on hover.

    Mirrors the Windows UI helper.  Tkinter's `activebackground` only fires
    for the *pressed* state on most widgets — mouse hover alone does nothing.
    This is a common complaint: a flat-header button looks dead until you
    click it.  We capture the original fg/bg at bind time so theme changes
    don't leave the widget stuck on the hover color.
    """
    try:
        orig_fg = button.cget("fg")
        orig_bg = button.cget("bg")
    except Exception:
        return
    target_bg = hover_bg if hover_bg is not None else orig_bg

    def _on_enter(_e):
        try:
            button.configure(fg=hover_fg, bg=target_bg)
        except Exception:
            pass

    def _on_leave(_e):
        try:
            button.configure(fg=orig_fg, bg=orig_bg)
        except Exception:
            pass

    button.bind("<Enter>", _on_enter, add="+")
    button.bind("<Leave>", _on_leave, add="+")


def _ver(v: str) -> tuple:
    """
    Robust semver-ish parser.  See telemetry_agent._ver() for full rationale.
    """
    if not v:
        return (0,)
    out: list = []
    for part in str(v).strip().split("."):
        digits = ""
        for ch in part:
            if ch.isdigit():
                digits += ch
            else:
                break
        try:
            out.append(int(digits) if digits else 0)
        except Exception:
            out.append(0)
    return tuple(out) or (0,)


# ── Self-update (detection + manual trigger) ──────────────────────────────────────

def _write_update_log(msg: str) -> None:
    """Append a timestamped message to DATA_DIR/update.log."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        path = os.path.join(DATA_DIR, "update.log")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()} | {msg}\n")
    except Exception:
        pass


def check_for_update() -> None:
    """
    Detection-only.  Queries /api/health; if the server reports a
    newer version, sets _update_available so the tray menu can show
    an "Update Available" button.  Skips silently on any error.
    """
    global _update_available, _server_new_version, _server_download_urls
    if not _SERVER_BASE:
        return
    try:
        resp = requests.get(f"{_SERVER_BASE}/api/health", timeout=10, verify=True)
        if not resp.ok:
            _update_available = False
            return
        data           = resp.json()
        server_version = data.get("version", "0")
        if _ver(server_version) > _ver(UI_VERSION):
            _update_available = True
            _server_new_version = server_version
            _server_download_urls = {
                "agent": data.get("linux_agent_download_url", f"{_SERVER_BASE}/download-linux-agent"),
                "ui":    data.get("linux_ui_download_url",    f"{_SERVER_BASE}/download-linux-ui"),
            }
        else:
            _update_available = False
    except Exception:
        pass


def _perform_update() -> None:
    """
    Called when the user clicks "Update Available" in the tray menu.
    Downloads new agent + UI scripts, replaces them on disk, restarts
    the agent, and re-execs the UI.
    """
    global _update_available
    if not _update_available or not _server_download_urls:
        return

    import tempfile
    tmp_dir = os.path.join(tempfile.gettempdir(), "telemetry-agent")
    os.makedirs(tmp_dir, exist_ok=True)

    script_dir    = os.path.dirname(os.path.abspath(__file__))
    agent_path    = os.path.join(script_dir, "linux_telemetry_agent.py")
    ui_path       = os.path.join(script_dir, "linux_telemetry_ui.py")
    tmp_agent     = os.path.join(tmp_dir, "linux_telemetry_agent_new.py")
    tmp_ui        = os.path.join(tmp_dir, "linux_telemetry_ui_new.py")

    # Download new agent script
    try:
        with requests.get(_server_download_urls["agent"], stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp_agent, "w", encoding="utf-8") as f:
                for chunk in r.iter_content(chunk_size=65536, decode_unicode=True):
                    f.write(chunk)
    except Exception as e:
        _write_update_log(f"Agent download failed: {e}")
        return

    if os.path.getsize(tmp_agent) < 1024:
        _write_update_log("Agent download too small — aborting")
        return

    # Download new UI script
    try:
        with requests.get(_server_download_urls["ui"], stream=True, timeout=60) as r:
            r.raise_for_status()
            with open(tmp_ui, "w", encoding="utf-8") as f:
                for chunk in r.iter_content(chunk_size=65536, decode_unicode=True):
                    f.write(chunk)
    except Exception as e:
        _write_update_log(f"UI download failed: {e}")
        return

    if os.path.getsize(tmp_ui) < 1024:
        _write_update_log("UI download too small — aborting")
        return

    _update_available = False

    # Replace agent script
    try:
        shutil.copy2(tmp_agent, agent_path)
        mode = os.stat(agent_path).st_mode | 0o111
        os.chmod(agent_path, mode)
    except Exception as e:
        _write_update_log(f"Agent replace failed: {e}")
        return

    # Replace UI script
    try:
        shutil.copy2(tmp_ui, ui_path)
        mode = os.stat(ui_path).st_mode | 0o111
        os.chmod(ui_path, mode)
    except Exception as e:
        _write_update_log(f"UI replace failed: {e}")
        return

    # Cleanup temp files
    for p in [tmp_agent, tmp_ui]:
        try:
            os.remove(p)
        except Exception:
            pass

    _write_update_log("Update applied — restarting")

    # Restart the agent so it picks up the new version immediately.
    # Spawning a new process directly fails silently: the running agent holds
    # an exclusive fcntl lock on agent.pid, so the new process exits immediately
    # via _acquire_singleton().  Use systemctl if available; otherwise kill the
    # old process first to release the lock before spawning.
    _agent_restarted = False
    try:
        r = subprocess.run(
            ["systemctl", "--user", "restart", "telemetry-agent.service"],
            capture_output=True, timeout=10,
        )
        if r.returncode == 0:
            _agent_restarted = True
    except Exception:
        pass
    if not _agent_restarted:
        try:
            subprocess.run(["pkill", "-f", "linux_telemetry_agent.py"],
                           capture_output=True, timeout=5)
            time.sleep(2)
        except Exception:
            pass
        try:
            subprocess.Popen([sys.executable, agent_path], close_fds=True)
        except Exception:
            pass

    # Re-exec the UI in-place
    try:
        os.execv(sys.executable, [sys.executable, ui_path] + sys.argv[1:])
    except Exception as e:
        _write_update_log(f"Re-exec failed: {e}")
        # Fallback: start a new process
        subprocess.Popen([sys.executable, ui_path] + sys.argv[1:], close_fds=True)


# ── Backup events (read from SQLite) ─────────────────────────────────────────────
def read_backup_events(date_str: str) -> list:
    if not os.path.exists(BACKUP_DB_PATH):
        return []
    events: list = []
    try:
        conn = sqlite3.connect(BACKUP_DB_PATH, timeout=2)
        rows = conn.execute(
            "SELECT events_json FROM offline_batches WHERE created_at LIKE ?",
            (date_str + "%",),
        ).fetchall()
        conn.close()
        for row in rows:
            events.extend(json.loads(row[0]))
    except Exception:
        pass
    return events


# ── Local aggregation ─────────────────────────────────────────────────────────────
_UNPRODUCTIVE_KEYWORDS = {
    "youtube", "netflix", "instagram", "facebook", "twitter", "x.com",
    "tiktok", "snapchat", "reddit", "twitch", "prime video", "disney+",
    "hbo max", "pinterest", "tumblr", "9gag", "imgur", "espn",
    "steam store", "epic games store", "whatsapp", "telegram",
    "spotify", "steam", "vlc",
}
_PRODUCTIVE_APP_KEYWORDS = {
    "code", "cursor", "pycharm", "idea", "rider", "clion", "goland", "webstorm",
    "terminal", "gnome-terminal", "konsole", "xterm", "bash", "zsh",
    "postman", "insomnia", "dbeaver", "docker", "nvim", "vim", "emacs",
    "libreoffice", "thunderbird", "teams", "slack", "zoom", "notion",
}


def _local_categorize(app: str, domain: str) -> str:
    al = (app or "").lower()
    dl = (domain or "").lower()
    if any(k in dl for k in _UNPRODUCTIVE_KEYWORDS):
        return "Unproductive"
    if any(k in al for k in _UNPRODUCTIVE_KEYWORDS):
        return "Unproductive"
    return "Productive"


def aggregate_backup(events: list) -> dict:
    total_active = total_idle = total_locked = prod_secs = 0
    app_times: dict = {}
    app_cat:   dict = {}
    hourly = [0] * 24
    for ev in events:
        dur    = ev.get("duration", 0)
        locked = ev.get("locked", False)
        active = ev.get("active", False)
        app    = ev.get("app", "Unknown")
        domain = ev.get("domain", "")
        cat    = _local_categorize(app, domain)
        if active:
            total_active += dur
            app_times[app] = app_times.get(app, 0) + dur
            app_cat[app]   = cat
            if cat == "Productive":
                prod_secs += dur
            try:
                ts = datetime.fromisoformat(ev["timestamp"]).astimezone()
                hourly[ts.hour] = min(hourly[ts.hour] + dur, 3600)
            except Exception:
                pass
        elif locked:
            total_locked += dur
        else:
            total_idle += dur
    top_app = max(app_times, key=app_times.get) if app_times else "—"
    score   = round(prod_secs / total_active * 100, 1) if total_active else 0.0
    apps = sorted(
        [{"app": a, "time": t, "category": app_cat[a]} for a, t in app_times.items()],
        key=lambda x: x["time"], reverse=True,
    )
    return {
        "summary": {
            "total_active_time":     total_active,
            "total_idle_time":       total_idle,
            "total_screen_off_time": total_locked,
            "productivity_score":    score,
            "top_app":               top_app,
        },
        "apps":        apps,
        "prod_secs":   sum(a["time"] for a in apps if a["category"] == "Productive"),
        "unprod_secs": sum(a["time"] for a in apps if a["category"] == "Unproductive"),
        "hourly":      hourly,
    }


# ── Data layer ────────────────────────────────────────────────────────────────────
def read_local() -> tuple:
    status = cache = None
    try:
        with open(STATUS_PATH) as f:
            status = json.load(f)
    except Exception:
        pass
    try:
        with open(CACHE_PATH) as f:
            cache = json.load(f)
    except Exception:
        pass
    return status, cache


def _server_available() -> bool:
    if not _SERVER_BASE or not _DEVICE_KEY:
        return False
    try:
        r = requests.get(f"{_SERVER_BASE}/api/health",
                         headers={"X-API-Key": _DEVICE_KEY}, timeout=2)
        return r.ok
    except Exception:
        return False


def fetch_server(date_str: str) -> tuple:
    if not _SERVER_BASE or not _DEVICE_KEY:
        return None, [], [], "not_configured"

    username    = _get_username()
    headers     = {"X-API-Key": _DEVICE_KEY}
    conn_status = "ok"

    def _get(path, default):
        nonlocal conn_status
        if conn_status in ("unreachable", "maintenance"):
            return default
        try:
            r = requests.get(
                f"{_SERVER_BASE}{path}?user={username}&date={date_str}",
                headers=headers, timeout=5,
            )
            if r.ok:
                return r.json()
            conn_status = "maintenance" if r.status_code in (502, 503, 504) else "error"
            return default
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout):
            conn_status = "unreachable"
            return default
        except Exception:
            conn_status = "error"
            return default

    summary  = _get("/api/me/summary",  None)
    apps     = _get("/api/me/apps",     [])
    timeline = _get("/api/me/timeline", [])
    return summary, apps, timeline, conn_status


def fetch_server_week(days: int = 7) -> list:
    if not _SERVER_BASE or not _DEVICE_KEY:
        return []
    if not _server_available():
        return []

    today    = datetime.now(timezone.utc).date()
    dates    = [today - timedelta(days=i) for i in range(days - 1, -1, -1)]
    username = _get_username()
    headers  = {"X-API-Key": _DEVICE_KEY}

    def _one(d):
        date_str = d.isoformat()
        try:
            rs = requests.get(
                f"{_SERVER_BASE}/api/me/summary?user={username}&date={date_str}",
                headers=headers, timeout=5,
            )
            summary = rs.json() if rs.ok else {}
        except Exception:
            summary = {}
        try:
            ra   = requests.get(
                f"{_SERVER_BASE}/api/me/apps?user={username}&date={date_str}",
                headers=headers, timeout=5,
            )
            apps = ra.json() if ra.ok else []
        except Exception:
            apps = []
        return {
            "date":        date_str,
            "label":       d.strftime("%a"),
            "active_secs": summary.get("total_active_time", 0),
            "prod_secs":   sum(a.get("time", 0) for a in apps
                               if a.get("category") == "Productive"),
        }

    with ThreadPoolExecutor(max_workers=7) as ex:
        return list(ex.map(_one, dates))


def _make_banner(conn_status: str, summary, backup_agg) -> tuple | None:
    if conn_status == "ok":
        return None
    has_data = summary is not None or backup_agg is not None
    if conn_status == "not_configured":
        return ("ℹ  No server configured — showing local data only", "info")
    if conn_status == "maintenance":
        msg = "🔧  Server under maintenance" + (
            " — showing cached data" if has_data else " — no local data available")
        return (msg, "warn")
    if conn_status in ("unreachable", "error"):
        if has_data:
            src = "cached" if backup_agg is None else "backup"
            return (f"⚠  Server unreachable — showing {src} data", "warn")
        return ("✗  Server unreachable — no local data. Check network.", "error")
    return None


def _timeline_to_hourly(timeline: list) -> list:
    hourly = [0] * 24
    for entry in timeline:
        if not entry.get("active"):
            continue
        try:
            ts = datetime.fromisoformat(entry["timestamp"]).astimezone()
            hourly[ts.hour] = min(hourly[ts.hour] + entry.get("duration", 0), 3600)
        except Exception:
            pass
    return hourly


def build_display_data(date_str: str | None = None) -> dict:
    today    = datetime.now(timezone.utc).date().isoformat()
    if not date_str:
        date_str = today
    is_today = date_str == today

    status, cache = (read_local() if is_today else (None, None))
    summary, apps, timeline, conn_status = fetch_server(date_str)
    server_ok = summary is not None and conn_status == "ok"

    if is_today:
        app       = (status or {}).get("app",    "Unknown")
        active    = (status or {}).get("active", False)
        locked    = (status or {}).get("locked", False)
        status_ts = (status or {}).get("timestamp", "")
        if locked:
            status_label, status_color = "Away",   "#dc2626"
        elif not active:
            status_label, status_color = "Idle",   "#d97706"
        else:
            status_label, status_color = "Active", "#16a34a"
    else:
        app = "—"
        status_label, status_color = "Historical", "#64748b"
        status_ts = ""

    backup_agg = None
    if summary is None and is_today and cache:
        summary = cache.get("summary")
    if summary is None:
        backup_events = read_backup_events(date_str)
        if backup_events:
            backup_agg = aggregate_backup(backup_events)
            summary    = backup_agg["summary"]

    active_secs     = (summary or {}).get("total_active_time",     0)
    idle_secs_val   = (summary or {}).get("total_idle_time",       0)
    screen_off_secs = (summary or {}).get("total_screen_off_time", 0)
    score           = (summary or {}).get("productivity_score",    0.0)
    top_app         = (summary or {}).get("top_app", "—")

    if not apps and is_today and cache:
        apps = cache.get("top_apps", [])
    if not apps and backup_agg:
        apps = backup_agg["apps"]

    prod_secs   = (backup_agg["prod_secs"]   if backup_agg and not server_ok
                   else sum(a.get("time", 0) for a in apps if a.get("category") == "Productive"))
    unprod_secs = (backup_agg["unprod_secs"] if backup_agg and not server_ok
                   else sum(a.get("time", 0) for a in apps if a.get("category") == "Unproductive"))

    if timeline:
        hourly = _timeline_to_hourly(timeline)
    elif is_today and cache:
        hourly = cache.get("hourly_active", [0] * 24)
    elif backup_agg:
        hourly = backup_agg["hourly"]
    else:
        hourly = [0] * 24

    if not is_today:
        last_updated = f"Server — {date_str}" if server_ok else f"No data — {date_str}"
    elif server_ok:
        last_updated = f"Server  {datetime.now().strftime('%H:%M:%S')}"
    elif status_ts:
        try:
            ts = datetime.fromisoformat(status_ts).astimezone()
            last_updated = f"Local  {ts.strftime('%H:%M:%S')}"
        except Exception:
            last_updated = "Local cache"
    elif backup_agg:
        last_updated = f"Backup  {datetime.now().strftime('%H:%M:%S')}"
    else:
        last_updated = "No data"

    return {
        "app":             app,
        "is_today":        is_today,
        "status_label":    status_label,
        "status_color":    status_color,
        "score":           score,
        "active_secs":     active_secs + idle_secs_val,
        "prod_secs":       prod_secs,
        "unprod_secs":     unprod_secs,
        "idle_secs":       idle_secs_val,
        "screen_off_secs": screen_off_secs,
        "top_app":         top_app,
        "hourly":          hourly,
        "top_apps":        apps[:10],
        "last_updated":    last_updated,
        "server_ok":       server_ok,
        "conn_status":     conn_status,
        "banner":          _make_banner(conn_status, summary, backup_agg),
    }


# ═════════════════════════════════════════════════════════════════════════════════
# Dashboard window  (identical layout to Windows UI)
# ═════════════════════════════════════════════════════════════════════════════════

class DashboardWindow:
    CW, CH = 380, 610
    FW, FH = 1100, 700

    def __init__(self, root: tk.Tk):
        self._root          = root
        self._dark          = _detect_dark_mode()
        self._theme         = _make_theme(self._dark)
        self._mode          = "compact"
        self._selected_date = datetime.now(timezone.utc).date().isoformat()
        self._week_data:    list = []
        self._refresh_job:  str | None = None
        self._drag_start:   tuple | None = None
        self._cal_visible   = False
        self._cal_year      = 0
        self._cal_month     = 0
        self._cal_day_btns: list = []
        self._week_cv:      tk.Canvas | None = None

        self._win = tk.Toplevel(root)
        self._win.overrideredirect(True)
        self._win.configure(bg=self._theme["BG"])
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.98)

        self._position_compact()
        self._build_compact()
        self._start_theme_monitor()
        self.refresh()

    @property
    def T(self) -> dict:
        return self._theme

    def _start_theme_monitor(self):
        def _check():
            if not self._win.winfo_exists():
                return
            new_dark = _detect_dark_mode()
            if new_dark != self._dark:
                self._dark  = new_dark
                self._theme = _make_theme(new_dark)
                self._win.configure(bg=self.T["BG"])
                self._rebuild(self._mode)
            self._win.after(60_000, _check)
        self._win.after(60_000, _check)

    def _rebuild(self, mode: str):
        self._mode = mode
        if self._refresh_job:
            self._win.after_cancel(self._refresh_job)
            self._refresh_job = None
        for w in self._win.winfo_children():
            w.destroy()
        self._cal_visible = False
        self._win.configure(bg=self.T["BG"])
        if mode == "compact":
            self._position_compact()
            self._build_compact()
        else:
            self._position_full()
            self._build_full()
        self.refresh()
        if mode == "full":
            threading.Thread(target=self._fetch_week, daemon=True).start()

    def show(self):
        self._win.deiconify()
        self._win.lift()
        self._win.attributes("-topmost", True)

    def hide(self):
        self._win.withdraw()

    def _on_focus_out(self, _event=None):
        if self._mode == "compact" and not self._cal_visible:
            self.hide()

    def _switch_to_full(self):
        self._win.unbind("<FocusOut>")
        self._win.unbind("<ButtonPress-1>")
        self._win.unbind("<B1-Motion>")
        self._rebuild("full")

    def _switch_to_compact(self):
        self._rebuild("compact")

    def _position_compact(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        self._win.geometry(f"{self.CW}x{self.CH}+{sw - self.CW - 14}+{sh - self.CH - 48}")

    def _position_full(self):
        sw = self._root.winfo_screenwidth()
        sh = self._root.winfo_screenheight()
        x  = (sw - self.FW) // 2
        y  = (sh - self.FH) // 2
        self._win.geometry(f"{self.FW}x{self.FH}+{x}+{y}")

    # ── Compact UI ────────────────────────────────────────────────────────────────
    def _build_compact(self):
        T = self.T
        w = self._win

        hdr = tk.Frame(w, bg=T["BG2"], height=46)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        self._dot = tk.Label(hdr, text="●", fg=T["GREEN"], bg=T["BG2"],
                             font=("Sans", 12))
        self._dot.place(x=14, y=13)
        self._status_lbl = tk.Label(hdr, text="Active", fg=T["TEXT"], bg=T["BG2"],
                                    font=("Sans", 11, "bold"))
        self._status_lbl.place(x=34, y=11)
        # ⤢ expand — a clean NORTH-EAST AND SOUTH-WEST ARROW (U+2922) that
        # reads unambiguously as "open bigger" at every zoom level.  The
        # previous glyph "□" was easy to mistake for an empty checkbox at
        # small font sizes, especially on default Linux font fallbacks.
        _expand_btn = tk.Button(hdr, text="⤢", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["BLUE"],
                  font=("Sans", 14, "bold"), cursor="hand2",
                  command=self._switch_to_full)
        _expand_btn.place(x=318, y=5)
        _bind_hover(_expand_btn, T["BLUE"])
        _close_btn = tk.Button(hdr, text="✕", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["RED"],
                  font=("Sans", 13, "bold"), cursor="hand2",
                  command=self.hide)
        _close_btn.place(x=350, y=6)
        _bind_hover(_close_btn, T["RED"])

        nav = tk.Frame(w, bg=T["BG2"])
        nav.pack(fill="x")
        self._cal_btn = tk.Button(
            nav, text="📅  Today", fg=T["TEXT"], bg=T["BG2"], bd=0,
            activebackground=T["BG2"], activeforeground=T["BLUE"],
            font=("Sans", 9, "bold"), cursor="hand2",
            anchor="center", command=self._toggle_calendar,
        )
        self._cal_btn.pack(side="left", expand=True, padx=8, pady=5)
        self._today_btn = tk.Button(
            nav, text="← Today", fg=T["BLUE"], bg=T["BG2"], bd=0,
            activebackground=T["BG2"], activeforeground=T["BLUE"],
            font=("Sans", 8), cursor="hand2",
            command=self._go_today,
        )
        tk.Frame(w, bg=T["BORDER"], height=1).pack(fill="x")

        ftr = tk.Frame(w, bg=T["BG2"], height=34)
        ftr.pack(fill="x", side="bottom")
        ftr.pack_propagate(False)
        self._last_upd = tk.Label(ftr, text="Refreshing…", fg=T["MUTED"], bg=T["BG2"],
                                   font=("Sans", 8))
        self._last_upd.place(x=14, y=10)
        tk.Button(ftr, text="↻", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["TEXT"],
                  font=("Sans", 12), cursor="hand2",
                  command=self.refresh).place(x=350, y=4)

        self._cal_frame  = tk.Frame(w, bg=T["BG"])
        self._build_calendar_panel()

        self._body_frame = tk.Frame(w, bg=T["BG"])
        self._body_frame.pack(fill="both", expand=True)
        body = self._body_frame

        self._conn_wrap = tk.Frame(body, bg=T["BG2"], height=0)
        self._conn_wrap.pack(fill="x")
        self._conn_wrap.pack_propagate(False)
        self._conn_lbl = tk.Label(
            self._conn_wrap, text="", fg=T["TEXT"], bg=T["BG2"],
            font=("Sans", 8), anchor="w", padx=10,
        )
        self._conn_lbl.pack(fill="x", expand=True)

        self._donut_cv = tk.Canvas(body, width=130, height=130, bg=T["BG"],
                                    highlightthickness=0)
        self._donut_cv.pack(pady=(14, 2))

        leg = tk.Frame(body, bg=T["BG"])
        leg.pack(pady=(0, 6))
        self._prod_legend   = tk.Label(leg, text="● 0m Productive",
                                       fg=T["GREEN"], bg=T["BG"],
                                       font=("Sans", 8, "bold"))
        self._prod_legend.pack(side="left", padx=6)
        self._unprod_legend = tk.Label(leg, text="● 0m Unproductive",
                                       fg=T["RED"], bg=T["BG"],
                                       font=("Sans", 8, "bold"))
        self._unprod_legend.pack(side="left", padx=6)

        kpi = tk.Frame(body, bg=T["BG"])
        kpi.pack(fill="x", padx=16)
        self._kpi_lbl: dict = {}
        for col, (key, label) in enumerate([
            ("active_secs", "Active"), ("top_app", "Top App"), ("idle_secs", "Idle"),
        ]):
            cell = tk.Frame(kpi, bg=T["BG"])
            cell.grid(row=0, column=col, sticky="ew", padx=4)
            kpi.columnconfigure(col, weight=1)
            val = tk.Label(cell, text="—", fg=T["TEXT"], bg=T["BG"],
                           font=("Sans", 11, "bold"))
            val.pack()
            tk.Label(cell, text=label, fg=T["MUTED"], bg=T["BG"],
                     font=("Sans", 9)).pack()
            self._kpi_lbl[key] = val

        tk.Frame(body, bg=T["BORDER"], height=1).pack(fill="x", padx=16, pady=8)
        tk.Label(body, text="24-Hour Activity", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9)).pack(anchor="w", padx=16)
        self._activity_cv = tk.Canvas(body, width=self.CW - 32, height=64,
                                       bg=T["BG"], highlightthickness=0)
        self._activity_cv.pack(padx=16, pady=(4, 2))
        hrow = tk.Frame(body, bg=T["BG"])
        hrow.pack(fill="x", padx=16)
        tk.Label(hrow, text="12am", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 7)).pack(side="left")
        tk.Label(hrow, text="12pm", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 7)).pack(side="left", expand=True)
        tk.Label(hrow, text="11pm", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 7)).pack(side="right")

        tk.Frame(body, bg=T["BORDER"], height=1).pack(fill="x", padx=16, pady=8)
        # 'Top Apps' header — kept in a frame so the separator above and the
        # apps list below can be hidden together when there are no apps to show.
        # Without this, a "Top Apps" label sat above an empty list, making the
        # dashboard look broken for first-run / historical-no-data days.
        self._top_apps_hdr = tk.Frame(body, bg=T["BG"])
        self._top_apps_hdr.pack(fill="x", padx=16)
        tk.Label(self._top_apps_hdr, text="Top Apps", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9)).pack(anchor="w")
        self._apps_frame = tk.Frame(body, bg=T["BG"])
        self._apps_frame.pack(fill="x", padx=16, pady=(4, 0))
        # Hidden by default — _render_apps() shows the section only when there's
        # at least one app to display.
        self._top_apps_hdr.pack_forget()
        self._apps_frame.pack_forget()

        self._win.bind("<ButtonPress-1>", self._on_drag_start)
        self._win.bind("<B1-Motion>",     self._on_drag_move)
        self._win.bind("<FocusOut>",      self._on_focus_out)

    # ── Full UI ───────────────────────────────────────────────────────────────────
    def _build_full(self):
        T = self.T
        W = self.FW
        w = self._win

        hdr = tk.Frame(w, bg=T["BG2"], height=50)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)
        self._dot = tk.Label(hdr, text="●", fg=T["GREEN"], bg=T["BG2"],
                             font=("Sans", 13))
        self._dot.place(x=16, y=14)
        self._status_lbl = tk.Label(hdr, text="Active", fg=T["TEXT"], bg=T["BG2"],
                                    font=("Sans", 12, "bold"))
        self._status_lbl.place(x=40, y=13)
        tk.Label(hdr, text="Activity Dashboard", fg=T["MUTED"], bg=T["BG2"],
                 font=("Sans", 11)).place(x=160, y=15)
        self._cal_btn = tk.Button(
            hdr, text="📅  Today", fg=T["TEXT"], bg=T["BG3"], bd=0,
            activebackground=T["BG3"], activeforeground=T["BLUE"],
            font=("Sans", 9, "bold"), cursor="hand2",
            command=self._toggle_calendar, padx=10, pady=4,
        )
        self._cal_btn.place(x=360, y=10)
        self._last_upd = tk.Label(hdr, text="Refreshing…", fg=T["MUTED"], bg=T["BG2"],
                                   font=("Sans", 8))
        self._last_upd.place(x=560, y=18)
        tk.Button(hdr, text="↻", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["TEXT"],
                  font=("Sans", 12), cursor="hand2",
                  command=self.refresh).place(x=W - 110, y=13)
        tk.Button(hdr, text="⊟", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["TEXT"],
                  font=("Sans", 14), cursor="hand2",
                  command=self._switch_to_compact).place(x=W - 76, y=11)
        tk.Button(hdr, text="✕", fg=T["MUTED"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["RED"],
                  font=("Sans", 13), cursor="hand2",
                  command=self.hide).place(x=W - 44, y=13)
        tk.Frame(w, bg=T["BORDER"], height=1).pack(fill="x")

        kpi_bar = tk.Frame(w, bg=T["BG2"], height=74)
        kpi_bar.pack(fill="x")
        kpi_bar.pack_propagate(False)
        self._full_kpi: dict = {}
        kpi_defs = [
            ("active_secs",     "Active Time",  T["BLUE"]),
            ("prod_secs",       "Productive",   T["GREEN"]),
            ("screen_off_secs", "Screen Off",   T["MUTED"]),
            ("score",           "Score",        T["INDIGO"]),
            ("top_app",         "Top App",      T["TEXT"]),
        ]
        slot_w = W // len(kpi_defs)
        for i, (key, label, color) in enumerate(kpi_defs):
            cell = tk.Frame(kpi_bar, bg=T["BG2"])
            cell.place(x=i * slot_w, y=0, width=slot_w, height=74)
            if i > 0:
                tk.Frame(cell, bg=T["BORDER"], width=1).place(x=0, y=8, height=58)
            val = tk.Label(cell, text="—", fg=color, bg=T["BG2"],
                           font=("Sans", 16, "bold"))
            val.place(relx=0.5, y=10, anchor="n")
            tk.Label(cell, text=label, fg=T["MUTED"], bg=T["BG2"],
                     font=("Sans", 9)).place(relx=0.5, y=46, anchor="n")
            self._full_kpi[key] = val
        tk.Frame(w, bg=T["BORDER"], height=1).pack(fill="x")

        content = tk.Frame(w, bg=T["BG"])
        content.pack(fill="both", expand=True)

        left = tk.Frame(content, bg=T["BG"], width=300)
        left.pack(side="left", fill="y", padx=(20, 0), pady=16)
        left.pack_propagate(False)
        tk.Label(left, text="Productivity Mix", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9, "bold")).pack(anchor="w")
        self._donut_cv = tk.Canvas(left, width=270, height=270, bg=T["BG"],
                                    highlightthickness=0)
        self._donut_cv.pack(pady=(6, 6))
        leg = tk.Frame(left, bg=T["BG"])
        leg.pack()
        self._prod_legend   = tk.Label(leg, text="● 0m Productive",
                                       fg=T["GREEN"], bg=T["BG"],
                                       font=("Sans", 9, "bold"))
        self._prod_legend.pack(side="left", padx=6)
        self._unprod_legend = tk.Label(leg, text="● 0m Unproductive",
                                       fg=T["RED"], bg=T["BG"],
                                       font=("Sans", 9, "bold"))
        self._unprod_legend.pack(side="left", padx=6)
        tk.Frame(left, bg=T["BORDER"], height=1).pack(fill="x", pady=10)
        tk.Label(left, text="Top Apps", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9, "bold")).pack(anchor="w")
        self._apps_frame = tk.Frame(left, bg=T["BG"])
        self._apps_frame.pack(fill="x", pady=(4, 0))

        tk.Frame(content, bg=T["BORDER"], width=1).pack(side="left", fill="y",
                                                         padx=(16, 16))

        right = tk.Frame(content, bg=T["BG"])
        right.pack(side="left", fill="both", expand=True, padx=(0, 20), pady=16)

        self._conn_wrap = tk.Frame(right, bg=T["BG2"], height=0)
        self._conn_wrap.pack(fill="x", pady=(0, 4))
        self._conn_wrap.pack_propagate(False)
        self._conn_lbl = tk.Label(
            self._conn_wrap, text="", fg=T["TEXT"], bg=T["BG2"],
            font=("Sans", 8), anchor="w", padx=10,
        )
        self._conn_lbl.pack(fill="x", expand=True)

        tk.Label(right, text="24-Hour Activity", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9, "bold")).pack(anchor="w")
        self._activity_cv = tk.Canvas(right, height=155, bg=T["BG"],
                                       highlightthickness=0)
        self._activity_cv.pack(fill="x", pady=(4, 2))
        hrow = tk.Frame(right, bg=T["BG"])
        hrow.pack(fill="x")
        for lbl, side, expand in [
            ("12am", "left", False), ("6am", "left", True),
            ("12pm", "left", True),  ("6pm", "left", True),
            ("11pm", "right", False),
        ]:
            tk.Label(hrow, text=lbl, fg=T["MUTED"], bg=T["BG"],
                     font=("Sans", 8)).pack(side=side, expand=expand)
        tk.Frame(right, bg=T["BORDER"], height=1).pack(fill="x", pady=10)

        week_hdr = tk.Frame(right, bg=T["BG"])
        week_hdr.pack(fill="x")
        tk.Label(week_hdr, text="7-Day Activity", fg=T["MUTED"], bg=T["BG"],
                 font=("Sans", 9, "bold")).pack(side="left")
        tk.Label(week_hdr, text="  ● Active", fg=T["BLUE"], bg=T["BG"],
                 font=("Sans", 8)).pack(side="left", padx=(12, 0))
        tk.Label(week_hdr, text="  ● Productive", fg=T["GREEN"], bg=T["BG"],
                 font=("Sans", 8)).pack(side="left")
        self._week_cv = tk.Canvas(right, height=155, bg=T["BG"],
                                   highlightthickness=0)
        self._week_cv.pack(fill="x", pady=(6, 0))

        self._cal_frame = tk.Frame(w, bg=T["BG"],
                                    highlightthickness=1,
                                    highlightbackground=T["BORDER"])
        self._build_calendar_panel()

    # ── Calendar ──────────────────────────────────────────────────────────────────
    def _build_calendar_panel(self):
        import calendar as _cm
        T = self.T
        f = self._cal_frame

        mnav = tk.Frame(f, bg=T["BG2"])
        mnav.pack(fill="x", padx=10, pady=(8, 4))
        tk.Button(mnav, text="‹", fg=T["TEXT"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["BLUE"],
                  font=("Sans", 14), cursor="hand2",
                  command=self._cal_prev_month).pack(side="left")
        tk.Button(mnav, text="›", fg=T["TEXT"], bg=T["BG2"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["BLUE"],
                  font=("Sans", 14), cursor="hand2",
                  command=self._cal_next_month).pack(side="right")
        self._cal_hdr_lbl = tk.Label(mnav, text="", fg=T["TEXT"], bg=T["BG2"],
                                     font=("Sans", 10, "bold"), width=14, anchor="center")
        self._cal_hdr_lbl.pack(side="left", expand=True)

        dow = tk.Frame(f, bg=T["BG"])
        dow.pack(fill="x", padx=10)
        for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"):
            tk.Label(dow, text=d, fg=T["MUTED"], bg=T["BG"],
                     font=("Sans", 8, "bold"), width=4,
                     anchor="center").pack(side="left", expand=True)
        tk.Frame(f, bg=T["BORDER"], height=1).pack(fill="x", padx=10, pady=2)

        grid_f = tk.Frame(f, bg=T["BG"])
        grid_f.pack(fill="x", padx=10, pady=(2, 6))
        for r in range(6):
            grid_f.rowconfigure(r, weight=1)
        for c in range(7):
            grid_f.columnconfigure(c, weight=1)

        self._cal_day_btns = []
        for r in range(6):
            for c in range(7):
                btn = tk.Button(
                    grid_f, text="", width=3, height=1, bd=0,
                    font=("Sans", 9), cursor="hand2",
                    bg=T["BG"], fg=T["TEXT"],
                    activebackground=T["BLUE"], activeforeground="#ffffff",
                    relief="flat",
                )
                btn.grid(row=r, column=c, padx=2, pady=2, sticky="nsew")
                self._cal_day_btns.append(btn)

        tk.Button(f, text="Cancel", bg=T["BG2"], fg=T["MUTED"], bd=0,
                  activebackground=T["BG2"], activeforeground=T["TEXT"],
                  font=("Sans", 8), cursor="hand2",
                  command=self._toggle_calendar).pack(pady=(0, 8))

    def _toggle_calendar(self):
        if self._cal_visible:
            if self._mode == "full":
                self._cal_frame.place_forget()
            else:
                self._cal_frame.pack_forget()
                self._body_frame.pack(fill="both", expand=True)
                self._win.bind("<FocusOut>", self._on_focus_out)
            self._cal_visible = False
        else:
            from datetime import date as _d
            try:
                sel = _d.fromisoformat(self._selected_date)
            except Exception:
                sel = datetime.now(timezone.utc).date()
            self._cal_year, self._cal_month = sel.year, sel.month
            self._cal_refresh(self._selected_date)
            if self._mode == "full":
                self._cal_frame.place(x=340, y=50, width=310, height=330)
                self._cal_frame.lift()
            else:
                self._body_frame.pack_forget()
                self._cal_frame.pack(fill="both", expand=True)
            self._cal_visible = True
            self._win.unbind("<FocusOut>")

    def _cal_refresh(self, selected_iso: str = ""):
        import calendar as _cm
        from datetime import date as _d
        T     = self.T
        today = datetime.now(timezone.utc).date()
        self._cal_hdr_lbl.configure(
            text=_d(self._cal_year, self._cal_month, 1).strftime("%B %Y")
        )
        first_wd = _cm.weekday(self._cal_year, self._cal_month, 1)
        days_in  = _cm.monthrange(self._cal_year, self._cal_month)[1]
        cells = (
            [(0, None)] * first_wd
            + [(d, _d(self._cal_year, self._cal_month, d).isoformat())
               for d in range(1, days_in + 1)]
        )
        cells += [(0, None)] * (42 - len(cells))

        for idx, btn in enumerate(self._cal_day_btns):
            day_num, iso = cells[idx]
            if day_num == 0:
                btn.configure(text="", state="disabled", bg=T["BG"],
                              disabledforeground=T["BG"], cursor="arrow")
                btn.config(command=lambda: None)
            else:
                is_today = iso == today.isoformat()
                is_sel   = iso == selected_iso
                is_fut   = iso > today.isoformat()
                if is_fut:
                    bg_c, fg_c, st = T["BG"], T["BORDER"], "disabled"
                elif is_sel:
                    bg_c, fg_c, st = T["BLUE"], "#ffffff", "normal"
                elif is_today:
                    bg_c, fg_c, st = T["BG3"], T["BLUE"], "normal"
                else:
                    bg_c, fg_c, st = T["BG"], T["TEXT"], "normal"
                btn.configure(text=str(day_num), state=st, bg=bg_c, fg=fg_c,
                              disabledforeground=T["BORDER"], cursor="hand2")
                if st == "normal":
                    btn.config(command=lambda d=iso: self._cal_pick(d))

    def _cal_prev_month(self):
        if self._cal_month == 1:
            self._cal_year, self._cal_month = self._cal_year - 1, 12
        else:
            self._cal_month -= 1
        self._cal_refresh(self._selected_date)

    def _cal_next_month(self):
        from datetime import date as _d
        ny = self._cal_year + (1 if self._cal_month == 12 else 0)
        nm = 1 if self._cal_month == 12 else self._cal_month + 1
        if _d(ny, nm, 1) <= datetime.now(timezone.utc).date():
            self._cal_year, self._cal_month = ny, nm
            self._cal_refresh(self._selected_date)

    def _cal_pick(self, iso: str):
        self._selected_date = iso
        self._toggle_calendar()
        self.refresh()

    def _go_today(self):
        self._selected_date = datetime.now(timezone.utc).date().isoformat()
        if self._cal_visible:
            self._toggle_calendar()
        self.refresh()

    # ── Refresh ───────────────────────────────────────────────────────────────────
    def refresh(self):
        if self._refresh_job:
            self._win.after_cancel(self._refresh_job)
        threading.Thread(target=self._fetch_and_update, daemon=True).start()
        self._refresh_job = self._win.after(_AUTO_REFRESH * 1000, self.refresh)

    def _fetch_and_update(self):
        try:
            data = build_display_data(self._selected_date)
            if self._mode == "compact":
                self._win.after(0, lambda: self._apply_compact(data))
            else:
                self._win.after(0, lambda: self._apply_full(data))
        except Exception:
            pass

    def _fetch_week(self):
        try:
            week = fetch_server_week(7)
            self._week_data = week
            self._win.after(0, lambda: self._render_week(week))
        except Exception:
            pass

    # ── Apply compact ─────────────────────────────────────────────────────────────
    def _apply_compact(self, d: dict):
        if not self._win.winfo_exists():
            return
        T = self.T
        is_today = d["is_today"]
        try:
            if is_today:
                self._cal_btn.configure(text="📅  Today")
                self._today_btn.pack_forget()
            else:
                from datetime import date as _dt
                self._cal_btn.configure(
                    text=f"📅  {_dt.fromisoformat(self._selected_date).strftime('%b %d, %Y')}"
                )
                self._today_btn.pack(side="right", padx=(0, 6), pady=5)
        except Exception:
            pass

        self._update_banner(d.get("banner"))

        dot_key = {"Active": "GREEN", "Idle": "YELLOW", "Away": "RED"}.get(
            d["status_label"], "MUTED"
        )
        self._dot.configure(fg=T[dot_key])
        self._status_lbl.configure(text=d["status_label"])

        self._draw_donut(self._donut_cv, d["prod_secs"], d["unprod_secs"], 130, 19)
        self._prod_legend.configure(text=f"● {_fmt_time(d['prod_secs'])} Productive")
        self._unprod_legend.configure(text=f"● {_fmt_time(d['unprod_secs'])} Unproductive")

        self._kpi_lbl["active_secs"].configure(text=_fmt_time(d["active_secs"]))
        self._kpi_lbl["idle_secs"].configure(text=_fmt_time(d["idle_secs"]))
        self._kpi_lbl["top_app"].configure(text=(d["top_app"] or "—")[:12])

        self._draw_bar_chart(self._activity_cv, d["hourly"], self.CW - 32, 64)
        self._render_apps(d["top_apps"][:5])
        self._last_upd.configure(text=d["last_updated"])

    # ── Apply full ────────────────────────────────────────────────────────────────
    def _apply_full(self, d: dict):
        if not self._win.winfo_exists():
            return
        try:
            if d["is_today"]:
                self._cal_btn.configure(text="📅  Today")
            else:
                from datetime import date as _dt
                self._cal_btn.configure(
                    text=f"📅  {_dt.fromisoformat(self._selected_date).strftime('%b %d, %Y')}"
                )
        except Exception:
            pass

        self._update_banner(d.get("banner"))

        try:
            dot_key = {"Active": "GREEN", "Idle": "YELLOW", "Away": "RED"}.get(
                d["status_label"], "MUTED"
            )
            self._dot.configure(fg=self.T[dot_key])
            self._status_lbl.configure(text=d["status_label"])
        except Exception:
            pass

        try:
            self._full_kpi["active_secs"].configure(text=_fmt_time(d["active_secs"]))
            self._full_kpi["prod_secs"].configure(text=_fmt_time(d["prod_secs"]))
            self._full_kpi["screen_off_secs"].configure(text=_fmt_time(d["screen_off_secs"]))
            self._full_kpi["score"].configure(text=f"{d['score']:.0f}%")
            self._full_kpi["top_app"].configure(text=(d["top_app"] or "—")[:14])
        except Exception:
            pass

        self._draw_donut(self._donut_cv, d["prod_secs"], d["unprod_secs"], 270, 34)
        try:
            self._prod_legend.configure(text=f"● {_fmt_time(d['prod_secs'])} Productive")
            self._unprod_legend.configure(text=f"● {_fmt_time(d['unprod_secs'])} Unproductive")
        except Exception:
            pass

        self._win.after(50, lambda: self._draw_bar_chart_full(d["hourly"]))
        self._render_apps(d["top_apps"][:8])

        try:
            self._last_upd.configure(text=d["last_updated"])
        except Exception:
            pass

    def _draw_bar_chart_full(self, hourly: list):
        try:
            cv = self._activity_cv
            cv.update_idletasks()
            W = cv.winfo_width() or (self.FW - 380)
            self._draw_bar_chart(cv, hourly, W, 155)
        except Exception:
            pass

    # ── Banner ────────────────────────────────────────────────────────────────────
    def _update_banner(self, banner: tuple | None):
        try:
            T = self.T
            if banner:
                msg, severity = banner
                fg = {"info": T["BLUE"], "warn": T["YELLOW"], "error": T["RED"]}.get(
                    severity, T["MUTED"]
                )
                self._conn_wrap.configure(height=26, bg=T["BG2"])
                self._conn_lbl.configure(text=msg, fg=fg, bg=T["BG2"])
            else:
                self._conn_wrap.configure(height=0, bg=T["BG2"])
                self._conn_lbl.configure(text="", bg=T["BG2"])
        except Exception:
            pass

    # ── Charts ────────────────────────────────────────────────────────────────────
    def _draw_donut(self, cv: tk.Canvas, prod: int, unprod: int,
                    size: int, ring: int):
        T  = self.T
        cv.delete("all")
        cx = cy = size // 2
        r  = cx - ring // 2 - 4
        total = prod + unprod
        if total <= 0:
            cv.create_oval(cx - r, cy - r, cx + r, cy + r,
                           outline=T["BORDER"], width=ring)
        else:
            pa = prod / total * 360
            cv.create_arc(cx - r, cy - r, cx + r, cy + r,
                          start=90, extent=-360,
                          outline=T["RED"], width=ring, style="arc")
            if pa > 0:
                cv.create_arc(cx - r, cy - r, cx + r, cy + r,
                              start=90, extent=-pa,
                              outline=T["GREEN"], width=ring, style="arc")
        fs     = max(10, size // 10)
        fs_sub = max(7, fs - 3)
        label  = _fmt_time(prod) if prod > 0 else "0m"
        cv.create_text(cx, cy - fs // 2 - 2, text=label,
                       fill=T["TEXT"], font=("Sans", fs, "bold"))
        cv.create_text(cx, cy + fs // 2 + 4, text="productive",
                       fill=T["MUTED"], font=("Sans", fs_sub))

    def _draw_bar_chart(self, cv: tk.Canvas, hourly: list, W: int, H: int):
        T     = self.T
        cv.delete("all")
        max_v = max(hourly) if max(hourly) > 0 else 1
        bar_w = W / 24
        now_h = datetime.now().hour

        for h, val in enumerate(hourly):
            x0 = h * bar_w + 1
            x1 = x0 + bar_w - 2
            if val > 1800:
                color = T["GREEN"]
            elif val > 300:
                color = T["BLUE"]
            else:
                color = T["BORDER"]
            if val > 0:
                bar_h = max(2, (val / max_v) * (H - 6))
                cv.create_rectangle(x0, H - bar_h, x1, H, fill=color, outline="")
            if h == now_h:
                cv.create_line(
                    x0 + (bar_w - 2) / 2, 0,
                    x0 + (bar_w - 2) / 2, H,
                    fill=T["INDIGO"], dash=(3, 3), width=1,
                )

    def _render_week(self, week_data: list):
        if not self._week_cv or not self._week_cv.winfo_exists():
            return
        cv = self._week_cv
        cv.update_idletasks()
        W = cv.winfo_width() or (self.FW - 380)
        H = 155
        T = self.T
        cv.delete("all")
        if not week_data:
            return

        max_secs  = max(d["active_secs"] for d in week_data) or 1
        n         = len(week_data)
        slot_w    = W / n
        bar_area  = H - 32
        today_str = datetime.now(timezone.utc).date().isoformat()

        for i, day in enumerate(week_data):
            xc = slot_w * i + slot_w / 2
            bw = slot_w * 0.55
            x0, x1 = xc - bw / 2, xc + bw / 2
            if day["active_secs"] > 0:
                ah = max(3, day["active_secs"] / max_secs * bar_area)
                cv.create_rectangle(x0, bar_area - ah, x1, bar_area,
                                    fill=T["BLUE"], outline="")
            if day["prod_secs"] > 0:
                ph = max(3, day["prod_secs"] / max_secs * bar_area)
                cv.create_rectangle(x0, bar_area - ph, x1, bar_area,
                                    fill=T["GREEN"], outline="")
            is_today  = day["date"] == today_str
            lbl_color = T["INDIGO"] if is_today else T["MUTED"]
            font_wt   = "bold" if is_today else "normal"
            cv.create_text(xc, H - 14, text=day["label"],
                           fill=lbl_color, font=("Sans", 9, font_wt))
            if day["active_secs"] > 0:
                ah = max(3, day["active_secs"] / max_secs * bar_area)
                cv.create_text(xc, bar_area - ah - 10,
                               text=f"{day['active_secs']/3600:.1f}h",
                               fill=T["MUTED"], font=("Sans", 8))

    def _render_apps(self, apps: list):
        """
        Render the Top Apps list.  The header and the list are hidden together
        when `apps` is empty so the dashboard does not show a lonely
        "Top Apps" label above a blank list.  For full mode the header is
        always present (the section can be empty in the no-data case but the
        column itself is always there); only the body is updated.
        """
        T     = self.T
        frame = self._apps_frame
        for child in frame.winfo_children():
            child.destroy()

        # Compact mode: hide the entire Top Apps section when there is nothing
        # to show.  The full mode keeps the header (column structure is fixed).
        if self._mode == "compact":
            if not apps:
                try:
                    self._top_apps_hdr.pack_forget()
                    self._apps_frame.pack_forget()
                except Exception:
                    pass
                return
            try:
                self._top_apps_hdr.pack(fill="x", padx=16)
                self._apps_frame.pack(fill="x", padx=16, pady=(4, 0))
            except Exception:
                pass

        for entry in apps:
            row = tk.Frame(frame, bg=T["BG"])
            row.pack(fill="x", pady=1)
            color = T["GREEN"] if entry.get("category") == "Productive" else T["RED"]
            tk.Label(row, text="●", fg=color, bg=T["BG"],
                     font=("Sans", 8)).pack(side="left")
            name = entry.get("app", "")
            tk.Label(row, text=name, fg=T["TEXT"], bg=T["BG"],
                     font=("Sans", 9), anchor="w").pack(side="left", padx=4)
            tk.Label(row, text=_fmt_time(entry.get("time", 0)), fg=T["MUTED"], bg=T["BG"],
                     font=("Sans", 9)).pack(side="right")

        # Safety net — if compact and now have apps, ensure both frames are packed.
        if self._mode == "compact" and apps:
            try:
                self._top_apps_hdr.pack_info()
            except Exception:
                self._top_apps_hdr.pack(fill="x", padx=16)
            try:
                self._apps_frame.pack_info()
            except Exception:
                self._apps_frame.pack(fill="x", padx=16, pady=(4, 0))

    # ── Drag ──────────────────────────────────────────────────────────────────────
    def _on_drag_start(self, e):
        self._drag_start = (e.x_root - self._win.winfo_x(),
                            e.y_root - self._win.winfo_y())

    def _on_drag_move(self, e):
        if self._drag_start:
            dx, dy = self._drag_start
            self._win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")


# ═════════════════════════════════════════════════════════════════════════════════
# System tray
# ═════════════════════════════════════════════════════════════════════════════════

def _make_tray_icon(color: str = "#16a34a") -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    rgb  = tuple(int(color.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    draw.ellipse([4,  4,  size - 4,  size - 4],  fill=rgb)
    draw.ellipse([18, 18, size - 18, size - 18], fill=(0, 0, 0, 0))
    return img


def _tooltip_text() -> str:
    status, cache = read_local()
    if status:
        app    = status.get("app", "Unknown")
        locked = status.get("locked", False)
        active = status.get("active", False)
        state  = "Away" if locked else ("Active" if active else "Idle")
        score  = (cache or {}).get("summary", {}).get("productivity_score", 0)
        return f"TelemetryAgent — {state}\n{app}  |  Score: {score:.0f}%"
    return "TelemetryAgent — waiting for agent…"


def _pil_to_pixbuf(img: Image.Image):
    """Convert a PIL Image to a GdkPixbuf (needed by GTK StatusIcon)."""
    import io
    from gi.repository import GdkPixbuf
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    loader = GdkPixbuf.PixbufLoader.new_with_type("png")
    loader.write(buf.read())
    loader.close()
    return loader.get_pixbuf()


def _run_gtk_status_tray(root: tk.Tk) -> None:
    """
    Tray icon for MATE desktop using Gtk.StatusIcon.

    MATE's Notification Area applet uses the XEmbed protocol.  pystray's xorg
    backend also uses XEmbed but MATE does NOT forward mouse ButtonPress events
    to embedded client windows — so pystray clicks are silently swallowed.

    Gtk.StatusIcon avoids this entirely: it is registered as a native GTK status
    icon and receives activate/popup-menu signals from GTK itself, not via raw
    X11 event forwarding.

    Runs Gtk.main() in the calling thread.  Always call from a daemon thread.
    Cross-thread calls back to tkinter use root.after() which is thread-safe.
    """
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    _ref: list = [None]   # holds the DashboardWindow instance

    def _toggle():
        def _do():
            if _ref[0] and _ref[0]._win.winfo_exists():
                if _ref[0]._win.state() == "withdrawn":
                    _ref[0].show()
                else:
                    _ref[0].hide()
            else:
                _ref[0] = DashboardWindow(root)
        root.after(0, _do)

    def _on_activate(_icon):
        _toggle()

    def _on_popup_menu(_icon, button, activate_time):
        menu = Gtk.Menu()

        item_show = Gtk.MenuItem(label="Show Stats")
        item_show.connect("activate", lambda *_: _toggle())
        menu.append(item_show)

        item_dash = Gtk.MenuItem(label="Open Dashboard")
        def _open(*_):
            if _SERVER_BASE:
                import webbrowser
                webbrowser.open(_SERVER_BASE)
        item_dash.connect("activate", _open)
        menu.append(item_dash)

        menu.append(Gtk.SeparatorMenuItem())

        item_refresh = Gtk.MenuItem(label="Refresh")
        def _refresh(*_):
            if _ref[0] and _ref[0]._win.winfo_exists():
                root.after(0, _ref[0].refresh)
        item_refresh.connect("activate", _refresh)
        menu.append(item_refresh)

        if _update_available:
            item_update = Gtk.MenuItem(label=f"Update to v{_server_new_version} Available")
            def _do_gtk_update(*_):
                Gtk.main_quit()
                root.after(0, _perform_update)
            item_update.connect("activate", _do_gtk_update)
            menu.append(item_update)
            menu.append(Gtk.SeparatorMenuItem())

        item_exit = Gtk.MenuItem(label="Exit Agent UI")
        def _exit(*_):
            Gtk.main_quit()
            root.after(0, root.quit)
        item_exit.connect("activate", _exit)
        menu.append(item_exit)

        menu.show_all()
        menu.popup(None, None,
                   Gtk.StatusIcon.position_menu, _icon,
                   button, activate_time)

    status_icon = Gtk.StatusIcon()
    status_icon.set_from_pixbuf(_pil_to_pixbuf(_make_tray_icon()))
    status_icon.set_tooltip_text("TelemetryAgent")
    status_icon.set_visible(True)
    status_icon.connect("activate",    _on_activate)
    status_icon.connect("popup-menu",  _on_popup_menu)

    _gtk_update_check_counter = [0]

    def _update_icon():
        try:
            s, _ = read_local()
            if s:
                locked = s.get("locked", False)
                active = s.get("active", False)
                col = "#dc2626" if locked else ("#16a34a" if active else "#d97706")
                status_icon.set_from_pixbuf(_pil_to_pixbuf(_make_tray_icon(col)))
                tip = _tooltip_text()
                if _update_available:
                    tip += f" | Update v{_server_new_version} available"
                status_icon.set_tooltip_text(tip)
        except Exception:
            pass
        # Check for server updates every ~1 hour
        _gtk_update_check_counter[0] += 1
        if _gtk_update_check_counter[0] >= 360:
            _gtk_update_check_counter[0] = 0
            threading.Thread(target=check_for_update, daemon=True).start()
        return True  # keep repeating

    GLib.timeout_add_seconds(10, _update_icon)
    Gtk.main()


def build_tray_icon(root: tk.Tk) -> pystray.Icon:
    """
    Build and return the pystray Icon. Does NOT call icon.run() — the caller
    decides how to run it (run_detached() from main thread is required on Linux
    so that pystray manages its own thread and the GTK/AppIndicator event loop
    does not conflict with tkinter's main loop).
    """
    _ref: list = [None]

    def _show():
        def _do():
            if _ref[0] and _ref[0]._win.winfo_exists():
                if _ref[0]._win.state() == "withdrawn":
                    _ref[0].show()
                else:
                    _ref[0].hide()
            else:
                _ref[0] = DashboardWindow(root)
        root.after(0, _do)

    def _open_dashboard():
        if _SERVER_BASE:
            import webbrowser
            webbrowser.open(_SERVER_BASE)

    def _do_refresh():
        if _ref[0] and _ref[0]._win.winfo_exists():
            root.after(0, _ref[0].refresh)

    def _do_exit(icon, _item):
        icon.stop()
        root.after(0, root.quit)

    def _do_update_action(*_):
        threading.Thread(target=_perform_update, daemon=True).start()

    menu = pystray.Menu(
        pystray.MenuItem("Show Stats",     lambda *_: _show(),           default=True),
        pystray.MenuItem("Open Dashboard", lambda *_: _open_dashboard()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh",        lambda *_: _do_refresh()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(
            lambda _: f"Update to v{_server_new_version} Available",
            _do_update_action,
            visible=lambda _: _update_available,
        ),
        pystray.MenuItem("Exit Agent UI",  _do_exit),
    )
    icon = pystray.Icon("TelemetryAgent", _make_tray_icon(), "TelemetryAgent", menu)

    _prev_update_available = [False]
    _update_check_counter = [0]

    def _updater():
        while True:
            time.sleep(10)
            try:
                status, _ = read_local()
                if status:
                    locked = status.get("locked", False)
                    active = status.get("active", False)
                    col    = "#dc2626" if locked else ("#16a34a" if active else "#d97706")
                    icon.icon = _make_tray_icon(col)
                parts = [_tooltip_text()]
                if _update_available:
                    parts.append(f"Update v{_server_new_version} available")
                icon.title = " | ".join(parts)
            except Exception:
                pass

            # Rebuild menu when update becomes available
            if _update_available and not _prev_update_available[0]:
                _prev_update_available[0] = True
                try:
                    icon.update_menu()
                except Exception:
                    pass

            # Check for server updates every ~1 hour (360 × 10 s intervals)
            _update_check_counter[0] += 1
            if _update_check_counter[0] >= 360:
                _update_check_counter[0] = 0
                threading.Thread(target=check_for_update, daemon=True).start()

    threading.Thread(target=_updater, daemon=True).start()
    return icon


# ── Entry point ───────────────────────────────────────────────────────────────────
def main():
    # Background version check — never block the tray from appearing
    threading.Thread(target=check_for_update, daemon=True).start()
    root = tk.Tk()
    root.withdraw()
    root.title("TelemetryAgent UI")

    if "MATE" in _DESKTOP:
        # MATE: use Gtk.StatusIcon — receives click signals natively.
        # MATE's Notification Area does not forward raw X11 ButtonPress events to
        # embedded XEmbed windows, so pystray's xorg backend silently drops clicks.
        try:
            threading.Thread(
                target=_run_gtk_status_tray,
                args=(root,),
                daemon=True,
            ).start()
        except Exception as e:
            print(f"[warn] GTK StatusIcon failed ({e}), falling back to pystray",
                  file=sys.stderr)
            build_tray_icon(root).run_detached()
    else:
        # GNOME, KDE, Xfce, etc.: pystray AppIndicator.
        # Wrap in try/except — pystray may fail if AppIndicator/GI is missing
        # (common on Ubuntu without python3-gi + gnome-shell-extension-appindicator).
        try:
            build_tray_icon(root).run_detached()
        except Exception as e:
            print(f"[warn] pystray failed ({e}) — trying GTK StatusIcon", file=sys.stderr)
            _is_gnome = "GNOME" in _DESKTOP or "UBUNTU" in _DESKTOP
            if _is_gnome:
                print(
                    "[hint] On Ubuntu/GNOME the tray icon is invisible unless the\n"
                    "  AppIndicator extension is enabled. Fix with:\n"
                    "    gnome-extensions enable ubuntu-appindicators@ubuntu.com\n"
                    "  or install if missing:\n"
                    "    sudo apt install -y gnome-shell-extension-appindicator",
                    file=sys.stderr,
                )
            try:
                threading.Thread(
                    target=_run_gtk_status_tray,
                    args=(root,),
                    daemon=True,
                ).start()
            except Exception as e2:
                print(
                    f"[error] GTK StatusIcon also failed ({e2}).\n"
                    "  Install missing packages:\n"
                    "    sudo apt install -y python3-gi gir1.2-appindicator3-0.1 gnome-shell-extension-appindicator\n"
                    "  Then restart: ~/.local/bin/telemetry-ui",
                    file=sys.stderr,
                )

    root.mainloop()


if __name__ == "__main__":
    main()
