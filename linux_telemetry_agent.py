"""
linux_telemetry_agent.py — Linux background agent.

Mirrors telemetry_agent.py (Windows) exactly in behavior and payload schema.

Activity detection
------------------
- Idle       : xprintidle (ms since last X11 input); Wayland D-Bus fallback
- Active win : xdotool getactivewindow + getwindowname + getwindowclassname
- Lock       : D-Bus org.gnome.ScreenSaver / org.freedesktop.ScreenSaver + loginctl
- Suspend    : wall-clock jump detection (same technique as Windows)

Startup layers (mirrors Windows 3-layer strategy)
1. systemd user service  (WantedBy=graphical-session.target)
2. systemd watchdog timer (every 5 min)
3. XDG autostart .desktop  (fallback)

Offline backup: SQLite backup.db  (replaces Windows batch_*.json files)

IPC with UI companion:
  ~/.local/share/telemetry-agent/status.json  — updated every tick  (~5 s)
  ~/.local/share/telemetry-agent/cache.json   — updated every event (~30 s)
"""

import argparse
import getpass
import json
import logging
import os
import platform
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from enum import Enum

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False  # non-Linux (testing / CI)

import requests

# ── XDG paths ────────────────────────────────────────────────────────────────────
_XDG_DATA   = os.environ.get("XDG_DATA_HOME",   os.path.expanduser("~/.local/share"))
_XDG_CONFIG = os.environ.get("XDG_CONFIG_HOME",  os.path.expanduser("~/.config"))

DATA_DIR           = os.path.join(_XDG_DATA,   "telemetry-agent")
CONFIG_DIR         = os.path.join(_XDG_CONFIG,  "telemetry-agent")  # kept for cleanup only
SYSTEM_CONFIG_PATH = "/etc/telemetry-agent/config.json"
# config.json lives in DATA_DIR (mirrors Windows C:\ProgramData\TelemetryAgent\config.json)
# This avoids ~/.config permission issues on minimal/enterprise Linux setups.
USER_CONFIG_PATH   = os.path.join(DATA_DIR,    "config.json")
STATUS_PATH        = os.path.join(DATA_DIR,    "status.json")
CACHE_PATH         = os.path.join(DATA_DIR,    "cache.json")
BACKUP_DB_PATH     = os.path.join(DATA_DIR,    "backup.db")
LOG_PATH           = os.path.join(DATA_DIR,    "agent.log")
LAST_SEEN_PATH     = os.path.join(DATA_DIR,    "last_seen.json")
PID_FILE           = os.path.join(DATA_DIR,    "agent.pid")

AUTOSTART_DIR    = os.path.join(_XDG_CONFIG, "autostart")
SYSTEMD_USER_DIR = os.path.join(_XDG_CONFIG, "systemd", "user")

# Companion UI install path — matches the `~/.local/bin/telemetry-ui` wrapper
# created by `linux/install.sh` (and referenced in _AGENT_DESKTOP).  Centralised
# here so the in-process UI watchdog and the install routine cannot drift.
UI_EXE_PATH = os.path.expanduser("~/.local/bin/telemetry-ui")
# How often the in-process UI watchdog polls (real wall-clock seconds).
# Matches the telemetry-agent-watchdog.timer cadence (5 min) so we never
# have two recovery mechanisms racing each other.
UI_WATCHDOG_INTERVAL_SEC = 5 * 60
# Process names we treat as "the UI is running".  `pkill -f` would also match
# a developer running `python telemetry_ui.py` for debugging, which we
# want to satisfy the watchdog (telling them "no UI is running" when they
# are looking at one would be a false negative).
UI_RUNNING_PATTERN = "telemetry-ui"

# ── Config loading ────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    candidates = [
        SYSTEM_CONFIG_PATH,
        USER_CONFIG_PATH,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.config.json"),
    ]
    for path in candidates:
        try:
            with open(path) as f:
                cfg = json.load(f)
                print(f"[config] Loaded {path}")
                return cfg
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            print(f"[config] {path} parse error: {e} — skipping")
    return {}

_cfg = _load_config()

# ── Configuration ─────────────────────────────────────────────────────────────────
AGENT_VERSION  = "3.2"

IDLE_THRESHOLD = _cfg.get("idle_threshold",  300)
TICK_INTERVAL  = _cfg.get("tick_interval",     5)
LOG_INTERVAL   = _cfg.get("log_interval",     30)
BATCH_SIZE     = _cfg.get("batch_size",       10)
FLUSH_INTERVAL = _cfg.get("flush_interval",   60)
MAX_BACKUP_EVENTS = 500
MAX_LOG_SIZE   = 10 * 1024 * 1024  # 10 MB

INGEST_URL    = os.getenv("INGEST_URL") or _cfg.get("ingest_url", "http://localhost:8000/ingest")
AGENT_API_KEY = (os.getenv("AGENT_API_KEY") or os.getenv("API_KEY")
                 or _cfg.get("api_key", ""))

BROWSER_PROCESSES = {
    "firefox", "firefox-esr", "firefox-bin",
    "chrome", "google-chrome", "google-chrome-stable",
    "chromium", "chromium-browser",
    "brave", "brave-browser",
    "opera", "vivaldi",
}

# ── Display server detection ──────────────────────────────────────────────────────
_IS_WAYLAND = bool(os.environ.get("WAYLAND_DISPLAY"))

# ── Local categorisation (mirrors aggregator.py) ──────────────────────────────────
_UNPRODUCTIVE_DOMAINS = {
    "youtube.com", "youtu.be", "netflix.com", "primevideo.com", "hulu.com",
    "disneyplus.com", "twitch.tv", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "tiktok.com", "reddit.com", "9gag.com",
    "amazon.com", "ebay.com", "buzzfeed.com", "espn.com",
    "web.whatsapp.com", "web.telegram.org",
}
_UNPRODUCTIVE_APPS = {
    "steam", "steam.exe", "epicgameslauncher", "spotify", "vlc", "wine",
    "leagueclient", "valorant",
}


def _local_categorize(app: str, domain: str) -> str:
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    if d and any(k in d for k in _UNPRODUCTIVE_DOMAINS):
        return "Unproductive"
    a = (app or "").lower().strip()
    if any(k in a for k in _UNPRODUCTIVE_APPS):
        return "Unproductive"
    return "Productive"


# ── In-memory daily accumulators ─────────────────────────────────────────────────
_acc_date:       str  = ""
_acc_active:     int  = 0
_acc_idle:       int  = 0
_acc_locked:     int  = 0
_acc_productive: int  = 0
_acc_app_times:  dict = {}
_acc_hourly:     list = [0] * 24


def _check_day_reset() -> None:
    global _acc_date, _acc_active, _acc_idle, _acc_locked
    global _acc_productive, _acc_app_times, _acc_hourly
    today = datetime.now(timezone.utc).date().isoformat()
    if _acc_date == today:
        return
    _acc_date = today
    _acc_active = _acc_idle = _acc_locked = _acc_productive = 0
    _acc_app_times = {}
    _acc_hourly = [0] * 24


def _accumulate(app: str, domain: str,
                is_active: bool, is_locked: bool, duration: int) -> None:
    global _acc_active, _acc_idle, _acc_locked, _acc_productive
    _check_day_reset()
    if is_locked:
        _acc_locked += duration
    elif is_active:
        _acc_active += duration
        _acc_app_times[app] = _acc_app_times.get(app, 0) + duration
        hour = datetime.now(timezone.utc).astimezone().hour
        _acc_hourly[hour] = min(_acc_hourly[hour] + duration, 3600)
        if _local_categorize(app, domain) == "Productive":
            _acc_productive += duration
    else:
        _acc_idle += duration


# ── Activity state machine ────────────────────────────────────────────────────
class ActivityState(Enum):
    ACTIVE = "active"
    IDLE   = "idle"
    LOCKED = "locked"


class StateEngine:
    """
    State-machine that tracks ACTIVE / IDLE / LOCKED using
    time.monotonic() deltas exclusively.  Never uses event durations
    or constant TICK_INTERVAL additions.

    Invariant: active_seconds + idle_seconds + locked_seconds <= session_elapsed_seconds always.
    """

    def __init__(self, idle_threshold: int, max_tick_seconds: float = 30.0):
        self._threshold               = idle_threshold
        self._max_tick                = max_tick_seconds
        self._state                   = ActivityState.IDLE
        self._active_seconds          = 0.0
        self._idle_seconds            = 0.0
        self._locked_seconds          = 0.0
        self._session_elapsed_seconds = 0.0

    def tick(self, idle_secs: int, is_locked: bool, tick_elapsed: float) -> "ActivityState":
        tick_elapsed = max(0.0, min(tick_elapsed, self._max_tick))
        self._session_elapsed_seconds += tick_elapsed

        if is_locked:
            self._state = ActivityState.LOCKED
            self._locked_seconds += tick_elapsed
        elif idle_secs >= self._threshold:
            self._state = ActivityState.IDLE
            self._idle_seconds += tick_elapsed
        else:
            self._state = ActivityState.ACTIVE
            self._active_seconds += tick_elapsed

        return self._state

    def on_sleep_gap(self, gap_seconds: float) -> None:
        gap_seconds = max(0.0, gap_seconds)
        self._locked_seconds += gap_seconds
        self._session_elapsed_seconds += gap_seconds
        self._state = ActivityState.LOCKED

    def enforce_invariant(self) -> None:
        session_el = self._session_elapsed_seconds
        total = self._active_seconds + self._idle_seconds + self._locked_seconds
        # 1-second tolerance absorbs sub-second accumulation jitter
        if total > session_el + 1.0:
            _LOG.warning(
                "StateEngine invariant violation: total=%.0fs session=%.0fs — scaling down",
                total, session_el,
            )
            if total > 0:
                scale = session_el / total
                self._active_seconds  *= scale
                self._idle_seconds    *= scale
                self._locked_seconds  *= scale

    @property
    def state(self) -> "ActivityState":
        return self._state

    @property
    def is_active(self) -> bool:
        return self._state == ActivityState.ACTIVE

    @property
    def active_seconds(self) -> float:
        return self._active_seconds

    @property
    def idle_seconds(self) -> float:
        return self._idle_seconds

    @property
    def locked_seconds(self) -> float:
        return self._locked_seconds

    @property
    def session_elapsed_seconds(self) -> float:
        return self._session_elapsed_seconds

    def to_cache_dict(self) -> dict:
        snapshot_elapsed = self._session_elapsed_seconds
        self.enforce_invariant()
        return {
            "active_seconds":  int(self._active_seconds),
            "idle_seconds":    int(self._idle_seconds),
            "locked_seconds":  int(self._locked_seconds),
            "session_elapsed": int(snapshot_elapsed),
        }


# ── Status + cache writers ────────────────────────────────────────────────────────
def _write_status_file(app: str, active: bool, locked: bool, idle_secs: int) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "app":          app,
            "active":       active,
            "locked":       locked,
            "idle_seconds": idle_secs,
        }
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass


def _write_cache(username: str, device: str) -> None:
    _check_day_reset()
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        total_scored = _acc_active if _acc_active else 1
        score = round(_acc_productive / total_scored * 100, 1)
        top_apps = sorted(
            [{"app": a, "time": t, "category": _local_categorize(a, "")}
             for a, t in _acc_app_times.items()],
            key=lambda x: x["time"], reverse=True,
        )[:10]
        payload = {
            "date":         _acc_date,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "username":     username,
            "device":       device,
            "summary": {
                "total_active_time":     _acc_active,
                "total_idle_time":       _acc_idle,
                "total_screen_off_time": _acc_locked,
                "productivity_score":    score,
                "top_app":               top_apps[0]["app"] if top_apps else "None",
            },
            "top_apps":      top_apps,
            "hourly_active": _acc_hourly,
        }
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(payload, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass


# ── Logging ────────────────────────────────────────────────────────────────────────
_LOG = logging.getLogger("telemetry_agent")


def _setup_logging() -> None:
    if _LOG.handlers:
        return
    _LOG.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s", "%Y-%m-%d %H:%M:%S"
    )
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH)
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)
    except Exception as e:
        print(f"[log] Cannot create log file: {e}")
    try:
        if sys.stdout:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            _LOG.addHandler(ch)
    except Exception:
        pass


# ── State tracker ─────────────────────────────────────────────────────────────────
class TelemetryState:
    def __init__(self):
        self.current_app  = "Unknown"
        self._last_switch = time.time()

    def update(self, app_name: str) -> None:
        if app_name != self.current_app:
            self.current_app  = app_name
            self._last_switch = time.time()

    def session_duration(self) -> int:
        return int(time.time() - self._last_switch)


# ── Subprocess helper ─────────────────────────────────────────────────────────────
def _run(cmd: list, timeout: float = 1.5) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


def _detect_x11_env() -> None:
    """
    Systemd user services do not inherit the graphical session environment, so
    DISPLAY, XAUTHORITY, and DBUS_SESSION_BUS_ADDRESS are typically unset.
    Without DISPLAY, xdotool and xprop silently fail → every app reports "Unknown".

    This scans /proc/<pid>/environ for processes owned by the current user that
    have DISPLAY set (i.e. any GUI app already running), and copies those vars
    into os.environ so all subsequent X11 calls work.
    """
    needed = {"DISPLAY", "XAUTHORITY", "DBUS_SESSION_BUS_ADDRESS"}
    if all(os.environ.get(k) for k in needed):
        return  # already fully set

    uid = os.getuid()
    found: dict = {}
    try:
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            env_path = f"/proc/{entry}/environ"
            try:
                if os.stat(env_path).st_uid != uid:
                    continue
                with open(env_path, "rb") as f:
                    for var in f.read().split(b"\x00"):
                        try:
                            s = var.decode()
                        except UnicodeDecodeError:
                            continue
                        k, _, v = s.partition("=")
                        if k in needed and k not in found and v:
                            found[k] = v
                if len(found) == len(needed):
                    break
            except (PermissionError, FileNotFoundError, ProcessLookupError):
                continue
    except Exception:
        pass

    for k, v in found.items():
        os.environ.setdefault(k, v)
        _LOG.info("[x11-env] Detected %s=%s", k, v)

    if "DISPLAY" not in os.environ:
        _LOG.warning("[x11-env] Could not detect DISPLAY — window tracking will be unavailable")


# ── Idle detection ────────────────────────────────────────────────────────────────
def _get_idle_x11_xlib() -> int:
    """
    Query X11 Screen Saver idle time via python-xlib (ships with pystray).
    Equivalent to xprintidle without the external binary.
    Returns idle seconds, or -1 if unavailable.
    """
    try:
        from Xlib import display as _xd
        from Xlib.ext import screensaver as _xs
        d = _xd.Display()
        info = _xs.query_info(d.screen().root)
        d.close()
        return info.idle // 1000
    except Exception:
        return -1


def _get_idle_dbus() -> int:
    """
    Query idle milliseconds via D-Bus.  Tries GNOME Mutter, then KDE KIdleTime.
    Returns idle seconds, or -1 when no supported DE is running (e.g. MATE, XFCE).
    Returning -1 (not 0) distinguishes "unknown" from "just moved mouse".
    """
    out = _run([
        "dbus-send", "--session", "--print-reply",
        "--dest=org.gnome.Mutter.IdleMonitor",
        "/org/gnome/Mutter/IdleMonitor/Core",
        "org.gnome.Mutter.IdleMonitor.GetIdletime",
    ], timeout=2.0)
    m = re.search(r"uint64\s+(\d+)", out)
    if m:
        return int(m.group(1)) // 1000

    out = _run([
        "dbus-send", "--session", "--print-reply",
        "--dest=org.kde.KIdleTime",
        "/modules/kidletime",
        "org.kde.KIdleTime.GetIdleTime",
    ], timeout=2.0)
    m = re.search(r"int32\s+(\d+)", out)
    if m:
        return int(m.group(1)) // 1000

    return -1  # no D-Bus idle provider found


def get_idle_seconds() -> int:
    """
    Seconds since last user input.  Detection chain (most accurate → safest fallback):
      1. xprintidle     — external binary, most distros, all X11 DEs
      2. python-xlib    — Screen Saver extension query; no extra binary needed
      3. D-Bus          — GNOME Mutter / KDE KIdleTime
      4. IDLE_THRESHOLD — if ALL methods fail, default to idle (never false-active)

    Re-reads WAYLAND_DISPLAY each call because _detect_x11_env() may set it
    after module load (the module-level _IS_WAYLAND flag is evaluated too early).
    """
    is_wayland = bool(os.environ.get("WAYLAND_DISPLAY"))

    if not is_wayland:
        # Method 1: xprintidle binary
        out = _run(["xprintidle"])
        if out.isdigit():
            return int(out) // 1000

        # Method 2: python-xlib Screen Saver extension (works on MATE/XFCE/Openbox/etc.)
        idle = _get_idle_x11_xlib()
        if idle >= 0:
            return idle

    # Method 3: D-Bus (Wayland, or X11 without xprintidle/xlib)
    idle = _get_idle_dbus()
    if idle >= 0:
        return idle

    # Method 4: All detection failed.  Return IDLE_THRESHOLD so the user is
    # treated as IDLE rather than falsely active — wrong idle is less harmful
    # than wrong active (inflated productivity scores / impossible hour counts).
    _LOG.debug("Idle detection unavailable — defaulting to idle state")
    return IDLE_THRESHOLD


# ── Lock detection ────────────────────────────────────────────────────────────────
def is_session_locked() -> bool:
    """Returns True when the screen is locked.  Tries D-Bus then loginctl."""
    for dest, path in [
        ("org.gnome.ScreenSaver",       "/org/gnome/ScreenSaver"),
        ("org.freedesktop.ScreenSaver", "/org/freedesktop/ScreenSaver"),
        ("org.kde.screensaver",         "/org/kde/screensaver"),
    ]:
        out = _run([
            "dbus-send", "--session", "--print-reply",
            f"--dest={dest}", path, f"{dest}.GetActive",
        ])
        if "true" in out.lower():
            return True
        if "false" in out.lower():
            return False  # explicit false → unlocked, stop trying

    session = os.environ.get("XDG_SESSION_ID", "")
    if session:
        out = _run(["loginctl", "show-session", session, "--value", "-p", "LockedHint"])
        if out.lower() == "yes":
            return True

    return False


# ── Active window ─────────────────────────────────────────────────────────────────
def get_active_window() -> tuple:
    """
    Returns (app_name, window_title).

    Detection chain (works across Ubuntu, MATE, Xfce, Openbox, KDE, etc.):
      1. xdotool  — preferred; needs xdotool installed
      2. xprop    — standard X11 tool present on all distros with x11-utils/xorg-x11-utils
      3. /proc/<pid>/comm — pure-Python last resort using _NET_WM_PID
    """
    # ── Method 1: xdotool ─────────────────────────────────────────────────────
    win_id = _run(["xdotool", "getactivewindow"])
    if win_id:
        title    = _run(["xdotool", "getwindowname",    win_id])
        wm_class = _run(["xdotool", "getwindowclassname", win_id])
        app = wm_class if wm_class else (title.split()[-1] if title else "Unknown")
        return app, title

    # ── Method 2: xprop _NET_ACTIVE_WINDOW (EWMH — all modern WMs) ───────────
    root_out = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"], timeout=2.0)
    m = re.search(r"0x([0-9a-fA-F]+)", root_out)
    if m:
        win_hex  = "0x" + m.group(1)
        xp = _run(["xprop", "-id", win_hex,
                   "_NET_WM_NAME", "WM_NAME", "WM_CLASS", "_NET_WM_PID"], timeout=2.0)

        # Title: prefer _NET_WM_NAME (UTF-8), fall back to WM_NAME
        title = ""
        t = re.search(r'_NET_WM_NAME\([^)]+\) = "(.*?)"', xp)
        if not t:
            t = re.search(r'WM_NAME\([^)]+\) = "(.*?)"', xp)
        if t:
            title = t.group(1)

        # App: first string in WM_CLASS = instance name (e.g. "firefox", "caja", "mate-terminal")
        app = ""
        c = re.search(r'WM_CLASS\([^)]+\) = "([^"]+)"', xp)
        if c:
            app = c.group(1)

        # Last resort: read process name from /proc via _NET_WM_PID
        if not app:
            p = re.search(r'_NET_WM_PID\([^)]+\) = (\d+)', xp)
            if p:
                try:
                    with open(f"/proc/{p.group(1)}/comm") as f:
                        app = f.read().strip()
                except Exception:
                    pass

        if app or title:
            return (app or title.split()[-1] or "Unknown"), title

    return "Unknown", ""


def extract_domain(app: str, title: str) -> str:
    """Best-effort browser title → domain/page-title extraction."""
    if app.lower() not in BROWSER_PROCESSES:
        return ""
    return re.sub(
        r"\s*[-–—]\s*(Mozilla Firefox|Google Chrome|Chromium|Brave|"
        r"Opera|Vivaldi|Microsoft Edge).*$",
        "", title, flags=re.IGNORECASE,
    ).strip()


# ── Singleton guard (PID file + fcntl) ────────────────────────────────────────────
_pid_fd = None  # kept open for process lifetime


def _acquire_singleton() -> bool:
    global _pid_fd
    if not _HAS_FCNTL:
        return True  # non-Linux: always allow (used in tests / CI)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _pid_fd = open(PID_FILE, "w")
        fcntl.lockf(_pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _pid_fd.write(str(os.getpid()))
        _pid_fd.flush()
        return True
    except OSError:
        return False


# ── SQLite offline backup ─────────────────────────────────────────────────────────
def _db_conn() -> sqlite3.Connection:
    os.makedirs(DATA_DIR, exist_ok=True)
    conn = sqlite3.connect(BACKUP_DB_PATH, timeout=5)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS offline_batches (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at  TEXT    NOT NULL,
            user        TEXT    NOT NULL,
            device      TEXT    NOT NULL,
            events_json TEXT    NOT NULL,
            event_count INTEGER NOT NULL
        )
    """)
    conn.commit()
    return conn


def save_to_backup(username: str, device: str, events: list) -> None:
    if not events:
        return
    try:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT id, event_count FROM offline_batches ORDER BY id ASC"
        ).fetchall()
        total = sum(r[1] for r in rows)
        for row_id, count in rows:
            if total + len(events) <= MAX_BACKUP_EVENTS:
                break
            conn.execute("DELETE FROM offline_batches WHERE id=?", (row_id,))
            total -= count
            print(f"  [backup] Evicted batch id={row_id}")
        conn.execute(
            "INSERT INTO offline_batches "
            "(created_at, user, device, events_json, event_count) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(),
             username, device, json.dumps(events), len(events)),
        )
        conn.commit()
        conn.close()
        print(f"  [backup] {len(events)} events saved offline -> {BACKUP_DB_PATH}")
    except Exception as e:
        print(f"  [backup] DB write failed: {e}")


def flush_backup(username: str, device: str) -> int:
    recovered = 0
    try:
        conn = _db_conn()
        rows = conn.execute(
            "SELECT id, user, device, events_json FROM offline_batches ORDER BY id ASC"
        ).fetchall()
        if not rows:
            conn.close()
            return 0
        for row_id, u, d, evts_json in rows:
            events = json.loads(evts_json)
            if flush_batch(u or username, d or device, events):
                conn.execute("DELETE FROM offline_batches WHERE id=?", (row_id,))
                conn.commit()
                recovered += len(events)
                print(f"  [backup] Recovered {len(events)} events (id={row_id})")
            else:
                break
        conn.close()
    except Exception as e:
        print(f"  [backup] DB replay failed: {e}")
    if recovered:
        print(f"  [backup] Total recovered: {recovered} events")
    return recovered


# ── Last-seen state (startup gap detection) ───────────────────────────────────────
def _save_last_seen(timestamp: str, app: str) -> None:
    try:
        with open(LAST_SEEN_PATH, "w") as f:
            json.dump({"timestamp": timestamp, "app": app}, f)
    except Exception:
        pass


def _startup_gap_events() -> list:
    try:
        with open(LAST_SEEN_PATH) as f:
            data = json.load(f)
        last_ts = datetime.fromisoformat(data["timestamp"])
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        gap = int((datetime.now(timezone.utc) - last_ts).total_seconds())
        if gap < LOG_INTERVAL * 2:
            return []
        # Cap at 24 h so one event never inflates a single day beyond 86400 s
        gap = min(gap, 86_400)
        _LOG.info("Startup gap: %ds since last event — inserting screen-off", gap)
        return [{
            "app": "Screen Off", "domain": "", "active": False, "locked": True,
            "duration": gap, "timestamp": data["timestamp"],
        }]
    except (FileNotFoundError, KeyError, ValueError):
        return []
    except Exception as e:
        _LOG.warning("Startup gap check failed: %s", e)
        return []


# ── Agent-side aggregation ────────────────────────────────────────────────────────
def aggregate_events(events: list) -> list:
    if not events:
        return []
    merged = []
    cur = dict(events[0])
    for evt in events[1:]:
        same = (
            evt["app"]               == cur["app"] and
            evt["active"]            == cur["active"] and
            evt.get("locked", False) == cur.get("locked", False)
        )
        if same:
            cur["duration"] += evt.get("duration", 0)
            if evt.get("domain"):
                cur["domain"] = evt["domain"]
        else:
            merged.append(cur)
            cur = dict(evt)
    merged.append(cur)
    return merged


# ── Batch flush ───────────────────────────────────────────────────────────────────
def flush_batch(user: str, device: str, batch: list) -> bool:
    """
    POST a batch of raw events to the analytics server.
    Returns True on success, False on any failure.
    Buffer is NOT cleared here — caller decides based on return value.

    Mirrors telemetry_agent.flush_batch() — every error path now logs to
    stdout AND _LOG (when configured) with the same message.  The Linux
    agent runs as a systemd --user service so console output is normally
    discarded; without these _LOG calls the only diagnostic trail for a
    recurring "data not arriving" complaint would be a single journalctl
    line.  Each failure mode gets a distinct message that names the
    exception class and includes the server URL + event count so an
    operator can grep agent.log for the exact pattern.
    """
    if not batch:
        return True
    if INGEST_URL.startswith("http://") and getattr(sys, "frozen", False):
        try:
            _LOG.warning("Security: ingest URL uses plaintext HTTP -- telemetry data is unencrypted in transit")
        except Exception:
            pass
    try:
        resp = requests.post(
            INGEST_URL,
            json={"user": user, "device": device, "events": batch},
            headers={"X-API-Key": AGENT_API_KEY},
            timeout=10,
            verify=True,
        )
        if resp.status_code in (200, 202):
            data = resp.json()
            msg = f"  ->Batch sent: {data.get('accepted')}/{data.get('total')} events"
            print(msg)
            try:
                _LOG.debug("POST %s accepted=%s total=%s", INGEST_URL,
                           data.get("accepted"), data.get("total"))
            except Exception:
                pass
            return True
        # Server replied but didn't accept.  Body is truncated to 200 chars
        # so a runaway error page doesn't fill the log.
        body_preview = (resp.text or "")[:200]
        msg = f"  ->Server rejected [{resp.status_code}]: {body_preview}"
        print(msg)
        try:
            _LOG.error("POST %s returned HTTP %d for %d events -- body[:200]=%r",
                       INGEST_URL, resp.status_code, len(batch), body_preview)
        except Exception:
            pass
        return False
    except requests.exceptions.ConnectionError as e:
        msg = f"  ->Server unreachable ({INGEST_URL}). Events buffered locally."
        print(msg)
        try:
            _LOG.error("ConnectionError to %s (batch=%d events, user=%s): %s",
                       INGEST_URL, len(batch), user, e)
        except Exception:
            pass
        return False
    except requests.exceptions.Timeout as e:
        msg = f"  ->Server timeout after 10s ({INGEST_URL}). Events buffered locally."
        print(msg)
        try:
            _LOG.error("Timeout from %s (batch=%d events, user=%s): %s",
                       INGEST_URL, len(batch), user, e)
        except Exception:
            pass
        return False
    except requests.exceptions.SSLError as e:
        # Distinct message: this is the most common reason agents in
        # corporate / MITM-proxy environments fail silently.
        msg = f"  ->SSL error contacting {INGEST_URL}: {e}"
        print(msg)
        try:
            _LOG.error("SSLError to %s -- check CA bundle / corporate proxy: %s",
                       INGEST_URL, e)
        except Exception:
            pass
        return False
    except requests.exceptions.RequestException as e:
        msg = f"  ->HTTP error: {e}"
        print(msg)
        try:
            _LOG.error("RequestException to %s (batch=%d): %s",
                       INGEST_URL, len(batch), e)
        except Exception:
            pass
        return False
    except Exception as e:
        # Last-resort catch — should never fire but if it does the agent must
        # NOT crash (it would lose its systemd user-service relationship).
        msg = f"  ->Flush error ({type(e).__name__}): {e}"
        print(msg)
        try:
            _LOG.exception("Unexpected flush error to %s (batch=%d):",
                            INGEST_URL, len(batch))
        except Exception:
            pass
        return False


# ── Connection check + auto-update ───────────────────────────────────────────────
def _base_url() -> str:
    return (INGEST_URL.rsplit("/ingest", 1)[0]
            if "/ingest" in INGEST_URL else INGEST_URL.rsplit("/", 1)[0])


def _ver(v: str) -> tuple:
    """
    Robust semver-ish parser.  See telemetry_agent._ver() for full rationale.
    Handles '3.1.0-beta' -> (3, 1, 0) and None/garbage -> (0,) without raising.
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


def _do_update(download_url: str) -> None:
    """
    Download new linux_telemetry_agent.py, replace this file atomically,
    then re-exec so the new version takes over this process slot.

    Strategy
    --------
    1. Download to <script>.new  — keeps the running script intact on failure.
    2. Sanity-check size         — reject suspiciously small payloads.
    3. os.replace()              — atomic rename; never leaves a half-written file.
    4. os.execv()                — replace this process image in-place; PID stays
                                   the same so systemd keeps tracking it correctly.
    """
    current = os.path.abspath(__file__)
    tmp     = current + ".new"

    _LOG.info("Auto-update: downloading from %s", download_url)
    try:
        with requests.get(download_url, timeout=60, verify=True) as r:
            r.raise_for_status()
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(r.text)
    except Exception as e:
        _LOG.error("Auto-update: download failed -- %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    if os.path.getsize(tmp) < 1024:
        _LOG.error("Auto-update: downloaded file too small, aborting")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    # Preserve executable permission bits from the current script
    try:
        import stat as _stat
        mode = os.stat(current).st_mode | _stat.S_IEXEC | _stat.S_IXGRP | _stat.S_IXOTH
        os.chmod(tmp, mode)
    except Exception:
        pass

    # Atomic replace (POSIX rename semantics -- safe on Linux)
    try:
        os.replace(tmp, current)
    except Exception as e:
        _LOG.error("Auto-update: could not replace script -- %s", e)
        try:
            os.remove(tmp)
        except OSError:
            pass
        return

    _LOG.info("Auto-update: script replaced -- restarting via execv")

    # Re-exec: replace this process image with the updated script.
    # os.execv() never returns on success; on failure we log and continue
    # running the old in-memory bytecode until the next restart.
    try:
        os.execv(sys.executable, [sys.executable, current] + sys.argv[1:])
    except Exception as e:
        _LOG.error("Auto-update: re-exec failed -- %s  (restart manually)", e)


def check_for_update() -> None:
    """
    Called once at startup.  Queries /api/health; if the server reports a
    newer version, downloads and self-replaces via _do_update().
    Skips silently on any network error.
    """
    base       = _base_url()
    health_url = f"{base}/api/health"

    try:
        resp = requests.get(health_url, timeout=10, verify=True)
        if not resp.ok:
            return
        data           = resp.json()
        server_version = data.get("version", "0")
        download_url   = data.get(
            "linux_agent_download_url",
            f"{base}/download-linux-agent",
        )
    except Exception as e:
        _LOG.debug("Auto-update check skipped: %s", e)
        return

    if _ver(server_version) <= _ver(AGENT_VERSION):
        _LOG.info("Auto-update: up to date (v%s)", AGENT_VERSION)
        return

    _LOG.info(
        "Auto-update: server has v%s, running v%s -- updating",
        server_version, AGENT_VERSION,
    )
    _do_update(download_url)


def check_connection(retries: int = 3, delay: int = 5, base_url: str = None) -> bool:
    health_url = (base_url or _base_url()) + "/api/health"
    for attempt in range(1, retries + 1):
        try:
            r = requests.get(health_url, timeout=10, verify=True)
            if r.status_code == 200:
                _LOG.info("Connected to server (%s)", health_url)
                return True
            _LOG.warning("Health check %d/%d — HTTP %d", attempt, retries, r.status_code)
        except Exception as e:
            _LOG.warning("Health check %d/%d — %s", attempt, retries, e)
        if attempt < retries:
            time.sleep(delay)
    _LOG.error("Failed to connect after %d attempts (%s)", retries, health_url)
    return False


# ── Log rotation ──────────────────────────────────────────────────────────────────
def rotate_logs() -> None:
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_SIZE:
            old = LOG_PATH + ".old"
            if os.path.exists(old):
                os.remove(old)
            os.rename(LOG_PATH, old)
    except Exception:
        pass


# ── systemctl helper ──────────────────────────────────────────────────────────────
def _systemctl(*args) -> None:
    try:
        subprocess.run(["systemctl", "--user"] + list(args),
                       capture_output=True, timeout=10)
    except Exception as e:
        _LOG.warning("systemctl %s: %s", " ".join(args), e)


# ── Unit file templates ───────────────────────────────────────────────────────────
_AGENT_SERVICE = """\
[Unit]
Description=Telemetry Agent
After=graphical-session.target
PartOf=graphical-session.target

[Service]
Type=simple
ExecStart={exec_path}
Restart=on-failure
RestartSec=10
TimeoutStopSec=10
# Pass graphical session vars when the session manager provides them.
# Even when absent, the agent detects them from /proc at startup.
PassEnvironment=DISPLAY XAUTHORITY WAYLAND_DISPLAY DBUS_SESSION_BUS_ADDRESS XDG_SESSION_TYPE XDG_RUNTIME_DIR

[Install]
WantedBy=graphical-session.target
"""

_WATCHDOG_SERVICE = """\
[Unit]
Description=Telemetry Agent Watchdog (one-shot restart)
After=graphical-session.target

[Service]
Type=oneshot
ExecStart={exec_path}
"""

_WATCHDOG_TIMER = """\
[Unit]
Description=Telemetry Agent Watchdog Timer

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Unit=telemetry-agent-watchdog.service

[Install]
WantedBy=timers.target
"""

_AGENT_DESKTOP = """\
[Desktop Entry]
Type=Application
Name=TelemetryAgent
Exec={exec_path}
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=User activity telemetry agent (background)
"""


# ── Install / Uninstall ───────────────────────────────────────────────────────────
def install(server_url: str = None, admin_key: str = None) -> None:
    _LOG.info("=== Telemetry Agent Installation (Linux) ===")

    os.makedirs(DATA_DIR, exist_ok=True)
    _LOG.info("  Directory ready: %s", DATA_DIR)

    base      = (server_url or _base_url()).rstrip("/")
    agent_key = ""
    try:
        # /agent-config requires admin credentials — pass admin_key as X-API-Key.
        # Without it the server returns 401 and we fall back to manual key entry.
        headers = {"X-API-Key": admin_key} if admin_key else {}
        resp = requests.get(f"{base}/agent-config", headers=headers, timeout=10, verify=True)
        if resp.ok:
            cfg     = resp.json()
            fetched = cfg.get("server_url", "").rstrip("/")
            if fetched:
                base = fetched
            agent_key = cfg.get("agent_api_key", "")
            if agent_key:
                _LOG.info("  Agent API key received from /agent-config")
        elif resp.status_code == 401:
            _LOG.warning("  /agent-config requires --admin-key; skipping key auto-fetch")
    except Exception as e:
        _LOG.warning("  Could not fetch /agent-config: %s", e)

    username = getpass.getuser()
    if admin_key:
        try:
            resp = requests.post(
                f"{base}/api/register-device",
                json={"username": username},
                headers={"X-API-Key": admin_key},
                timeout=10,
            )
            if resp.ok:
                per_user_key = resp.json().get("agent_key", "")
                if per_user_key:
                    agent_key = per_user_key
                    _LOG.info("  Device registered — per-user key issued")
        except Exception as e:
            _LOG.warning("  Device registration failed: %s", e)

    config = {
        "ingest_url":     f"{base}/ingest",
        "api_key":        agent_key,
        "idle_threshold": IDLE_THRESHOLD,
        "tick_interval":  TICK_INTERVAL,
        "log_interval":   LOG_INTERVAL,
        "batch_size":     BATCH_SIZE,
    }
    # config.json goes in DATA_DIR — no ~/.config needed, avoids permission issues
    with open(USER_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    _LOG.info("  Config written: %s", USER_CONFIG_PATH)

    # Prefer the wrapper script created by install.sh (~/.local/bin/telemetry-agent)
    # because it is a real executable that calls the venv Python.  sys.argv[0] is a
    # plain .py file with no shebang — systemd cannot exec it directly (status=203/EXEC).
    _wrapper = os.path.join(os.path.expanduser("~"), ".local", "bin", "telemetry-agent")
    if os.path.isfile(_wrapper) and os.access(_wrapper, os.X_OK):
        exec_path = _wrapper
    else:
        # Fallback: explicit "venv_python script.py" — works without the wrapper
        exec_path = f"{sys.executable} {os.path.abspath(__file__)}"

    # systemd user units — under ~/.config/systemd/user/
    try:
        os.makedirs(SYSTEMD_USER_DIR, exist_ok=True)
        with open(os.path.join(SYSTEMD_USER_DIR, "telemetry-agent.service"), "w") as f:
            f.write(_AGENT_SERVICE.format(exec_path=exec_path))
        with open(os.path.join(SYSTEMD_USER_DIR, "telemetry-agent-watchdog.service"), "w") as f:
            f.write(_WATCHDOG_SERVICE.format(exec_path=exec_path))
        with open(os.path.join(SYSTEMD_USER_DIR, "telemetry-agent-watchdog.timer"), "w") as f:
            f.write(_WATCHDOG_TIMER)
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", "telemetry-agent.service")
        _systemctl("enable", "--now", "telemetry-agent-watchdog.timer")
        _LOG.info("  systemd user units enabled")
    except PermissionError as e:
        _LOG.warning("  Could not write systemd units to %s: %s", SYSTEMD_USER_DIR, e)
        _LOG.warning("  Agent will not auto-start via systemd — falling back to XDG autostart only")

    # XDG autostart — under ~/.config/autostart/
    try:
        os.makedirs(AUTOSTART_DIR, exist_ok=True)
        with open(os.path.join(AUTOSTART_DIR, "telemetry-agent.desktop"), "w") as f:
            f.write(_AGENT_DESKTOP.format(exec_path=exec_path))
        _LOG.info("  XDG autostart entry written")
    except PermissionError as e:
        _LOG.warning("  Could not write XDG autostart to %s: %s", AUTOSTART_DIR, e)
        _LOG.warning("  Add '%s' to your session startup manually", exec_path)

    if check_connection(base_url=base):
        _LOG.info("  Server connection: OK")
    else:
        _LOG.warning("  Server connection: FAILED — will retry at runtime")

    _LOG.info("=== Installation complete ===")
    _LOG.info("  Config  : %s", USER_CONFIG_PATH)
    _LOG.info("  Log     : %s", LOG_PATH)
    _LOG.info("  Data    : %s", DATA_DIR)


def uninstall() -> None:
    _LOG.info("=== Telemetry Agent Uninstall (Linux) ===")

    _systemctl("disable", "--now", "telemetry-agent.service")
    _systemctl("disable", "--now", "telemetry-agent-watchdog.timer")
    _systemctl("daemon-reload")

    for fname in (
        "telemetry-agent.service",
        "telemetry-agent-watchdog.service",
        "telemetry-agent-watchdog.timer",
    ):
        try:
            os.remove(os.path.join(SYSTEMD_USER_DIR, fname))
        except FileNotFoundError:
            pass

    try:
        os.remove(os.path.join(AUTOSTART_DIR, "telemetry-agent.desktop"))
    except FileNotFoundError:
        pass

    for path in [DATA_DIR, CONFIG_DIR]:
        shutil.rmtree(path, ignore_errors=True)
        _LOG.info("  Removed: %s", path)

    _LOG.info("=== Uninstall complete — cloud data not affected ===")


# ── Main loop ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="Linux Telemetry Agent")
    parser.add_argument("--install",    action="store_true")
    parser.add_argument("--uninstall",  action="store_true")
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--admin-key",  default=None)
    args = parser.parse_args()

    _setup_logging()

    if args.install:
        install(server_url=args.server_url, admin_key=args.admin_key)
        return
    if args.uninstall:
        uninstall()
        return

    # Ensure DISPLAY/XAUTHORITY/DBUS_SESSION_BUS_ADDRESS are available.
    # Systemd user services don't inherit the graphical session environment,
    # so xdotool and xprop silently fail without this.
    _detect_x11_env()

    if not _acquire_singleton():
        sys.exit(0)

    # Auto-update check: downloads + re-execs if server has a newer version.
    # Must run AFTER singleton guard so only one instance checks for updates.
    check_for_update()

    username = getpass.getuser()
    hostname = platform.node()
    state    = TelemetryState()

    event_buffer:        list = []
    elapsed_since_log:   int  = 0
    elapsed_since_flush: int  = 0
    last_event_app:      str  = None

    _LOG.info("Agent v%s started — %s @ %s", AGENT_VERSION, username, hostname)
    _LOG.info(
        "Display: %s | Tick: %ds | Event: %ds | Flush: %ds | URL: %s",
        "Wayland" if _IS_WAYLAND else "X11",
        TICK_INTERVAL, LOG_INTERVAL, FLUSH_INTERVAL, INGEST_URL,
    )

    if not check_connection(retries=3, delay=5):
        _LOG.warning("Startup connection check failed — will retry on each batch")

    flush_backup(username, hostname)

    gap_events = _startup_gap_events()
    if gap_events:
        if not flush_batch(username, hostname, gap_events):
            save_to_backup(username, hostname, gap_events)

    _last_tick_wall = time.time()
    _last_tick_mono = time.monotonic()         # monotonic anchor for tick elapsed
    state_eng = StateEngine(IDLE_THRESHOLD)   # activity state machine

    def _shutdown(sig, _frame):
        _LOG.info("Signal %d received — flushing and exiting", sig)
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            if not flush_batch(username, hostname, compressed):
                save_to_backup(username, hostname, compressed)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    # ── Sleep gap detection thresholds ────────────────────────────────────────
    # A "sleep gap" is any wall-clock jump > 3 ticks (default: 3*5=15 s) that
    # could be the machine coming back from suspend.  A single missed tick
    # is a normal scheduler hiccup, not a sleep cycle.  Multi-day gaps are
    # capped at 24 h (86400 s) to match the startup-gap cap so a single
    # partition can never be inflated by a long shutdown.
    SLEEP_GAP_MIN_SEC = TICK_INTERVAL * 3
    SLEEP_GAP_MAX_SEC = 86_400

    try:
        while True:
            # ── Monotonic elapsed for this tick ──────────────────────────────────
            _mono_now       = time.monotonic()
            _tick_elapsed   = _mono_now - _last_tick_mono
            _last_tick_mono = _mono_now

            # ── Wall-clock for sleep/resume gap detection ONLY ────────────────────
            # time.monotonic() is frozen during suspend; time.time() jumps on resume.
            # Negative deltas (NTP clock step backwards) are clamped to 0 — they
            # should never be classified as a "gap" but the previous code would
            # log a confusing "0s sleep gap captured" message.
            _wall_now   = time.time()
            _wall_delta = max(0.0, _wall_now - _last_tick_wall)
            _last_tick_wall = _wall_now

            if _wall_delta > SLEEP_GAP_MIN_SEC:
                # Cap the gap so a multi-day outage cannot inflate a single day
                # to more than 86400 s of locked time.  The uncapped wall_delta
                # is logged for diagnostics; the stored event uses the capped value.
                _raw_gap   = int(_wall_delta)
                _sleep_gap = min(_raw_gap, SLEEP_GAP_MAX_SEC)
                gap_start  = datetime.fromtimestamp(
                    _wall_now - _wall_delta, tz=timezone.utc
                ).isoformat()
                event_buffer.append({
                    "app": "Screen Off", "domain": "", "active": False, "locked": True,
                    "duration": _sleep_gap, "timestamp": gap_start,
                })
                if _raw_gap > SLEEP_GAP_MAX_SEC:
                    _LOG.warning(
                        "Sleep/resume gap: %ds captured (capped from %ds)",
                        _sleep_gap, _raw_gap,
                    )
                else:
                    _LOG.info(
                        "Sleep/resume gap: %ds of screen-off time captured (threshold=%ds)",
                        _sleep_gap, SLEEP_GAP_MIN_SEC,
                    )
                state_eng.on_sleep_gap(_sleep_gap)
                compressed = aggregate_events(event_buffer)
                if flush_batch(username, hostname, compressed):
                    event_buffer.clear()
                    flush_backup(username, hostname)
                else:
                    save_to_backup(username, hostname, compressed)
                    event_buffer.clear()
                elapsed_since_log   = 0
                elapsed_since_flush = 0
                _last_tick_mono = time.monotonic()
                time.sleep(TICK_INTERVAL)
                continue

            # ── Sample current state ───────────────────────────────────────────────
            is_locked = is_session_locked()
            idle_secs = get_idle_seconds()
            app_name, win_title = get_active_window()
            domain = extract_domain(app_name, win_title) if not is_locked else ""

            state.update(app_name)
            elapsed_since_log   += _tick_elapsed
            elapsed_since_flush += _tick_elapsed

            current_state = state_eng.tick(idle_secs, is_locked, _tick_elapsed)
            is_active = current_state == ActivityState.ACTIVE

            _write_status_file(app_name, is_active, is_locked, idle_secs)

            # ── Every LOG_INTERVAL: build one raw event ────────────────────────────
            if elapsed_since_log >= LOG_INTERVAL:
                rotate_logs()
                now = datetime.now(timezone.utc).isoformat()
                _event_duration = max(1, min(int(elapsed_since_log), LOG_INTERVAL * 3))

                event_buffer.append({
                    "app":       state.current_app,
                    "domain":    domain,
                    "active":    is_active,
                    "locked":    is_locked,
                    "duration":  _event_duration,
                    "timestamp": now,
                })
                _save_last_seen(now, state.current_app)
                _accumulate(state.current_app, domain, is_active, is_locked, _event_duration)
                state_eng.enforce_invariant()
                _write_cache(username, hostname)

                prev_app       = last_event_app
                last_event_app = state.current_app
                app_switched   = prev_app is not None and state.current_app != prev_app
                time_to_flush  = elapsed_since_flush >= FLUSH_INTERVAL
                cap_reached    = len(event_buffer) >= BATCH_SIZE

                if (time_to_flush or app_switched or cap_reached) and event_buffer:
                    compressed = aggregate_events(event_buffer)
                    if flush_batch(username, hostname, compressed):
                        event_buffer.clear()
                        flush_backup(username, hostname)
                    else:
                        save_to_backup(username, hostname, compressed)
                        event_buffer.clear()
                    elapsed_since_flush = 0

                elapsed_since_log = 0

            time.sleep(TICK_INTERVAL)

    except Exception as e:
        _LOG.error("Agent crash: %s", e)
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            if not flush_batch(username, hostname, compressed):
                save_to_backup(username, hostname, compressed)


if __name__ == "__main__":
    main()
