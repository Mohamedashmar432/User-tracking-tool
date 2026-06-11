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
    2. agent.config.json next to this script / sys._MEIPASS (dev / frozen bundle)
    """
    candidates = [SYSTEM_CONFIG_PATH]
    # In a frozen --onedir build, __file__ == sys.executable so the dirname is the
    # EXE directory, but bundled data files land in sys._MEIPASS (_internal/).
    # In dev/script mode __file__ is the .py file, so dirname is the repo root.
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(os.path.join(sys._MEIPASS, "agent.config.json"))
    else:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.config.json"))
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

# Companion UI install path — same convention as _register_ui_task() in install().
# Centralised here so the UI watchdog and the install routine cannot drift.
# Resolved dynamically: frozen → canonical install dir, dev → same dir as this script.
def _ui_exe_path() -> str:
    if getattr(sys, "frozen", False):
        return r"C:\Program Files\TelemetryUI\telemetry_ui.exe"
    script_dir = os.path.dirname(os.path.abspath(__file__))
    ui_py = os.path.join(script_dir, "telemetry_ui.py")
    if os.path.exists(ui_py):
        return ui_py
    return r"C:\Program Files\TelemetryUI\telemetry_ui.exe"

UI_EXE_PATH: str = _ui_exe_path()
# How often the in-process UI watchdog polls (real wall-clock seconds).
# Matches the TelemetryAgentWatchdog scheduled-task cadence (5 min) so we
# never have two recovery mechanisms racing each other.
UI_WATCHDOG_INTERVAL_SEC = 5 * 60

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


class StateEngine:
    """
    State-machine that tracks ACTIVE / IDLE / LOCKED using
    tick_elapsed accumulators exclusively.  Never uses time.monotonic()
    directly after construction.

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
        """
        Advance the engine by one tick.
        tick_elapsed: actual seconds elapsed this tick (from time.monotonic()).
        """
        tick_elapsed = max(0.0, min(tick_elapsed, self._max_tick))
        self._session_elapsed_seconds += tick_elapsed   # accumulate real elapsed

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
        """Gap time is classified as LOCKED and advances session_elapsed_seconds."""
        gap_seconds = max(0.0, gap_seconds)
        self._locked_seconds += gap_seconds
        self._session_elapsed_seconds += gap_seconds
        self._state = ActivityState.LOCKED   # state is LOCKED during a sleep gap

    def enforce_invariant(self) -> None:
        """Hard cap: active + idle + locked <= session elapsed."""
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
        """Return counters safe for writing to cache.json."""
        snapshot_elapsed = self._session_elapsed_seconds   # single snapshot
        self.enforce_invariant()
        return {
            "active_seconds":  int(self._active_seconds),
            "idle_seconds":    int(self._idle_seconds),
            "locked_seconds":  int(self._locked_seconds),
            "session_elapsed": int(snapshot_elapsed),
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


def _is_admin() -> bool:
    """Return True if the current process has administrator privileges."""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _relaunch_elevated(argv: list) -> None:
    """Re-launch this process with UAC elevation and exit the current process."""
    exe = sys.executable
    params = " ".join(f'"{a}"' for a in argv[1:])
    ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 1)
    sys.exit(0)


def _write_crash_log(exc: BaseException) -> None:
    """Write an unhandled exception to %TEMP%\\TelemetryAgent\\crash.log."""
    import traceback
    try:
        crash_dir = os.path.join(tempfile.gettempdir(), "TelemetryAgent")
        os.makedirs(crash_dir, exist_ok=True)
        crash_path = os.path.join(crash_dir, "crash.log")
        with open(crash_path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"CRASH at {datetime.now().isoformat()}\n")
            traceback.print_exc(file=f)
    except Exception:
        pass


def _setup_logging() -> None:
    """
    Configure _LOG with:
      - FileHandler  -> C:\\ProgramData\\TelemetryAgent\\agent.log  (primary)
      - FileHandler  -> %TEMP%\\TelemetryAgent\\agent.log            (fallback)
      - StreamHandler -> stdout  (only when a console is attached)
    Safe to call multiple times (guards against duplicate handlers).
    """
    if _LOG.handlers:
        return  # already configured

    _LOG.setLevel(logging.INFO)
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)-8s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # File handler — primary: ProgramData; fallback: %TEMP%
    log_opened = False
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setFormatter(fmt)
        _LOG.addHandler(fh)
        log_opened = True
    except Exception as e:
        print(f"[log] Cannot create log file {LOG_PATH}: {e}")

    if not log_opened:
        try:
            fallback_log = os.path.join(tempfile.gettempdir(), "TelemetryAgent", "agent.log")
            os.makedirs(os.path.dirname(fallback_log), exist_ok=True)
            fh2 = logging.FileHandler(fallback_log, encoding="utf-8")
            fh2.setFormatter(fmt)
            _LOG.addHandler(fh2)
            _LOG.warning("Using fallback log (ProgramData unavailable): %s", fallback_log)
        except Exception:
            pass  # truly no log available — noconsole mode, nothing we can do

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

    Cap: a single startup-gap event represents at most 86400 s (24 h).
    Longer gaps (multi-day shutdowns) are capped so one partition never
    accumulates more than one day of locked time from a single event.
    """
    try:
        with open(LAST_SEEN_PATH, encoding="utf-8") as f:
            data = json.load(f)
        last_ts_str = data.get("timestamp", "")
        if not last_ts_str:
            return []
        last_ts = datetime.fromisoformat(last_ts_str)
        if last_ts.tzinfo is None:
            last_ts = last_ts.replace(tzinfo=timezone.utc)
        now     = datetime.now(timezone.utc)
        gap_sec = int((now - last_ts).total_seconds())
        if gap_sec < LOG_INTERVAL * 2:
            return []
        # Cap at 24 h so one event never inflates a single day beyond 86400 s
        gap_sec = min(gap_sec, 86_400)
        _LOG.info("Startup gap: %ds since last event — inserting screen-off", gap_sec)
        return [{
            "app":       "Screen Off",
            "domain":    "",
            "active":    False,
            "locked":    True,
            "duration":  gap_sec,
            "timestamp": last_ts_str,
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

    Every error path now logs to BOTH stdout (verbose) and _LOG (file) with
    the same message.  Previously the `print()` calls only went to stdout,
    which means in noconsole/frozen-Windows-service mode the only diagnostic
    trail for a recurring "why is data not arriving?" complaint was the
    single agent.log line "Server unreachable".  Now each failure mode gets
    a distinct message that names the exception class and includes the
    server URL + event count so an operator can grep agent.log for the
    exact pattern matching the production problem.
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
            msg = f"  ->Batch sent: {data.get('accepted')}/{data.get('total')} events accepted"
            print(msg)
            _LOG.debug("POST %s accepted=%s total=%s", INGEST_URL,
                       data.get("accepted"), data.get("total"))
            return True
        # Server replied but didn't accept.  Body is truncated to 200 chars
        # so a runaway error page doesn't fill the log.
        body_preview = (resp.text or "")[:200]
        msg = f"  ->Server rejected batch [{resp.status_code}]: {body_preview}"
        print(msg)
        _LOG.error("POST %s returned HTTP %d for %d events — body[:200]=%r",
                   INGEST_URL, resp.status_code, len(batch), body_preview)
        return False
    except requests.exceptions.ConnectionError as e:
        msg = f"  ->Server unreachable ({INGEST_URL}). Events buffered locally."
        print(msg)
        _LOG.error("ConnectionError to %s (batch=%d events, user=%s): %s",
                   INGEST_URL, len(batch), user, e)
        return False
    except requests.exceptions.Timeout as e:
        msg = f"  ->Server timeout after 10s ({INGEST_URL}). Events buffered locally."
        print(msg)
        _LOG.error("Timeout from %s (batch=%d events, user=%s): %s",
                   INGEST_URL, len(batch), user, e)
        return False
    except requests.exceptions.SSLError as e:
        # Distinct message: this is the most common reason agents in locked-down
        # corporate environments (custom CA, MITM proxy) fail silently.
        msg = f"  ->SSL error contacting {INGEST_URL}: {e}"
        print(msg)
        _LOG.error("SSLError to %s — check CA bundle / corporate proxy: %s",
                   INGEST_URL, e)
        return False
    except requests.exceptions.RequestException as e:
        # Catches any other requests-level error (TooManyRedirects, InvalidURL, etc.)
        msg = f"  ->HTTP error: {e}"
        print(msg)
        _LOG.error("RequestException to %s (batch=%d): %s",
                   INGEST_URL, len(batch), e)
        return False
    except Exception as e:
        # Last-resort catch — should never fire but if it does the agent must
        # NOT crash (it would lose its watchdog relationship).  Log the full
        # class name so future grepping is precise.
        msg = f"  ->Flush error ({type(e).__name__}): {e}"
        print(msg)
        _LOG.exception("Unexpected flush error to %s (batch=%d):",
                        INGEST_URL, len(batch))
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

# Shared file the UI reads to show/hide the "Update Available" button.
_UPDATE_STATUS_PATH = os.path.join(PROGRAM_DATA, "update-status.json")

def _write_update_status(server_version: str) -> None:
    try:
        os.makedirs(PROGRAM_DATA, exist_ok=True)
        with open(_UPDATE_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump({"update_available": True, "server_version": server_version}, f)
    except Exception:
        pass

def _clear_update_status() -> None:
    try:
        if os.path.exists(_UPDATE_STATUS_PATH):
            os.remove(_UPDATE_STATUS_PATH)
    except Exception:
        pass


def _ver(v: str) -> tuple:
    """
    Parse a semver-ish version string into a comparable tuple.

    Robust against:
      - non-numeric pre-release suffixes ('3.1.0-beta' -> (3, 1, 0))
      - missing components  ('3'  -> (3,))
      - whitespace, empty string, None, garbage ('  ', '??', None -> (0,))

    The previous implementation did `int(x)` on every component, which raised
    on '3.1.0-beta' and silently returned (0,).  That made (0,) < (1, 0) true
    and falsely triggered auto-updates against misbehaving servers.
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
        try: os.remove(tmp_zip)
        except OSError: pass
        return

    # Magic-byte sanity check — ensure the download is actually a ZIP, not an
    # HTML error page that would silently replace the EXE bundle with garbage.
    # ZIP files start with the bytes b'PK\x03\x04' (PKZip local file header).
    try:
        with open(tmp_zip, "rb") as f:
            magic = f.read(4)
        if magic != b"PK\x03\x04":
            _LOG.error(
                "Auto-update: downloaded file is not a ZIP (magic=%r) — aborting. "
                "Check that agent_zip_download_url on the server points to a real ZIP.",
                magic,
            )
            try: os.remove(tmp_zip)
            except OSError: pass
            return
    except Exception as e:
        _LOG.error("Auto-update: could not read downloaded file: %s", e)
        return

    _LOG.info("Auto-update: download complete (%d bytes), preparing updater",
              os.path.getsize(tmp_zip))

    # Hidden PowerShell updater: waits for this process to exit, kills any other
    # agent instance so files are not locked, extracts the ZIP, then re-launches.
    ps_lines = [
        "Start-Sleep -Seconds 3",
        "# Kill other agent processes FIRST so files are not locked during extraction",
        "Get-Process -Name 'telemetry_agent' -ErrorAction SilentlyContinue | Stop-Process -Force",
        "Start-Sleep -Seconds 1",
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
    writes update-status.json so the UI can show an "Update Available" button.
    Skips silently in dev mode (no sys.frozen) or on any network error.
    """
    if not getattr(sys, "frozen", False):
        return   # dev mode — never self-replace

    base        = _base_url()
    health_url  = f"{base}/api/health"

    try:
        resp = requests.get(health_url, timeout=10, verify=True)
        if not resp.ok:
            return
        data            = resp.json()
        server_version  = data.get("version", "0")
    except Exception as e:
        _LOG.debug("Update check skipped: %s", e)
        return

    if _ver(server_version) > _ver(AGENT_VERSION):
        _LOG.info("Update available: server v%s, running v%s", server_version, AGENT_VERSION)
        _write_update_status(server_version)
    else:
        _clear_update_status()
        _LOG.debug("Up to date (v%s)", AGENT_VERSION)


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


def _ensure_ui_running(ui_path: str) -> None:
    """
    Self-healing watchdog for the companion UI tray app.

    The TelemetryUI scheduled task is registered at install time and should
    start the UI at every logon.  In practice the user can kill the tray
    process (right-click → Exit) and the scheduled task only re-runs at
    the next logon.  This watchdog lets the agent bring the UI back up
    without requiring a logout / login cycle.

    Called from the agent main loop every ~5 minutes (same cadence as the
    TelemetryAgentWatchdog task) and once at agent startup.  Silent on
    failure — the worst case is the user has to launch the UI manually
    from the Start menu / a logon, which is the same as before this fix.

    Handles both frozen EXE and dev (python script) modes transparently.
    """
    if not ui_path or not os.path.exists(ui_path):
        return  # UI not installed — nothing to do
    try:
        if _is_ui_running():
            return

        # Determine how to launch: frozen → EXE directly, dev → python script
        if getattr(sys, "frozen", False):
            args = [ui_path]
        else:
            args = [sys.executable, ui_path]

        subprocess.Popen(
            args,
            creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NO_WINDOW,
            close_fds=True,
        )
        _LOG.info(
            "UI watchdog: %s was not running — started %s",
            os.path.basename(ui_path), ui_path,
        )
    except Exception as e:
        _LOG.warning("UI watchdog: failed to start %s: %s", ui_path, e)


def _is_ui_running() -> bool:
    """Check whether the telemetry UI process is currently running."""
    frozen = getattr(sys, "frozen", False)
    try:
        if frozen:
            si = subprocess.STARTUPINFO()
            si.dwFlags    |= subprocess.STARTF_USESHOWWINDOW
            si.wShowWindow = 0
            r = subprocess.run(
                ["tasklist", "/fi", "imagename eq telemetry_ui.exe",
                 "/fo", "csv", "/nh"],
                capture_output=True, text=True, timeout=8,
                creationflags=subprocess.CREATE_NO_WINDOW,
                startupinfo=si,
            )
            if "telemetry_ui.exe" in r.stdout.lower():
                return True
        else:
            for proc in psutil.process_iter(["name", "cmdline"]):
                try:
                    if proc.info.get("name", "").lower() != "python.exe":
                        continue
                    cmdline = proc.info.get("cmdline") or []
                    if any("telemetry_ui.py" in arg for arg in cmdline):
                        return True
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    continue
        return False
    except Exception:
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


def install(server_url: str = None, admin_key: str = None, api_key: str = None) -> None:
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

    # 2. Resolve base server URL and shared agent key.
    #    If --api-key was supplied (injected by the server's install script), use it
    #    directly and skip the /agent-config round-trip entirely.
    #    Otherwise try /agent-config with an optional --admin-key for auth.
    base      = (server_url or _base_url()).rstrip("/")
    agent_key = api_key or ""
    if not agent_key:
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
    else:
        _LOG.info("  Agent API key supplied via --api-key")

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

    # Update module-level globals immediately so check_connection() and any
    # logging in this same process uses the prod URL, not the import-time default.
    global INGEST_URL, AGENT_API_KEY
    INGEST_URL = config["ingest_url"]
    if agent_key:
        AGENT_API_KEY = agent_key

    # 4. Determine EXE path and copy if frozen
    #    PyInstaller --onedir bundles the EXE + _internal/ as a directory.
    #    We must copy the whole directory tree, not just the EXE — the EXE
    #    cannot start without _internal/ (Python DLLs, .pyd modules, etc.).
    if getattr(sys, "frozen", False):
        src      = sys.executable
        src_dir  = os.path.dirname(src)   # onedir folder: EXE + _internal/
        exe_dest = os.path.join(INSTALL_DIR, "telemetry_agent.exe")
        if os.path.abspath(src_dir).lower() != os.path.abspath(INSTALL_DIR).lower():
            try:
                # copytree requires the destination to not exist — remove then copy
                shutil.rmtree(INSTALL_DIR, ignore_errors=True)
                shutil.copytree(src_dir, INSTALL_DIR)
                _LOG.info("  Agent bundle copied: %s → %s", src_dir, INSTALL_DIR)
            except Exception as e:
                _LOG.error("  Bundle copy failed: %s — agent will run from: %s", e, src_dir)
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
        "--api-key", metavar="KEY", default=None,
        help="Agent ingest API key — injected by the server install script; stored in config.json",
    )
    parser.add_argument(
        "--uninstall", action="store_true",
        help="Remove agent: stop scheduled task, delete files and config",
    )
    args = parser.parse_args()

    # Catch all unhandled exceptions and write them to %TEMP%\TelemetryAgent\crash.log
    # so failures in noconsole/frozen mode leave a diagnostic trail.
    def _excepthook(exc_type, exc_value, exc_tb):
        _write_crash_log(exc_value)
        sys.__excepthook__(exc_type, exc_value, exc_tb)
    sys.excepthook = _excepthook

    # Install/uninstall require Administrator — auto-elevate via UAC if needed.
    if (args.install or args.uninstall) and not _is_admin():
        _relaunch_elevated(sys.argv)
        return

    _setup_logging()

    if args.install:
        install(server_url=args.server_url, admin_key=args.admin_key, api_key=args.api_key)
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

    # One-shot UI watchdog at startup — covered in detail by the periodic
    # block below.  The TelemetryUI scheduled task only re-runs at logon;
    # running the watchdog here lets the UI come back the very next tick
    # when the user has killed the tray mid-session.
    _ensure_ui_running(_ui_exe_path())

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
    _last_tick_mono = time.monotonic()          # monotonic anchor for real elapsed
    state_eng = StateEngine(IDLE_THRESHOLD)    # activity state machine

    # ── Sleep gap detection thresholds ──────────────────────────────────────────
    # A "sleep gap" is any wall-clock jump > 3 ticks (default: 3*5=15 s) that
    # could be the machine coming back from sleep/hibernate.  A single missed
    # tick is a normal scheduler hiccup, not a sleep cycle.  Multi-day gaps
    # are capped at 24 h (86400 s) to match the startup-gap cap so a single
    # partition can never be inflated by a long shutdown.
    SLEEP_GAP_MIN_SEC  = TICK_INTERVAL * 3
    SLEEP_GAP_MAX_SEC  = 86_400

    # ── UI watchdog counter ─────────────────────────────────────────────────
    # Increment once per tick.  When the counter reaches _UI_CHECK_THRESHOLD
    # ticks (== UI_WATCHDOG_INTERVAL_SEC real seconds) we call
    # _ensure_ui_running() and reset.  This is the periodic companion to the
    # one-shot startup call above — together they form the in-process recovery
    # path that catches a tray process killed mid-session.
    _ui_check_counter    = 0
    _UI_CHECK_THRESHOLD  = max(1, UI_WATCHDOG_INTERVAL_SEC // TICK_INTERVAL)

    # ── Version check counter ──────────────────────────────────────────────────
    # Fire check_for_update() every ~30 minutes so the agent picks up a server
    # version bump without requiring a process restart.  The old code only
    # checked at startup, so a server upgrade mid-session was never detected.
    _update_check_counter    = 0
    _UPDATE_CHECK_THRESHOLD  = max(1, 1800 // TICK_INTERVAL)

    try:
        while True:
            # ── Monotonic elapsed for this tick ──────────────────────────────────
            _mono_now       = time.monotonic()
            _tick_elapsed   = _mono_now - _last_tick_mono
            _last_tick_mono = _mono_now

            # ── Wall-clock anchor — ONLY for sleep/resume gap detection ──────────
            # time.monotonic() is frozen during suspend; time.time() jumps on resume.
            # Negative deltas (clock skew backwards) are clamped to 0 — they should
            # never be classified as a "gap" but the previous code would log a
            # confusing "0s sleep gap captured" message.
            _wall_now   = time.time()
            _wall_delta = max(0.0, _wall_now - _last_tick_wall)
            _last_tick_wall = _wall_now

            if _wall_delta > SLEEP_GAP_MIN_SEC:
                # Cap the gap so a multi-day outage cannot inflate a single day
                # to more than 86400 s of locked time.  The uncapped wall_delta
                # is logged for diagnostics; the stored event uses the capped value.
                _raw_gap    = int(_wall_delta)
                _sleep_gap  = min(_raw_gap, SLEEP_GAP_MAX_SEC)
                _gap_start  = datetime.fromtimestamp(
                    _wall_now - _wall_delta, tz=timezone.utc
                ).isoformat()
                event_buffer.append({
                    "app":       "Screen Off",
                    "domain":    "",
                    "active":    False,
                    "locked":    True,
                    "duration":  _sleep_gap,
                    "timestamp": _gap_start,
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
                _compressed = aggregate_events(event_buffer)
                if flush_batch(username, hostname, _compressed):
                    event_buffer.clear()
                    flush_backup(username, hostname)
                else:
                    save_to_backup(username, hostname, _compressed)
                    event_buffer.clear()
                elapsed_since_log   = 0
                elapsed_since_flush = 0
                _last_tick_mono = time.monotonic()
                time.sleep(TICK_INTERVAL)
                continue

            # ── Sample foreground state ──────────────────────────────────────────
            is_locked = is_workstation_locked()
            idle_secs = get_idle_seconds()
            # Active only when screen is unlocked AND user has recent input
            is_active = not is_locked and (idle_secs < IDLE_THRESHOLD)
            app_name, hwnd = get_foreground_app()
            domain = extract_domain(hwnd, app_name) if (hwnd and not is_locked) else ""

            state.update(app_name)
            elapsed_since_log   += _tick_elapsed
            elapsed_since_flush += _tick_elapsed

            # Advance state machine with real elapsed
            current_state = state_eng.tick(idle_secs, is_locked, _tick_elapsed)
            is_active = current_state == ActivityState.ACTIVE

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
                _event_duration = max(1, min(int(elapsed_since_log), LOG_INTERVAL * 3))
                event_buffer.append({
                    "app":       state.current_app,
                    "domain":    domain,
                    "active":    is_active,
                    "locked":    is_locked,
                    "duration":  _event_duration,
                    "timestamp": now,
                })
                # Persist timestamp so next startup can detect any gap (sleep/shutdown)
                _save_last_seen(now, state.current_app)

                # ── Update local cache (UI reads this when server unreachable) ────
                _accumulate(state.current_app, domain, is_active, is_locked, _event_duration)
                state_eng.enforce_invariant()
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

            # ── Periodic UI watchdog tick ─────────────────────────────────────────
            # Increment once per tick; fire _ensure_ui_running() when the
            # counter hits _UI_CHECK_THRESHOLD.  This is the in-process
            # companion to the TelemetryAgentWatchdog scheduled task — it
            # catches a tray process the user has killed mid-session without
            # requiring a logout / logon cycle.
            _ui_check_counter += 1
            if _ui_check_counter >= _UI_CHECK_THRESHOLD:
                _ui_check_counter = 0
                _ensure_ui_running(_ui_exe_path())

            # ── Periodic version check ────────────────────────────────────────────
            # Re-check the server version every ~30 minutes so a server upgrade
            # mid-session is detected.  check_for_update() is lightweight — it
            # just writes update-status.json so the UI can show the button.
            _update_check_counter += 1
            if _update_check_counter >= _UPDATE_CHECK_THRESHOLD:
                _update_check_counter = 0
                check_for_update()

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
