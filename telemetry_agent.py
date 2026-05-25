"""
telemetry_agent.py — Windows background agent.

What it does
------------
- Samples the foreground window every TICK_INTERVAL seconds
- Builds one raw event every LOG_INTERVAL seconds (default: 60 s)
- Merges consecutive events with the same (app, active, locked) before sending
- Flushes to the server every FLUSH_INTERVAL seconds OR when the active app changes
- Writes the same data to logs.txt for offline analysis
- On server failure: saves the compressed batch to disk (%TEMP%/telemetry_backup/)
- On reconnect (startup or next successful flush): replays backed-up batches
- On shutdown: saves any unsent events to disk backup

Agent-side aggregation
----------------------
Before each POST, consecutive events that share the same app, active state, and
locked state are merged into a single event whose duration is the sum of the
originals.  This reduces event volume to the server dramatically when the user
stays in one application for an extended period.

Example:
  Raw buffer (3 events × 60 s):
    [{app: Code.exe, active: true, duration: 60}, ...same..., ...same...]
  After aggregate_events():
    [{app: Code.exe, active: true, duration: 180}]

Flush triggers (whichever comes first)
---------------------------------------
1. FLUSH_INTERVAL elapsed (default: 120 s = 2 events at 60 s/event)
2. Active app changes between two consecutive events
3. Safety cap: BATCH_SIZE events buffered without a flush

Offline backup
--------------
Layout : <TEMP>/telemetry_backup/<username>/batch_<timestamp>.json
Cap    : MAX_BACKUP_EVENTS (500) total events on disk — oldest evicted first
Replay : oldest-first; stops at first failure so partial recovery is safe

Batch payload sent to POST /ingest
-----------------------------------
{
    "user":   "MohamedAshmar",
    "device": "E813-Ashmar",
    "events": [
        {"app": "Code.exe", "domain": "", "active": true,  "duration": 180, "timestamp": "..."},
        {"app": "brave.exe","domain": "YouTube", "active": true, "duration": 120, "timestamp": "..."},
        ...
    ]
}
"""

import os
import sys
import time
import json
import glob
import tempfile
import logging
import argparse
import shutil
import subprocess
import platform
import getpass
import re
import ctypes
from datetime import datetime, timezone
from enum import Enum

import requests

# ── System paths (production install locations) ─────────────────────────────────
PROGRAM_DATA       = r"C:\ProgramData\TelemetryAgent"
INSTALL_DIR        = r"C:\Program Files\TelemetryAgent"
SYSTEM_CONFIG_PATH = os.path.join(PROGRAM_DATA, "config.json")
LOG_PATH           = os.path.join(PROGRAM_DATA, "agent.log")
LAST_SEEN_PATH     = os.path.join(PROGRAM_DATA, "last_seen.json")

try:
    import win32gui
    import win32process
    import psutil
except ImportError:
    print("Error: pywin32 and psutil are required.  Run: pip install pywin32 psutil")
    sys.exit(1)


# ── Config file (agent.config.json sits next to this script) ────────────────────
# Deploy one config file per site/environment — no env vars needed on each machine.
# env var INGEST_URL overrides config file if both are present.

def _load_config() -> dict:
    """
    Priority order:
    1. C:\\ProgramData\\TelemetryAgent\\config.json  (production install)
    2. agent.config.json next to this script          (local / dev)
    """
    candidates = [
        SYSTEM_CONFIG_PATH,
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.config.json"),
    ]
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as f:
                cfg = json.load(f)
                print(f"[config] Loaded {path}")
                return cfg
        except FileNotFoundError:
            continue
        except json.JSONDecodeError as e:
            print(f"[config] {path} parse error: {e} — skipping")
    return {}

_cfg = _load_config()


# ── Configuration ───────────────────────────────────────────────────────────────

IDLE_THRESHOLD    = _cfg.get("idle_threshold",  300)  # seconds
AGENT_VERSION     = "3.1"   # bump this before every EXE build

TICK_INTERVAL     = _cfg.get("tick_interval",    5)   # seconds
LOG_INTERVAL      = _cfg.get("log_interval",    30)   # seconds — 30s balances granularity vs storage cost
BATCH_SIZE        = _cfg.get("batch_size",      10)   # safety cap: flush if buffer reaches this
FLUSH_INTERVAL    = _cfg.get("flush_interval",  60)   # seconds — 60s + flush-on-app-switch keeps lag < 60s
MAX_BACKUP_EVENTS = 500   # ~6 days of average usage; oldest events evicted when exceeded

LOG_FILE      = os.path.join(tempfile.gettempdir(), "TelemetryAgent", "logs.txt")
MAX_LOG_SIZE  = 10 * 1024 * 1024   # 10 MB

# Local cache files — shared with the UI companion process
CACHE_PATH  = os.path.join(PROGRAM_DATA, "cache.json")   # daily summary + top apps + hourly bars
STATUS_PATH = os.path.join(PROGRAM_DATA, "status.json")  # current status, updated every tick (5s)

# Resolution order: env var → config file → default
INGEST_URL = os.getenv("INGEST_URL") or _cfg.get("ingest_url", "http://localhost:8000/ingest")
AGENT_API_KEY = os.getenv("AGENT_API_KEY") or os.getenv("API_KEY") or _cfg.get("api_key", "")

BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}

# ── Minimal local categorisation (mirrors aggregator.py, no import needed) ──────
_UNPRODUCTIVE_DOMAINS = {
    "youtube.com", "youtu.be", "netflix.com", "primevideo.com", "hulu.com",
    "disneyplus.com", "twitch.tv", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "tiktok.com", "reddit.com", "9gag.com",
    "amazon.com", "ebay.com", "buzzfeed.com", "espn.com",
    "web.whatsapp.com", "web.telegram.org",
}
_UNPRODUCTIVE_TITLE_KW = {
    "youtube", "netflix", "prime video", "hulu", "disney+", "twitch",
    "instagram", "facebook", "twitter", " x.com", "tiktok", "reddit",
    "buzzfeed", "espn", "whatsapp", "telegram",
}
_UNPRODUCTIVE_APPS = {
    "steam.exe", "epicgameslauncher.exe", "spotify.exe", "vlc.exe",
    "battle.net.exe", "leagueclient.exe", "valorant.exe",
}


def _local_categorize(app: str, domain: str) -> str:
    """Lightweight version of aggregator.categorize() for local cache scoring."""
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    if d:
        if any(k in d for k in _UNPRODUCTIVE_DOMAINS):
            return "Unproductive"
        if any(k in d for k in _UNPRODUCTIVE_TITLE_KW):
            return "Unproductive"
    a = (app or "").lower().strip()
    if any(k in a for k in _UNPRODUCTIVE_APPS):
        return "Unproductive"
    return "Productive"


# ── In-memory daily accumulators (reset at midnight) ────────────────────────────
_acc_date:         str       = ""        # tracks current date; "" triggers first-run init
_acc_active:       int       = 0         # total active seconds today
_acc_idle:         int       = 0         # total idle seconds today
_acc_locked:       int       = 0         # total screen-off seconds today
_acc_productive:   int       = 0         # active seconds on productive apps
_acc_app_times:    dict      = {}        # {app_name: seconds}
_acc_hourly:       list      = [0] * 24  # active seconds per clock hour (local time)


def _check_day_reset() -> None:
    """Reset accumulators when the calendar date rolls over."""
    global _acc_date, _acc_active, _acc_idle, _acc_locked
    global _acc_productive, _acc_app_times, _acc_hourly
    today = datetime.now(timezone.utc).date().isoformat()
    if _acc_date == today:
        return
    _acc_date      = today
    _acc_active    = 0
    _acc_idle      = 0
    _acc_locked    = 0
    _acc_productive = 0
    _acc_app_times = {}
    _acc_hourly    = [0] * 24


def _accumulate(app: str, domain: str, is_active: bool, is_locked: bool, duration: int) -> None:
    """Update in-memory counters for one event interval."""
    global _acc_active, _acc_idle, _acc_locked, _acc_productive
    _check_day_reset()
    if is_locked:
        _acc_locked += duration
    elif is_active:
        _acc_active += duration
        _acc_app_times[app] = _acc_app_times.get(app, 0) + duration
        hour = datetime.now(timezone.utc).astimezone().hour  # local clock hour
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
    SLEEP  = "sleep"


class StateEngine:
    """
    State-machine that tracks ACTIVE / IDLE / LOCKED / SLEEP using
    time.monotonic() deltas exclusively.  Never uses event durations
    or constant TICK_INTERVAL additions.

    Invariant: active_seconds <= session_elapsed_seconds always.
    """

    def __init__(self, idle_threshold: int):
        self._threshold      = idle_threshold
        self._state          = ActivityState.IDLE
        self._mono_start     = time.monotonic()
        self._active_seconds = 0.0
        self._idle_seconds   = 0.0
        self._locked_seconds = 0.0

    def tick(self, idle_secs: int, is_locked: bool, tick_elapsed: float) -> "ActivityState":
        """
        Advance the engine by one tick.
        tick_elapsed: actual seconds elapsed this tick (from time.monotonic()).
        Also shifts _mono_start so session_elapsed_seconds stays consistent.
        """
        tick_elapsed = max(0.0, min(tick_elapsed, float(self._threshold)))

        # Keep session_elapsed in sync with accumulated tick_elapsed values
        self._mono_start -= tick_elapsed

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
        """Gap time is classified as LOCKED but does NOT advance active counter.
        Shifts _mono_start backward so session_elapsed_seconds reflects the gap."""
        gap = max(0.0, gap_seconds)
        self._locked_seconds += gap
        self._mono_start -= gap

    def enforce_invariant(self) -> None:
        """Hard cap: active + idle + locked must not exceed session elapsed."""
        session_el = self.session_elapsed_seconds
        total = self._active_seconds + self._idle_seconds + self._locked_seconds
        if total > session_el + 1.0:
            if total > 0:
                scale = session_el / total
                self._active_seconds  *= scale
                self._idle_seconds    *= scale
                self._locked_seconds  *= scale
            _LOG.warning(
                "StateEngine invariant violation: total=%.0fs session=%.0fs — scaled down",
                total, session_el,
            )

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
        return time.monotonic() - self._mono_start

    def to_cache_dict(self) -> dict:
        """Return counters safe for writing to cache.json."""
        self.enforce_invariant()
        return {
            "active_seconds":  int(self._active_seconds),
            "idle_seconds":    int(self._idle_seconds),
            "locked_seconds":  int(self._locked_seconds),
            "session_elapsed": int(self.session_elapsed_seconds),
        }


def _write_status_file(app: str, active: bool, locked: bool, idle_secs: int) -> None:
    """
    Write current agent state to STATUS_PATH every tick (~5 s).
    The UI reads this for the real-time status indicator (<5 s lag).
    """
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        payload = {
            "timestamp":   datetime.now(timezone.utc).isoformat(),
            "app":         app,
            "active":      active,
            "locked":      locked,
            "idle_seconds": idle_secs,
        }
        # Write to a temp file then rename — atomic on Windows
        tmp = STATUS_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, STATUS_PATH)
    except Exception:
        pass  # non-fatal; UI falls back to server data


def _write_cache(username: str, device: str) -> None:
    """
    Write daily summary + top-apps + hourly activity to CACHE_PATH
    after every event interval (~15 s).  The UI companion reads this when
    the server is unreachable or for faster first-paint.
    """
    _check_day_reset()
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        total_scored = _acc_active if _acc_active else 1
        score = round(_acc_productive / total_scored * 100, 1)

        top_apps = sorted(
            [{"app": a, "time": t,
              "category": _local_categorize(a, "")}
             for a, t in _acc_app_times.items()],
            key=lambda x: x["time"], reverse=True
        )[:10]

        top_app = top_apps[0]["app"] if top_apps else "None"

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
                "top_app":               top_app,
            },
            "top_apps":       top_apps,
            "hourly_active":  _acc_hourly,
        }
        tmp = CACHE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        os.replace(tmp, CACHE_PATH)
    except Exception:
        pass  # non-fatal


# ── Structured logging ───────────────────────────────────────────────────────────
# _LOG is configured in _setup_logging() (called at the start of main/install).
# All important events (startup, connection, errors) go through this logger so
# they appear both in the console (dev) and in agent.log (production).
# Verbose per-event print() calls are intentionally left as print() — they are
# invisible in noconsole / service mode and are only useful during development.

_LOG = logging.getLogger("telemetry_agent")


def _setup_logging() -> None:
    """
    Configure _LOG with:
      - FileHandler  ->C:\\ProgramData\\TelemetryAgent\\agent.log  (always)
      - StreamHandler → stdout  (only when a console is attached)
    Safe to call multiple times (guards against duplicate handlers).
    """
    if _LOG.handlers:
        return  # already configured

    _LOG.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — create ProgramData dir if needed
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)
    except Exception as e:
        print(f"[log] Cannot create log file {LOG_PATH}: {e}")

    # Console handler — skip in noconsole / frozen-windowless mode
    try:
        if sys.stdout is not None:
            ch = logging.StreamHandler(sys.stdout)
            ch.setFormatter(fmt)
            _LOG.addHandler(ch)
    except Exception:
        pass


# ── State tracker ───────────────────────────────────────────────────────────────

class TelemetryState:
    """Minimal in-memory state — only tracks current app and session start time."""

    def __init__(self):
        self.current_app  = "Unknown"
        self._last_switch = time.time()

    def update(self, app_name: str):
        if app_name != self.current_app:
            self.current_app  = app_name
            self._last_switch = time.time()

    def session_duration(self) -> int:
        return int(time.time() - self._last_switch)


# ── Windows helpers ─────────────────────────────────────────────────────────────

def get_user_info() -> dict:
    try:
        return {"hostname": platform.node(), "username": getpass.getuser()}
    except Exception:
        return {"hostname": "Unknown", "username": "Unknown"}


class _LASTINPUTINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]


def get_idle_seconds() -> int:
    """
    Returns seconds since the last keyboard/mouse input in this session.

    Uses ctypes + LASTINPUTINFO struct directly instead of win32api.GetLastInputInfo()
    because the pywin32 wrapper returns 0 on some Windows configurations (treating a
    failed/uninitialised struct as 'last input at boot'), which makes
    GetTickCount() - 0 = system_uptime, always exceeding IDLE_THRESHOLD and
    marking every event as idle regardless of actual user activity.
    """
    try:
        lii = _LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(_LASTINPUTINFO)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
            return 0
        tick    = ctypes.windll.kernel32.GetTickCount()
        idle_ms = (tick - lii.dwTime) & 0xFFFFFFFF  # unsigned 32-bit wrap-safe subtraction
        return idle_ms // 1000
    except Exception:
        return 0




LOCK_SCREEN_PROCESSES = frozenset({"lockapp.exe", "logonui.exe"})


def is_workstation_locked() -> bool:
    """
    Returns True when the Windows workstation is locked.

    Three complementary checks (any one is sufficient):

    1. GetForegroundWindow() == 0
       On Windows 10/11, the lock screen (LockApp.exe) runs on a separate
       secure desktop ('Winlogon').  The user session cannot see any window
       there, so GetForegroundWindow() returns 0 while locked.

    2. Foreground process is a known lock-screen process
       Catches the case where the lock-screen window IS accessible (some
       configurations return the LockApp/LogonUI window handle).

    3. OpenInputDesktop desktop-name check
       Fallback for older Windows where the input desktop switches to
       'Winlogon' on lock; doesn't fire on modern Windows but costs nothing.
    """
    # ── Check 1 & 2: foreground window ──────────────────────────────────────
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            # No window visible in user session → secure desktop is active (locked)
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if psutil.Process(pid).name().lower() in LOCK_SCREEN_PROCESSES:
            return True
    except Exception:
        pass

    # ── Check 3: input desktop name (classic lock, older Windows) ────────────
    try:
        hdesk = ctypes.windll.user32.OpenInputDesktop(0, False, 0x0001)
        if not hdesk:
            return True
        buf = ctypes.create_unicode_buffer(256)
        ctypes.windll.user32.GetUserObjectInformationW(
            hdesk, 2, buf, ctypes.sizeof(buf), None
        )
        ctypes.windll.user32.CloseDesktop(hdesk)
        return buf.value.lower() != "default"
    except Exception:
        return False


def get_foreground_app():
    """Returns (process_name, hwnd) or ("Unknown", None) on failure."""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "Unknown", None
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        return psutil.Process(pid).name(), hwnd
    except Exception:
        return "Unknown", None


def extract_domain(hwnd, process_name: str) -> str:
    """
    Best-effort domain/title extraction for browser windows.
    Returns empty string for non-browser processes.
    """
    if process_name.lower() not in BROWSER_PROCESSES:
        return ""
    try:
        title = win32gui.GetWindowText(hwnd) or ""
        # Strip trailing "— Google Chrome", "— Firefox", etc.
        return re.sub(
            r"\s[-–]\s(Google Chrome|Microsoft Edge|Firefox|Brave).*$",
            "", title, flags=re.IGNORECASE
        ).strip()
    except Exception:
        return ""


def rotate_logs():
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
            old = LOG_FILE + ".old"
            if os.path.exists(old):
                os.remove(old)
            os.rename(LOG_FILE, old)
    except Exception as e:
        print(f"  [warn] Log rotation failed: {e}")


# ── Offline backup ───────────────────────────────────────────────────────────────
# When the server is unreachable, batches are written to disk so they survive
# process restarts.  On reconnect they are replayed in chronological order.
#
# Layout : %TEMP%/telemetry_backup/<username>/batch_<YYYYMMDDTHHMMSSffffff>.json
# Cap    : MAX_BACKUP_EVENTS total events across all files — oldest evicted first.

def _backup_dir(username: str) -> str:
    path = os.path.join(tempfile.gettempdir(), "telemetry_backup", username)
    already_exists = os.path.isdir(path)
    os.makedirs(path, exist_ok=True)
    if not already_exists:
        # Restrict permissions once on first creation — no need to repeat every call.
        try:
            subprocess.call(
                ["icacls", path, "/inheritance:r", "/grant:r", f"{username}:(OI)(CI)F"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
    return path


def _backup_files(username: str) -> list:
    """Sorted list of backup file paths, oldest first."""
    d = _backup_dir(username)
    return sorted(glob.glob(os.path.join(d, "batch_*.json")))


def save_to_backup(username: str, device: str, events: list) -> None:
    """
    Persist a failed batch to disk.
    Evicts the oldest backup files first when the cap would be exceeded.
    """
    if not events:
        return

    files = _backup_files(username)

    # Count current backed-up events and evict oldest until there is room
    total = 0
    counts = []
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8") as fh:
                n = len(json.load(fh).get("events", []))
        except Exception:
            n = 0
        total += n
        counts.append((fpath, n))

    for fpath, n in counts:
        if total + len(events) <= MAX_BACKUP_EVENTS:
            break
        try:
            os.remove(fpath)
            total -= n
            print(f"  [backup] Evicted {os.path.basename(fpath)} to make room")
        except Exception:
            pass

    ts    = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    fpath = os.path.join(_backup_dir(username), f"batch_{ts}.json")
    try:
        with open(fpath, "w", encoding="utf-8") as f:
            json.dump({"user": username, "device": device, "events": events}, f)
    except Exception as e:
        print(f"  [backup] Disk write failed: {e}")
        return
    print(f"  [backup] {len(events)} events saved offline -> {fpath}")


def flush_backup(username: str, device: str) -> int:
    """
    Replay backed-up batches to the server, oldest first.
    Stops at the first failure so partial recovery is safe.
    Returns the total number of events successfully sent.
    """
    files = _backup_files(username)
    if not files:
        return 0

    recovered = 0
    for fpath in files:
        try:
            with open(fpath, encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            print(f"  [backup] Skipping unreadable file {os.path.basename(fpath)}: {e}")
            continue

        events = payload.get("events", [])
        if not events:
            try:
                os.remove(fpath)
            except Exception:
                pass
            continue

        ok = flush_batch(payload.get("user", username), payload.get("device", device), events)
        if ok:
            try:
                os.remove(fpath)
            except Exception:
                pass
            recovered += len(events)
            print(f"  [backup] Recovered {len(events)} events from {os.path.basename(fpath)}")
        else:
            break  # Server still down — leave remaining files for next attempt

    if recovered:
        print(f"  [backup] Total recovered this session: {recovered} events")
    return recovered


# ── Last-seen state (startup gap detection) ─────────────────────────────────────
# Written on every event so we can compute how long the machine was off/asleep
# the next time the agent starts.

def _save_last_seen(timestamp: str, app: str) -> None:
    """Persist the timestamp of the most recent logged event to disk."""
    try:
        with open(LAST_SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump({"timestamp": timestamp, "app": app}, f)
    except Exception:
        pass


def _startup_gap_events() -> list:
    """
    On agent start, read the last-seen timestamp and return a synthetic
    locked/screen-off event covering any gap since the agent was last running.

    This captures time the machine was asleep or the agent was stopped between
    sessions — time that would otherwise be silently lost.

    Returns an empty list if there is no last-seen file or the gap is too small
    to be meaningful (< 2 × LOG_INTERVAL to avoid noise from normal restarts).
    """
    try:
        with open(LAST_SEEN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        last_ts_str = data.get("timestamp", "")
        if not last_ts_str:
            return []
        # Parse — handle both offset-aware and naive ISO strings
        last_ts = datetime.fromisoformat(last_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        now     = datetime.now(timezone.utc)
        gap_sec = int((now - last_ts).total_seconds())
        if gap_sec < LOG_INTERVAL * 2:          # < 2 min — noise, skip
            return []
        _LOG.info("Startup gap: %ds since last event (%s) — inserting screen-off time", gap_sec, last_ts_str)
        return [{
            "app":       "Screen Off",
            "domain":    "",
            "active":    False,
            "locked":    True,
            "duration":  gap_sec,
            "timestamp": last_ts_str,  # gap started when agent last logged
        }]
    except (FileNotFoundError, KeyError, ValueError):
        return []
    except Exception as e:
        _LOG.warning("Startup gap check failed: %s", e)
        return []


# ── Agent-side aggregation ───────────────────────────────────────────────────────

def aggregate_events(events: list) -> list:
    """
    Merge consecutive events that share the same (app, active, locked) state.

    duration is summed across the merged run.
    timestamp is kept from the FIRST event in the run (marks when it started).
    domain is taken from the LAST event in the run (most recent browser title).

    This reduces the payload sent to the server when the user stays in one
    application for an extended period.

    Example input  (3 × 60 s in Code.exe):
        [{app: Code.exe, active: True, locked: False, duration: 60}, × 3]
    Example output (1 merged event):
        [{app: Code.exe, active: True, locked: False, duration: 180}]
    """
    if not events:
        return []

    merged = []
    cur = dict(events[0])

    for evt in events[1:]:
        same_state = (
            evt["app"]               == cur["app"] and
            evt["active"]            == cur["active"] and
            evt.get("locked", False) == cur.get("locked", False)
        )
        if same_state:
            cur["duration"] += evt.get("duration", 0)
            if evt.get("domain"):          # keep most-recent browser title
                cur["domain"] = evt["domain"]
        else:
            merged.append(cur)
            cur = dict(evt)

    merged.append(cur)
    return merged


# ── Batch flush ─────────────────────────────────────────────────────────────────

def flush_batch(user: str, device: str, batch: list) -> bool:
    """
    POST a batch of raw events to the analytics server.
    Returns True on success, False on any failure.
    Buffer is NOT cleared here — caller decides based on return value.
    """
    if not batch:
        return True
    if INGEST_URL.startswith("http://") and getattr(sys, "frozen", False):
        _LOG.warning("Security: ingest URL uses plaintext HTTP — telemetry data is unencrypted in transit")
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
            print(f"  ->Batch sent: {data.get('accepted')}/{data.get('total')} events accepted")
            return True
        print(f"  ->Server rejected batch [{resp.status_code}]: {resp.text[:200]}")
        return False
    except requests.exceptions.ConnectionError:
        print(f"  ->Server unreachable ({INGEST_URL}). Events buffered locally.")
        return False
    except Exception as e:
        print(f"  ->Flush error: {e}")
        return False


# ── Install helpers ──────────────────────────────────────────────────────────────

def _base_url() -> str:
    """Derive server base URL from INGEST_URL (strips /ingest suffix)."""
    url = INGEST_URL
    return url.rsplit("/ingest", 1)[0] if "/ingest" in url else url.rsplit("/", 1)[0]


def check_connection(retries: int = 3, delay: int = 5) -> bool:
    """
    GET /api/health and return True on HTTP 200.
    Retries up to `retries` times with `delay` seconds between attempts.
    Logs each attempt via _LOG.
    """
    health_url = _base_url() + "/api/health"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(health_url, timeout=10, verify=True)
            if resp.status_code == 200:
                _LOG.info("Connected to server successfully (%s)", health_url)
                return True
            _LOG.warning(
                "Health check attempt %d/%d — HTTP %d from %s",
                attempt, retries, resp.status_code, health_url,
            )
        except requests.exceptions.ConnectionError:
            _LOG.warning(
                "Health check attempt %d/%d — server unreachable (%s)",
                attempt, retries, health_url,
            )
        except Exception as e:
            _LOG.warning("Health check attempt %d/%d — %s", attempt, retries, e)

        if attempt < retries:
            time.sleep(delay)

    _LOG.error("Failed to connect to server (%s) after %d attempts", health_url, retries)
    return False


# ── Auto-update ──────────────────────────────────────────────────────────────────

def _ver(v: str) -> tuple:
    """'2.10' → (2, 10) for correct numeric comparison."""
    try:
        return tuple(int(x) for x in v.strip().split("."))
    except Exception:
        return (0,)


def _do_update(download_url: str, current_exe: str) -> None:
    """
    Download the new ZIP release, then hand off to a hidden PowerShell script
    that waits for this process to exit, extracts the ZIP over the install
    directory, unblocks all files, and re-launches the agent.
    """
    install_dir = os.path.dirname(current_exe)
    tmp_dir  = os.path.join(tempfile.gettempdir(), "TelemetryAgent")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_zip  = os.path.join(tmp_dir, "telemetry_agent_new.zip")
    ps_path  = os.path.join(tmp_dir, "updater.ps1")

    _LOG.info("Auto-update: downloading from %s", download_url)
    try:
        with requests.get(download_url, stream=True, timeout=60, verify=True) as r:
            r.raise_for_status()
            with open(tmp_zip, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
    except Exception as e:
        _LOG.error("Auto-update: download failed — %s", e)
        return

    if os.path.getsize(tmp_zip) < 1024:
        _LOG.error("Auto-update: downloaded file too small, aborting")
        return

    _LOG.info("Auto-update: download complete (%d bytes), preparing updater",
              os.path.getsize(tmp_zip))

    # Hidden PowerShell updater: waits for this process to exit, then extracts
    # the ZIP over the install directory and re-launches.
    ps_lines = [
        "Start-Sleep -Seconds 3",
        f"Expand-Archive -Path '{tmp_zip}' -DestinationPath '{install_dir}' -Force",
        f"Get-ChildItem -Path '{install_dir}' -Recurse | Unblock-File -ErrorAction SilentlyContinue",
        f"Remove-Item '{tmp_zip}' -Force -ErrorAction SilentlyContinue",
        f"Start-Process '{current_exe}'",
        "Remove-Item $MyInvocation.MyCommand.Path -Force -ErrorAction SilentlyContinue",
    ]
    with open(ps_path, "w", encoding="utf-8") as f:
        f.write("\n".join(ps_lines))

    subprocess.Popen(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-WindowStyle", "Hidden", "-File", ps_path],
        creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
        close_fds=True,
    )
    _LOG.info("Auto-update: updater launched — exiting for replacement")
    sys.exit(0)


def check_for_update() -> None:
    """
    Called once at startup (frozen EXE only).
    Queries /api/health for the server version; if newer than AGENT_VERSION
    downloads and self-replaces via _do_update().
    Skips silently in dev mode (no sys.frozen) or on any network error.
    """
    if not getattr(sys, "frozen", False):
        return   # dev mode — never self-replace

    current_exe = sys.executable
    base        = _base_url()
    health_url  = f"{base}/api/health"

    try:
        resp = requests.get(health_url, timeout=10, verify=True)
        if not resp.ok:
            return
        data            = resp.json()
        server_version  = data.get("version", "0")
        download_url    = data.get("agent_zip_download_url", f"{base}/download-agent-zip")
    except Exception as e:
        _LOG.debug("Auto-update check skipped: %s", e)
        return

    if _ver(server_version) > _ver(AGENT_VERSION):
        _LOG.info(
            "Auto-update: server has v%s, running v%s — updating",
            server_version, AGENT_VERSION,
        )
        _do_update(download_url, current_exe)
    else:
        _LOG.info("Auto-update: up to date (v%s)", AGENT_VERSION)


def _schtasks_import_xml(task_name: str, xml: str) -> bool:
    """
    Write a Task Scheduler XML to a temp file and import it via schtasks.exe.

    Using schtasks /create /xml is the safest approach:
    - No PowerShell, no script execution, no -ExecutionPolicy flags
    - schtasks.exe is a signed native Windows binary → no Defender alerts
    - XML written to PROGRAM_DATA (trusted directory, not temp)
    - File is deleted immediately after import whether it succeeded or not

    The subprocess call uses both CREATE_NO_WINDOW and STARTF_USESHOWWINDOW
    so no console or window can appear even for a fraction of a second.
    """
    xml_path = os.path.join(PROGRAM_DATA, f"{task_name}.xml")
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        # schtasks expects UTF-16 LE with BOM; Python's 'utf-16' adds BOM on LE systems
        with open(xml_path, "w", encoding="utf-16") as f:
            f.write(xml)
    except Exception as e:
        _LOG.error("Could not write task XML for %s: %s", task_name, e)
        return False

    si = subprocess.STARTUPINFO()
    si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
    si.wShowWindow = 0  # SW_HIDE

    try:
        r = subprocess.run(
            ["schtasks", "/create", "/tn", task_name, "/xml", xml_path, "/f"],
            capture_output=True, text=True, timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=si,
        )
        if r.returncode == 0:
            return True
        _LOG.debug("schtasks /xml import failed for %s: %s", task_name, r.stderr.strip())
        return False
    except Exception as e:
        _LOG.error("schtasks import failed for %s: %s", task_name, e)
        return False
    finally:
        try:
            os.remove(xml_path)
        except Exception:
            pass


def _task_xml(exe_path: str, trigger_xml: str) -> str:
    """Build a Task Scheduler v1.2 XML string with common settings."""
    exe_escaped = (
        exe_path
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    return (
        '<?xml version="1.0" encoding="UTF-16"?>\n'
        '<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">\n'
        f"  <Triggers>{trigger_xml}</Triggers>\n"
        "  <Actions Context=\"Author\">\n"
        "    <Exec>\n"
        f"      <Command>{exe_escaped}</Command>\n"
        "    </Exec>\n"
        "  </Actions>\n"
        "  <Settings>\n"
        "    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>\n"
        "    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>\n"
        "    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>\n"
        "    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>\n"
        "    <StartWhenAvailable>true</StartWhenAvailable>\n"
        "    <Enabled>true</Enabled>\n"
        "  </Settings>\n"
        "  <Principals>\n"
        "    <Principal id=\"Author\">\n"
        "      <LogonType>InteractiveToken</LogonType>\n"
        "      <RunLevel>HighestAvailable</RunLevel>\n"
        "    </Principal>\n"
        "  </Principals>\n"
        "</Task>"
    )


def _register_scheduled_task(exe_path: str) -> bool:
    """
    Register TelemetryAgent — fires at every logon with restart-on-failure.

    Uses schtasks /create /xml (no PowerShell, no script execution).
    RestartOnFailure is a Settings element only available via XML import,
    not the schtasks command-line flags.
    """
    trigger = (
        "\n    <LogonTrigger>"
        "\n      <RestartOnFailure>"
        "\n        <Count>5</Count>"
        "\n        <Interval>PT2M</Interval>"
        "\n      </RestartOnFailure>"
        "\n      <Enabled>true</Enabled>"
        "\n    </LogonTrigger>"
    )
    xml = _task_xml(exe_path, trigger)
    return _schtasks_import_xml("TelemetryAgent", xml)


def _register_watchdog_task(exe_path: str) -> bool:
    """
    Register TelemetryAgentWatchdog — runs telemetry_agent.exe every 5 minutes.

    The agent's own named mutex (_acquire_singleton) exits any duplicate
    immediately, so this is safe to fire whether or not the main instance is
    already running. No PowerShell, no PS1 file, no Defender exposure.
    """
    # StartBoundary in the past + StartWhenAvailable fires the task at logon
    # even if the exact 5-minute mark was missed.
    trigger = (
        "\n    <TimeTrigger>"
        "\n      <Repetition>"
        "\n        <Interval>PT5M</Interval>"
        "\n        <Duration>P9999D</Duration>"
        "\n        <StopAtDurationEnd>false</StopAtDurationEnd>"
        "\n      </Repetition>"
        "\n      <StartBoundary>2020-01-01T00:00:00</StartBoundary>"
        "\n      <Enabled>true</Enabled>"
        "\n    </TimeTrigger>"
    )
    xml = _task_xml(exe_path, trigger)
    return _schtasks_import_xml("TelemetryAgentWatchdog", xml)


def _register_ui_task(ui_exe_path: str) -> bool:
    """
    Register TelemetryUI as an ONLOGON scheduled task so the tray companion
    starts automatically on every login.  Uses the same schtasks /xml method
    as the agent to stay Defender-safe.
    """
    trigger = "\n    <LogonTrigger><Enabled>true</Enabled></LogonTrigger>"
    xml = _task_xml(ui_exe_path, trigger)
    return _schtasks_import_xml("TelemetryUI", xml)


def _acquire_singleton() -> bool:
    """
    Acquire a named Windows mutex to enforce a single running instance.

    Returns True  when this process is the first instance (mutex created).
    Returns False when another instance already holds the mutex.

    The mutex is released automatically when the process exits — no explicit
    cleanup needed.  This is called at the very start of main() so watchdog-
    triggered copies exit in under a second without starting the main loop,
    making the 5-minute watchdog task safe to fire at all times.
    """
    try:
        h = ctypes.windll.kernel32.CreateMutexW(None, True, "Global\\TelemetryAgentSingleton")
        # ERROR_ALREADY_EXISTS (183) means another instance owns the mutex
        return ctypes.windll.kernel32.GetLastError() != 183
    except Exception:
        return True  # if mutex check fails, allow this instance to continue


def _register_run_key(exe_path: str) -> bool:
    """
    Add the agent to HKCU\\...\\Run as a third-layer startup guarantee.
    This fires even if both scheduled tasks are removed, giving belt-and-suspenders
    coverage for environments where Task Scheduler is restricted.
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, "TelemetryAgent", 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
        return True
    except Exception as e:
        _LOG.error("Registry Run key failed: %s", e)
        return False


def _ensure_startup_registered(exe_path: str) -> None:
    """
    Called at every agent startup (frozen mode only).

    Verifies that all three startup hooks are still in place and silently
    re-registers any that are missing.  This makes the agent self-healing:
    if a user or admin accidentally deletes the task or Run key, it is
    restored the next time the agent runs — without any manual intervention.
    """
    _si = subprocess.STARTUPINFO()
    _si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
    _si.wShowWindow = 0  # SW_HIDE

    # 1. Main scheduled task
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", "TelemetryAgent"],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=_si,
        )
        if r.returncode != 0:
            _LOG.warning("TelemetryAgent task missing — re-registering")
            _register_scheduled_task(exe_path)
    except Exception as e:
        _LOG.warning("Could not verify TelemetryAgent task: %s", e)

    # 2. Watchdog task
    try:
        r = subprocess.run(
            ["schtasks", "/query", "/tn", "TelemetryAgentWatchdog"],
            capture_output=True, timeout=10,
            creationflags=subprocess.CREATE_NO_WINDOW,
            startupinfo=_si,
        )
        if r.returncode != 0:
            _LOG.warning("TelemetryAgentWatchdog task missing — re-registering")
            _register_watchdog_task(exe_path)
    except Exception as e:
        _LOG.warning("Could not verify watchdog task: %s", e)

    # 3. Registry Run key
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_READ,
        )
        try:
            winreg.QueryValueEx(key, "TelemetryAgent")
        except FileNotFoundError:
            _LOG.warning("Registry Run key missing — re-adding")
            _register_run_key(exe_path)
        finally:
            winreg.CloseKey(key)
    except Exception as e:
        _LOG.warning("Could not verify registry Run key: %s", e)

    # 4. UI companion scheduled task (TelemetryUI)
    ui_exe = os.path.join("C:\\Program Files\\TelemetryUI", "telemetry_ui.exe")
    if os.path.exists(ui_exe):
        try:
            r = subprocess.run(
                ["schtasks", "/query", "/tn", "TelemetryUI"],
                capture_output=True, timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=_si,
            )
            if r.returncode != 0:
                _LOG.warning("TelemetryUI task missing — re-registering")
                _register_ui_task(ui_exe)
        except Exception as e:
            _LOG.warning("Could not verify TelemetryUI task: %s", e)


def install(server_url: str = None, admin_key: str = None) -> None:
    """
    Full installation routine:
      1. Create C:\\ProgramData\\TelemetryAgent and C:\\Program Files\\TelemetryAgent
      2. Resolve server URL from /agent-config (public endpoint)
      3. Register this device → server generates a per-user key (requires admin_key,
         passed as --admin-key CLI arg; NEVER written to config.json)
      4. Write config.json with only the per-user key
      5. Copy EXE to install dir (when running as frozen EXE)
      6. Register Windows Scheduled Task (logon trigger)
      7. Run connection check and report result

    Usage:
        telemetry_agent.exe --install --server-url https://host --admin-key <key>

    Security
    --------
    admin_key is used once to call POST /api/register-device, which returns a
    per-user device key.  Only that device key is stored in config.json.
    The admin key is discarded immediately after registration and never touches disk.
    """
    _LOG.info("=== Telemetry Agent Installation ===")

    # 1. Create directories
    for d in [PROGRAM_DATA, INSTALL_DIR]:
        try:
            os.makedirs(d, exist_ok=True)
            _LOG.info("  Directory ready: %s", d)
        except PermissionError:
            _LOG.error("  Permission denied creating %s — run as Administrator", d)
            sys.exit(1)

    # 2. Resolve base server URL and shared agent key from /agent-config
    #    /agent-config now requires admin credentials — pass admin_key as X-API-Key.
    base      = (server_url or _base_url()).rstrip("/")
    agent_key = ""
    try:
        headers = {"X-API-Key": admin_key} if admin_key else {}
        resp = requests.get(f"{base}/agent-config", headers=headers, timeout=10, verify=True)
        if resp.ok:
            cfg = resp.json()
            fetched = cfg.get("server_url", "").rstrip("/")
            if fetched:
                _LOG.info("  /agent-config returned server_url: %s", fetched)
                base = fetched
            agent_key = cfg.get("agent_api_key", "")
            if agent_key:
                _LOG.info("  Agent API key received from /agent-config")
        elif resp.status_code == 401:
            _LOG.warning("  /agent-config requires --admin-key; skipping key auto-fetch")
    except Exception as e:
        _LOG.warning("  Could not fetch /agent-config: %s — using %s", e, base)

    # 3. Optionally register device for a per-user key (overrides the shared key)
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
                    _LOG.info("  Device registered — per-user key issued (length %d)", len(agent_key))
            else:
                _LOG.warning("  /api/register-device returned HTTP %d", resp.status_code)
        except Exception as e:
            _LOG.warning("  Device registration failed: %s", e)
        # admin_key goes out of scope here — never written anywhere

    # 4. Write config.json  (admin key is ABSENT — only the per-user key is stored)
    config = {
        "ingest_url":     f"{base}/ingest",
        "api_key":        agent_key,   # per-user device key (scoped to this user only)
        "idle_threshold": IDLE_THRESHOLD,
        "tick_interval":  TICK_INTERVAL,
        "log_interval":   LOG_INTERVAL,
        "batch_size":     BATCH_SIZE,
    }
    try:
        with open(SYSTEM_CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=4)
        _LOG.info("  Config written: %s", SYSTEM_CONFIG_PATH)
    except Exception as e:
        _LOG.error("  Failed to write config: %s", e)

    # 4. Determine EXE path and copy if frozen
    if getattr(sys, "frozen", False):
        src      = sys.executable
        exe_dest = os.path.join(INSTALL_DIR, "telemetry_agent.exe")
        if os.path.abspath(src).lower() != os.path.abspath(exe_dest).lower():
            try:
                shutil.copy2(src, exe_dest)
                _LOG.info("  Agent copied: %s → %s", src, exe_dest)
            except Exception as e:
                _LOG.error("  Copy failed: %s — using current location", e)
                exe_dest = src
        else:
            _LOG.info("  Agent already at install location: %s", exe_dest)
    else:
        # Script mode (dev / testing)
        exe_dest = os.path.abspath(sys.argv[0])
        _LOG.info("  Script mode — scheduled task will run: %s", exe_dest)

    # 5. Register startup hooks (three layers for self-healing coverage)
    if _register_scheduled_task(exe_dest):
        _LOG.info("  Scheduled task 'TelemetryAgent' registered (ONLOGON, restart-on-failure x5)")
    else:
        _LOG.error(
            "  Scheduled task registration failed — "
            "re-run as Administrator or create the task manually"
        )

    if _register_watchdog_task(exe_dest):
        _LOG.info("  Watchdog task 'TelemetryAgentWatchdog' registered (every 5 min)")
    else:
        _LOG.warning("  Watchdog task registration failed — non-critical, agent will still start at logon")

    if _register_run_key(exe_dest):
        _LOG.info("  Registry Run key added (HKCU\\...\\Run\\TelemetryAgent)")
    else:
        _LOG.warning("  Registry Run key failed — non-critical")

    # 5b. Register TelemetryUI scheduled task if the UI exe is present
    ui_exe = os.path.join("C:\\Program Files\\TelemetryUI", "telemetry_ui.exe")
    if os.path.exists(ui_exe):
        if _register_ui_task(ui_exe):
            _LOG.info("  UI companion task 'TelemetryUI' registered (ONLOGON)")
        else:
            _LOG.warning("  UI task registration failed — run install-script from dashboard to fix")

    # 6. Connection check
    if check_connection(retries=3, delay=3):
        _LOG.info("  Server connection: OK")
    else:
        _LOG.warning(
            "  Server connection: FAILED — agent will retry when it runs normally"
        )

    # 7. Start the agent immediately in the background so data flows right now
    #    without requiring a logout/login.  The scheduled task handles future logons.
    try:
        if getattr(sys, "frozen", False):
            launch = [exe_dest]
        else:
            launch = [sys.executable, os.path.abspath(sys.argv[0])]

        subprocess.Popen(
            launch,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        _LOG.info("  Agent launched in background — data will start flowing immediately")
    except Exception as e:
        _LOG.warning("  Could not auto-start agent: %s", e)
        _LOG.warning("  Start manually: %s  or log out and back in", exe_dest)

    _LOG.info("=== Installation complete ===")
    _LOG.info("  Log file : %s", LOG_PATH)
    _LOG.info("  Config   : %s", SYSTEM_CONFIG_PATH)
    _LOG.info("  Agent    : %s", exe_dest)


def uninstall() -> None:
    """
    Clean removal of the agent from this machine:
      1. Stop and delete the TelemetryAgent scheduled task
      2. Terminate any other running agent processes
      3. Delete C:\\Program Files\\TelemetryAgent\\
      4. Delete C:\\ProgramData\\TelemetryAgent\\  (config, logs, cache, status)
      5. Delete %TEMP%\\TelemetryAgent\\            (rolling logs)
      6. Delete %TEMP%\\telemetry_backup\\<user>\\   (offline event batches)

    Cloud data is NOT touched — only local files are removed.
    """
    _LOG.info("=== Telemetry Agent Uninstall ===")

    # 1. Remove all startup hooks
    for task in ("TelemetryAgent", "TelemetryAgentWatchdog"):
        try:
            subprocess.call(
                ["schtasks", "/delete", "/tn", task, "/f"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            _LOG.info("  Scheduled task removed: %s", task)
        except Exception as e:
            _LOG.warning("  Could not remove task %s: %s", task, e)

    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Run",
            0, winreg.KEY_SET_VALUE,
        )
        try:
            winreg.DeleteValue(key, "TelemetryAgent")
            _LOG.info("  Registry Run key removed")
        except FileNotFoundError:
            pass
        winreg.CloseKey(key)
    except Exception as e:
        _LOG.warning("  Could not remove registry Run key: %s", e)

    # 2. Kill other running agent processes (not self) and wait for them to exit
    procs_killed = []
    try:
        for proc in psutil.process_iter(["pid", "name"]):
            if proc.info["name"] == "telemetry_agent.exe" and proc.pid != os.getpid():
                try:
                    proc.terminate()
                    procs_killed.append(proc)
                    _LOG.info("  Terminated PID %d", proc.pid)
                except Exception:
                    pass
    except Exception as e:
        _LOG.warning("  Could not enumerate processes: %s", e)

    for proc in procs_killed:
        try:
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    if procs_killed:
        time.sleep(2)   # let Windows release file locks after process death

    # 3–6. Delete local directories (including _MEI* PyInstaller extraction dirs)
    username = getpass.getuser()
    paths_to_remove = [
        INSTALL_DIR,
        os.path.join(tempfile.gettempdir(), "TelemetryAgent"),
        os.path.join(tempfile.gettempdir(), "telemetry_backup", username),
    ]
    # Clean up PyInstaller extraction subdirs before removing PROGRAM_DATA root
    for mei_dir in glob.glob(os.path.join(PROGRAM_DATA, "_MEI*")):
        try:
            shutil.rmtree(mei_dir, ignore_errors=True)
            _LOG.info("  Removed extraction dir: %s", mei_dir)
        except Exception as e:
            _LOG.warning("  Could not remove %s: %s", mei_dir, e)
    paths_to_remove.append(PROGRAM_DATA)

    for path in paths_to_remove:
        try:
            shutil.rmtree(path, ignore_errors=True)
            _LOG.info("  Removed: %s", path)
        except Exception as e:
            _LOG.warning("  Could not remove %s: %s", path, e)

    _LOG.info("=== Uninstall complete — cloud data is not affected ===")
    print()
    print("=" * 50)
    print("  Telemetry Agent uninstalled successfully.")
    print("  All local files and scheduled tasks removed.")
    print("  Cloud data is not affected.")
    print("=" * 50)
    print()


# ── Main loop ───────────────────────────────────────────────────────────────────

def main():
    # ── CLI args ─────────────────────────────────────────────────────────────
    parser = argparse.ArgumentParser(description="Telemetry Agent")
    parser.add_argument(
        "--install", action="store_true",
        help="Install agent: create dirs, write config, register scheduled task",
    )
    parser.add_argument(
        "--server-url", metavar="URL", default=None,
        help="Server base URL for install (e.g. https://host:8000)",
    )
    parser.add_argument(
        "--admin-key", metavar="KEY", default=None,
        help="Admin API key — used ONCE to register this device; never stored on disk",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove agent: stop scheduled task, delete files and config",
    )
    args = parser.parse_args()

    _setup_logging()

    if args.install:
        install(server_url=args.server_url, admin_key=args.admin_key)
        return

    if args.uninstall:
        uninstall()
        return

    # ── Single-instance guard ─────────────────────────────────────────────────
    # The watchdog task fires every 5 minutes and starts this EXE directly.
    # If the main instance is already running, _acquire_singleton returns False
    # and the watchdog-spawned copy exits here in under a second — no main loop,
    # no network calls, no logging.  Only the first instance passes this gate.
    if getattr(sys, "frozen", False) and not _acquire_singleton():
        sys.exit(0)

    # ── Auto-update check (frozen EXE only; exits+restarts if newer available) ─
    check_for_update()

    # ── Self-heal startup registrations on every run ──────────────────────────
    # Silently re-registers the scheduled task, watchdog, and registry Run key
    # if any of them have been removed since the last run. This ensures the
    # agent survives accidental task deletion or profile resets without any
    # manual re-installation.
    if getattr(sys, "frozen", False):
        _ensure_startup_registered(sys.executable)

    # ── Normal run ────────────────────────────────────────────────────────────
    user_info = get_user_info()
    username  = user_info["username"]
    hostname  = user_info["hostname"]
    state     = TelemetryState()

    event_buffer:       list = []
    elapsed_since_log:  int  = 0
    elapsed_since_flush: int = 0
    last_event_app:     str  = None  # tracks app at last event boundary for change detection

    _LOG.info("Agent started v%s — %s @ %s", AGENT_VERSION, username, hostname)
    _LOG.info(
        "Tick: %ds | Event: every %ds | Flush: every %ds or on app-switch | URL: %s",
        TICK_INTERVAL, LOG_INTERVAL, FLUSH_INTERVAL, INGEST_URL,
    )
    _LOG.info("Backup dir: %s", _backup_dir(username))
    _LOG.info("Log file  : %s", LOG_PATH)

    # Connection check — warns but never blocks the agent from starting
    if not check_connection(retries=3, delay=5):
        _LOG.warning("Startup connection check failed — will continue and retry on each batch")

    # Replay any batches that were saved offline during a previous run
    flush_backup(username, hostname)

    # ── Startup gap: capture time the machine was asleep / agent was stopped ────
    # If last_seen.json shows the agent last ran > 2 min ago, inject a synthetic
    # Screen Off event so the dashboard reflects the full offline period.
    gap_events = _startup_gap_events()
    if gap_events:
        ok = flush_batch(username, hostname, gap_events)
        if not ok:
            save_to_backup(username, hostname, gap_events)

    _last_tick_wall = time.time()   # wall-clock anchor for sleep-resume detection

    try:
        while True:
            # ── Sleep/resume gap detection ───────────────────────────────────────
            # When Windows suspends the machine, this process is frozen.
            # time.sleep() returns immediately after wake, but the wall clock
            # has jumped forward by the sleep duration.  Detect the jump and
            # inject a Screen Off event so the gap shows in the dashboard.
            _now_wall   = time.time()
            _tick_delta = _now_wall - _last_tick_wall
            _last_tick_wall = _now_wall
            if _tick_delta > TICK_INTERVAL * 3:          # >15 s gap → system slept
                _sleep_gap = int(_tick_delta)
                _gap_start = datetime.fromtimestamp(
                    _now_wall - _tick_delta, tz=timezone.utc
                ).isoformat()
                event_buffer.append({
                    "app":       "Screen Off",
                    "domain":    "",
                    "active":    False,
                    "locked":    True,
                    "duration":  _sleep_gap,
                    "timestamp": _gap_start,
                })
                _LOG.info("Sleep/resume gap: %ds of screen-off time captured", _sleep_gap)
                # Flush gap event immediately so it is not lost on a subsequent crash
                _compressed = aggregate_events(event_buffer)
                if flush_batch(username, hostname, _compressed):
                    event_buffer.clear()
                    flush_backup(username, hostname)
                else:
                    save_to_backup(username, hostname, _compressed)
                    event_buffer.clear()
                # Reset both elapsed counters.  The gap already accounts for the
                # missing time, so the next LOG_INTERVAL event must count from zero.
                # Without this reset, elapsed_since_flush was double-incremented
                # (once by += _sleep_gap here, once by += TICK_INTERVAL below),
                # and elapsed_since_log carried over pre-sleep partial time causing
                # a spurious active event immediately after every resume.
                elapsed_since_log   = 0
                elapsed_since_flush = 0
                time.sleep(TICK_INTERVAL)
                continue  # skip normal tick processing for this iteration

            # ── Sample foreground state ──────────────────────────────────────────
            is_locked = is_workstation_locked()
            idle_secs = get_idle_seconds()
            # Active only when screen is unlocked AND user has recent input
            is_active = not is_locked and (idle_secs < IDLE_THRESHOLD)
            app_name, hwnd = get_foreground_app()
            domain = extract_domain(hwnd, app_name) if (hwnd and not is_locked) else ""

            state.update(app_name)
            elapsed_since_log   += TICK_INTERVAL
            elapsed_since_flush += TICK_INTERVAL

            # ── Write real-time status (every tick = ~5 s lag for UI) ────────────
            _write_status_file(app_name, is_active, is_locked, idle_secs)

            # ── Every LOG_INTERVAL: build one raw event ──────────────────────────
            if elapsed_since_log >= LOG_INTERVAL:
                rotate_logs()
                now = datetime.now(timezone.utc).isoformat()

                # Local log — human-readable, useful for dashboard.html offline view
                log_entry = {
                    "timestamp":        now,
                    "hostname":         hostname,
                    "username":         username,
                    "current_app":      state.current_app,
                    "domain":           domain or "N/A",
                    "idle_seconds":     idle_secs,
                    "active":           is_active,
                    "locked":           is_locked,
                    "session_duration": state.session_duration(),
                }
                print(json.dumps(log_entry))
                try:
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(json.dumps(log_entry) + "\n")
                except Exception as e:
                    print(f"  [warn] Log write failed: {e}")

                # Raw event for the analytics server (minimal schema)
                event_buffer.append({
                    "app":       state.current_app,
                    "domain":    domain,
                    "active":    is_active,
                    "locked":    is_locked,
                    "duration":  LOG_INTERVAL,
                    "timestamp": now,
                })
                # Persist timestamp so next startup can detect any gap (sleep/shutdown)
                _save_last_seen(now, state.current_app)

                # ── Update local cache (UI reads this when server unreachable) ────
                _accumulate(state.current_app, domain, is_active, is_locked, LOG_INTERVAL)
                _write_cache(username, hostname)

                # ── Flush triggers ───────────────────────────────────────────────
                # 1. Time-based: FLUSH_INTERVAL seconds have elapsed since last flush
                # 2. App switch: the foreground app changed since the previous event
                # 3. Safety cap: buffer hit BATCH_SIZE without a time/app-switch flush
                prev_app       = last_event_app
                last_event_app = state.current_app
                app_switched   = prev_app is not None and state.current_app != prev_app
                time_to_flush  = elapsed_since_flush >= FLUSH_INTERVAL
                cap_reached    = len(event_buffer) >= BATCH_SIZE

                if (time_to_flush or app_switched or cap_reached) and event_buffer:
                    compressed = aggregate_events(event_buffer)
                    success    = flush_batch(username, hostname, compressed)
                    elapsed_since_flush = 0
                    if success:
                        event_buffer.clear()
                        # Server reachable — drain any batches saved while offline
                        flush_backup(username, hostname)
                    else:
                        # Server unreachable — persist compressed batch to disk
                        save_to_backup(username, hostname, compressed)
                        event_buffer.clear()

                elapsed_since_log = 0

            time.sleep(TICK_INTERVAL)

    except KeyboardInterrupt:
        print("\nStopping — flushing remaining buffer...")
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            ok = flush_batch(username, hostname, compressed)
            if not ok:
                save_to_backup(username, hostname, compressed)
                print(f"  [backup] {len(event_buffer)} events saved to disk — will be sent on next start")
        print("Agent stopped.")
    except Exception as e:
        print(f"Agent crash: {e}")
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            ok = flush_batch(username, hostname, compressed)
            if not ok:
                save_to_backup(username, hostname, compressed)


if __name__ == "__main__":
    main()
