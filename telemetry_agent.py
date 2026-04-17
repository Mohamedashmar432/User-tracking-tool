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
Cap    : MAX_BACKUP_EVENTS (100) total events on disk — oldest evicted first
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
TICK_INTERVAL     = _cfg.get("tick_interval",    5)   # seconds
LOG_INTERVAL      = _cfg.get("log_interval",    60)   # seconds — one event per minute
BATCH_SIZE        = _cfg.get("batch_size",      10)   # safety cap: flush if buffer reaches this
FLUSH_INTERVAL    = _cfg.get("flush_interval", 120)   # seconds between server pushes (primary trigger)
MAX_BACKUP_EVENTS = 100   # max events persisted to disk when server is unreachable

LOG_FILE      = "logs.txt"
MAX_LOG_SIZE  = 10 * 1024 * 1024   # 10 MB

# Resolution order: env var → config file → default
INGEST_URL = os.getenv("INGEST_URL") or _cfg.get("ingest_url", "http://localhost:8000/ingest")
AGENT_API_KEY = os.getenv("AGENT_API_KEY") or os.getenv("API_KEY") or _cfg.get("api_key", "")

BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}


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
      - FileHandler  → C:\\ProgramData\\TelemetryAgent\\agent.log  (always)
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
    os.makedirs(path, exist_ok=True)
    return path


def _backup_files(username: str) -> list:
    """Sorted list of backup file paths, oldest first."""
    return sorted(glob.glob(os.path.join(_backup_dir(username), "batch_*.json")))


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
        print(f"  [backup] {len(events)} events saved offline → {fpath}")
    except Exception as e:
        print(f"  [backup] Disk write failed: {e}")


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
    try:
        resp = requests.post(
            INGEST_URL,
            json={"user": user, "device": device, "events": batch},
            headers={"X-API-Key": AGENT_API_KEY},
            timeout=10,
        )
        if resp.status_code in (200, 202):
            data = resp.json()
            print(f"  → Batch sent: {data.get('accepted')}/{data.get('total')} events accepted")
            return True
        print(f"  → Server rejected batch [{resp.status_code}]: {resp.text[:200]}")
        return False
    except requests.exceptions.ConnectionError:
        print(f"  → Server unreachable ({INGEST_URL}). Events buffered locally.")
        return False
    except Exception as e:
        print(f"  → Flush error: {e}")
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
            resp = requests.get(health_url, timeout=10)
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


def _register_scheduled_task(exe_path: str) -> bool:
    """
    Create (or replace) a Windows Scheduled Task named TelemetryAgent
    that launches the agent at every user logon, silently.
    Requires the calling process to have sufficient privileges.
    """
    cmd = [
        "schtasks", "/create",
        "/tn", "TelemetryAgent",
        "/tr", f'"{exe_path}"',
        "/sc", "ONLOGON",
        "/rl", "HIGHEST",
        "/f",                  # overwrite if already exists
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0:
            return True
        _LOG.debug("schtasks stderr: %s", result.stderr.strip())
        return False
    except Exception as e:
        _LOG.error("schtasks failed: %s", e)
        return False


def install(server_url: str = None) -> None:
    """
    Full installation routine:
      1. Create C:\\ProgramData\\TelemetryAgent and C:\\Program Files\\TelemetryAgent
      2. Resolve server URL (arg → /agent-config fetch → INGEST_URL fallback)
      3. Write config.json to ProgramData
      4. Copy EXE to install dir (when running as frozen EXE)
      5. Register Windows Scheduled Task (logon trigger)
      6. Run connection check and report result

    Usage:
        telemetry_agent.exe --install --server-url http://your-server:8000
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

    # 2. Resolve base server URL and fetch agent API key from /agent-config
    base      = (server_url or _base_url()).rstrip("/")
    agent_key = ""
    try:
        resp = requests.get(f"{base}/agent-config", timeout=10)
        if resp.ok:
            cfg_data  = resp.json()
            fetched   = cfg_data.get("server_url", "").rstrip("/")
            agent_key = cfg_data.get("agent_api_key", "")
            if fetched:
                _LOG.info("  /agent-config returned server_url: %s", fetched)
                base = fetched
            if agent_key:
                _LOG.info("  /agent-config provided agent_api_key (length %d)", len(agent_key))
            else:
                _LOG.warning("  /agent-config did not return agent_api_key — set AGENT_API_KEY on server")
    except Exception as e:
        _LOG.warning("  Could not fetch /agent-config: %s — using %s", e, base)

    # 3. Write config.json
    config = {
        "ingest_url":     f"{base}/ingest",
        "api_key":        agent_key,
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

    # 5. Register scheduled task
    if _register_scheduled_task(exe_dest):
        _LOG.info("  Scheduled task 'TelemetryAgent' registered (trigger: ONLOGON)")
    else:
        _LOG.error(
            "  Scheduled task registration failed — "
            "re-run as Administrator or create the task manually"
        )

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
        help="Server base URL for install (e.g. http://host:8000)",
    )
    args = parser.parse_args()

    _setup_logging()

    if args.install:
        install(server_url=args.server_url)
        return

    # ── Normal run ────────────────────────────────────────────────────────────
    user_info = get_user_info()
    username  = user_info["username"]
    hostname  = user_info["hostname"]
    state     = TelemetryState()

    event_buffer:       list = []
    elapsed_since_log:  int  = 0
    elapsed_since_flush: int = 0
    last_event_app:     str  = None  # tracks app at last event boundary for change detection

    _LOG.info("Agent started — %s @ %s", username, hostname)
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
                elapsed_since_flush += _sleep_gap
                _LOG.info("Sleep/resume gap: %ds of screen-off time captured", _sleep_gap)

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
