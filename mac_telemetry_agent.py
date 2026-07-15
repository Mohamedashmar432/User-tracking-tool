"""
mac_telemetry_agent.py — macOS background agent.

Mirrors linux_telemetry_agent.py exactly in behavior and payload schema.

Activity detection
------------------
- Idle       : ioreg IOHIDSystem HIDIdleTime (nanoseconds → seconds)
- Active win : osascript via System Events (app name + window title)
- Lock       : osascript screensaver state + loginwindow-as-frontmost check
- Suspend    : wall-clock jump detection (same technique as Windows/Linux)

Startup layers (mirrors Linux 3-layer strategy)
1. launchd user agent  (~/Library/LaunchAgents/com.telemetry.agent.plist, KeepAlive=true)
   launchd acts as its own watchdog — KeepAlive restarts the agent on any crash/exit.

Offline backup: SQLite backup.db  (same as Linux)

IPC with UI companion:
  ~/Library/Application Support/TelemetryAgent/status.json  — updated every tick (~5 s)
  ~/Library/Application Support/TelemetryAgent/cache.json   — updated every event (~30 s)

Permissions note:
  The agent queries System Events via osascript.  On macOS 10.15+ the first run
  may trigger a Transparency/Consent/Control (TCC) prompt asking whether the
  Terminal (or your Python launcher) may control "System Events".  Approve it.
  If window titles remain empty you may also need to grant Accessibility access
  in System Settings → Privacy & Security → Accessibility.
"""

import argparse
import getpass
import glob
import json
import logging
import os
import platform
import plistlib
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, Future
from datetime import datetime, timezone
from enum import Enum

try:
    import fcntl
    _HAS_FCNTL = True
except ImportError:
    _HAS_FCNTL = False

import requests

# ── macOS paths ───────────────────────────────────────────────────────────────────
_HOME = os.path.expanduser("~")

DATA_DIR           = os.path.join(_HOME, "Library", "Application Support", "TelemetryAgent")
SYSTEM_CONFIG_PATH = "/Library/Application Support/TelemetryAgent/config.json"
USER_CONFIG_PATH   = os.path.join(DATA_DIR, "config.json")
STATUS_PATH        = os.path.join(DATA_DIR, "status.json")
CACHE_PATH         = os.path.join(DATA_DIR, "cache.json")
BACKUP_DB_PATH     = os.path.join(DATA_DIR, "backup.db")
LOG_PATH           = os.path.join(DATA_DIR, "agent.log")
LAST_SEEN_PATH     = os.path.join(DATA_DIR, "last_seen.json")
UPDATE_STATUS_PATH = os.path.join(DATA_DIR, "update-status.json")
INGEST_STATUS_PATH = os.path.join(DATA_DIR, "ingest-status.json")
PID_FILE           = os.path.join(DATA_DIR, "agent.pid")

LAUNCH_AGENTS_DIR  = os.path.join(_HOME, "Library", "LaunchAgents")
LAUNCH_AGENT_LABEL = "com.telemetry.agent"
LAUNCH_AGENT_PLIST = os.path.join(LAUNCH_AGENTS_DIR, f"{LAUNCH_AGENT_LABEL}.plist")


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
AGENT_VERSION  = "3.7"

IDLE_THRESHOLD = _cfg.get("idle_threshold",  300)
TICK_INTERVAL  = _cfg.get("tick_interval",     5)
LOG_INTERVAL   = _cfg.get("log_interval",     60)
BATCH_SIZE     = _cfg.get("batch_size",       10)
FLUSH_INTERVAL = _cfg.get("flush_interval",   120)
MAX_BACKUP_EVENTS = 500
MAX_LOG_SIZE   = 10 * 1024 * 1024  # 10 MB

INGEST_URL    = os.getenv("INGEST_URL") or _cfg.get("ingest_url", "http://localhost:8000/ingest")
AGENT_API_KEY = (os.getenv("AGENT_API_KEY") or os.getenv("API_KEY")
                 or _cfg.get("api_key", ""))


def _read_api_key() -> str:
    k = os.getenv("AGENT_API_KEY") or os.getenv("API_KEY")
    if k:
        return k
    for path in (USER_CONFIG_PATH, SYSTEM_CONFIG_PATH):
        try:
            with open(path) as f:
                v = json.load(f).get("api_key", "")
            if v:
                return v
        except Exception:
            continue
    return AGENT_API_KEY


# macOS app names as reported by System Events (capitalized, may include spaces)
BROWSER_PROCESSES = {
    "firefox", "Firefox",
    "safari", "Safari",
    "google chrome", "Google Chrome",
    "chromium", "Chromium",
    "brave browser", "Brave Browser",
    "opera", "Opera",
    "vivaldi", "Vivaldi",
    "microsoft edge", "Microsoft Edge",
    "arc", "Arc",
    # lowercase variants for robustness
    "safari technology preview",
    "orion",
}

# ── Local categorisation (mirrors aggregator.py) ──────────────────────────────────
_UNPRODUCTIVE_DOMAINS = {
    "youtube.com", "youtu.be", "netflix.com", "primevideo.com", "hulu.com",
    "disneyplus.com", "twitch.tv", "instagram.com", "facebook.com",
    "twitter.com", "x.com", "tiktok.com", "reddit.com", "9gag.com",
    "amazon.com", "ebay.com", "buzzfeed.com", "espn.com",
    "web.whatsapp.com", "web.telegram.org",
}
_UNPRODUCTIVE_APPS = {
    "steam", "epicgameslauncher", "spotify", "vlc",
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


# ── Async sampler (non-blocking osascript calls) ───────────────────────────────────
class _AsyncSampler:
    """Runs osascript/ioreg calls in background threads to avoid blocking main loop."""
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="mac-sampler")
        self._win_fut: Future | None = None
        self._lock_fut: Future | None = None
        self._idle_fut: Future | None = None
        # cached last-known values
        self.window: tuple[str, str] = ("Unknown", "")
        self.locked: bool = False
        self.idle_secs: float = 0.0

    def poll(self):
        """Collect previous futures (non-blocking), submit new ones."""
        if self._win_fut and self._win_fut.done():
            try:
                self.window = self._win_fut.result() or ("Unknown", "")
            except Exception:
                pass  # keep last-known value on error
        if self._lock_fut and self._lock_fut.done():
            try:
                self.locked = self._lock_fut.result() or False
            except Exception:
                pass
        if self._idle_fut and self._idle_fut.done():
            try:
                self.idle_secs = self._idle_fut.result() or 0.0
            except Exception:
                pass

        # submit next round only if previous finished (avoid pile-up)
        if not self._win_fut or self._win_fut.done():
            self._win_fut = self._pool.submit(get_active_window)
        if not self._lock_fut or self._lock_fut.done():
            self._lock_fut = self._pool.submit(is_session_locked)
        if not self._idle_fut or self._idle_fut.done():
            self._idle_fut = self._pool.submit(get_idle_seconds)

    def shutdown(self):
        """Shut down background thread pool."""
        self._pool.shutdown(wait=False, cancel_futures=True)


# ── Status + cache writers ────────────────────────────────────────────────────────
def _write_status_file(app: str, active: bool, locked: bool, idle_secs: int, domain: str = "") -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = {
            "timestamp":    datetime.now(timezone.utc).isoformat(),
            "app":          app,
            "domain":       domain,
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
def _run(cmd: list, timeout: float = 2.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout.strip()
    except Exception:
        return ""


# ── Idle detection ────────────────────────────────────────────────────────────────
def get_idle_seconds() -> int:
    """
    Seconds since last user input on macOS.

    Method 1: ioreg IOHIDSystem HIDIdleTime
      The kernel stores idle time in nanoseconds in the IOHIDSystem object.
      This is the most accurate method — it tracks keyboard, mouse, and
      touchpad input across all applications with no TCC permission needed.

    Method 2: Fallback to IDLE_THRESHOLD
      If ioreg fails (should never happen on macOS), treat as idle to avoid
      inflating productivity scores.
    """
    try:
        r = subprocess.run(
            ["ioreg", "-c", "IOHIDSystem"],
            capture_output=True, text=True, timeout=3.0,
        )
        m = re.search(r'"HIDIdleTime"\s*=\s*(\d+)', r.stdout)
        if m:
            return int(m.group(1)) // 1_000_000_000
    except Exception as e:
        _LOG.debug("ioreg idle check failed: %s", e)

    _LOG.debug("Idle detection unavailable — defaulting to idle state")
    return IDLE_THRESHOLD


# ── Lock detection ────────────────────────────────────────────────────────────────
def is_session_locked() -> bool:
    """
    Returns True when the screen is locked or screensaver is active on macOS.

    Method 1: osascript — ask System Events if the screensaver is running.
    Method 2: Check if loginwindow is the frontmost process (lock screen).
    """
    # Method 1: Screensaver running
    out = _run([
        "osascript", "-e",
        'tell application "System Events" to get running of screen saver preferences',
    ])
    if out.strip().lower() == "true":
        return True

    # Method 2: loginwindow is frontmost = lock screen is showing
    out = _run([
        "osascript", "-e",
        'tell application "System Events" to name of first process whose frontmost is true',
    ])
    if out.strip().lower() == "loginwindow":
        return True

    return False


# ── Active window detection ───────────────────────────────────────────────────────
def get_active_window() -> tuple:
    """
    Returns (app_name, window_title) on macOS.

    Uses osascript to query System Events for the frontmost process name and
    the title of its front window.

    Window titles require the calling process to have Accessibility permission
    in System Settings → Privacy & Security → Accessibility.  App names
    (process names) work without Accessibility permission.

    The separator "|||" is used because window titles can contain any printable
    character — including dashes and pipes — but the three-pipe sequence is
    extremely rare in practice.
    """
    script = (
        'tell application "System Events"\n'
        '  set fp to first process whose frontmost is true\n'
        '  set appName to name of fp\n'
        '  try\n'
        '    set winTitle to name of front window of fp\n'
        '  on error\n'
        '    set winTitle to ""\n'
        '  end try\n'
        '  return appName & "|||" & winTitle\n'
        'end tell'
    )
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=3.0,
        )
        if r.returncode == 0 and r.stdout.strip():
            parts = r.stdout.strip().split("|||", 1)
            app   = parts[0].strip() if parts else "Unknown"
            title = parts[1].strip() if len(parts) > 1 else ""
            if app:
                return app, title
    except Exception:
        pass

    # Fallback: just get the app name without a window title
    out = _run([
        "osascript", "-e",
        'tell application "System Events" to name of first process whose frontmost is true',
    ])
    if out:
        return out.strip(), ""

    return "Unknown", ""


def extract_domain(app: str, title: str) -> str:
    """Best-effort browser title → domain/page-title extraction for macOS."""
    # Normalise app name for comparison
    app_lower = (app or "").lower().strip()
    browser_lower = {b.lower().strip() for b in BROWSER_PROCESSES}
    if app_lower not in browser_lower:
        return ""
    return re.sub(
        r"\s*[-–—]\s*(Mozilla Firefox|Google Chrome|Safari|Chromium|Brave|"
        r"Brave Browser|Opera|Vivaldi|Microsoft Edge|Arc|"
        r"Safari Technology Preview|Orion).*$",
        "", title, flags=re.IGNORECASE,
    ).strip()


# ── Singleton guard (PID file + fcntl) ────────────────────────────────────────────
_pid_fd = None


def _acquire_singleton() -> bool:
    global _pid_fd
    if not _HAS_FCNTL:
        return True
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
            _LOG.info("[backup] Evicted batch id=%s", row_id)
        conn.execute(
            "INSERT INTO offline_batches "
            "(created_at, user, device, events_json, event_count) VALUES (?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(),
             username, device, json.dumps(events), len(events)),
        )
        conn.commit()
        conn.close()
        _LOG.info("[backup] %d events saved offline -> %s", len(events), BACKUP_DB_PATH)
    except Exception as e:
        _LOG.error("[backup] DB write failed: %s", e)


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
                _LOG.info("[backup] Recovered %d events (id=%s)", len(events), row_id)
            else:
                break
        conn.close()
    except Exception as e:
        _LOG.error("[backup] DB replay failed: %s", e)
    if recovered:
        _LOG.info("[backup] Total recovered: %d events", recovered)
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
    """POST a batch of raw events to the analytics server."""
    if not batch:
        return True
    if INGEST_URL.startswith("http://"):
        _LOG.warning("Security: ingest URL uses plaintext HTTP — telemetry data is unencrypted in transit")
    api_key = _read_api_key()
    try:
        resp = requests.post(
            INGEST_URL,
            json={"user": user, "device": device, "events": batch},
            headers={"X-API-Key": api_key},
            timeout=10,
            verify=True,
        )
        if resp.status_code in (200, 202):
            data = resp.json()
            msg = f"  ->Batch sent: {data.get('accepted')}/{data.get('total')} events"
            print(msg)
            try:
                _LOG.info("POST %s accepted=%s total=%s", INGEST_URL,
                          data.get("accepted"), data.get("total"))
                with open(INGEST_STATUS_PATH, "w") as _sf:
                    json.dump({
                        "last_success":  datetime.now(timezone.utc).isoformat(),
                        "events_sent":   data.get("accepted", len(batch)),
                    }, _sf)
            except Exception:
                pass
            return True
        if resp.status_code == 401:
            _LOG.error(
                "AUTH FAILED (HTTP 401) posting to %s — "
                "the api_key in %s does not match the server's AGENT_API_KEY.",
                INGEST_URL, USER_CONFIG_PATH,
            )
            print(f"  ->AUTH FAILED (401): check api_key in {USER_CONFIG_PATH}")
            return False
        body_preview = (resp.text or "")[:200]
        msg = f"  ->Server rejected [{resp.status_code}]: {body_preview}"
        print(msg)
        _LOG.error("POST %s returned HTTP %d for %d events -- body[:200]=%r",
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
        msg = f"  ->SSL error contacting {INGEST_URL}: {e}"
        print(msg)
        _LOG.error("SSLError to %s -- check CA bundle / corporate proxy: %s",
                   INGEST_URL, e)
        return False
    except requests.exceptions.RequestException as e:
        msg = f"  ->HTTP error: {e}"
        print(msg)
        _LOG.error("RequestException to %s (batch=%d): %s", INGEST_URL, len(batch), e)
        return False
    except Exception as e:
        msg = f"  ->Flush error ({type(e).__name__}): {e}"
        print(msg)
        _LOG.exception("Unexpected flush error to %s (batch=%d):", INGEST_URL, len(batch))
        return False


# ── Connection check + auto-update ───────────────────────────────────────────────
def _base_url() -> str:
    return (INGEST_URL.rsplit("/ingest", 1)[0]
            if "/ingest" in INGEST_URL else INGEST_URL.rsplit("/", 1)[0])


def _ver(v: str) -> tuple:
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


def _write_update_status(server_version: str) -> None:
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(UPDATE_STATUS_PATH, "w", encoding="utf-8") as f:
            json.dump({"update_available": True, "server_version": server_version}, f)
    except Exception:
        pass


def _clear_update_status() -> None:
    try:
        if os.path.exists(UPDATE_STATUS_PATH):
            os.remove(UPDATE_STATUS_PATH)
    except Exception:
        pass


def check_for_update() -> None:
    base       = _base_url()
    health_url = f"{base}/api/health"
    try:
        resp = requests.get(health_url, timeout=10, verify=True)
        if not resp.ok:
            return
        data           = resp.json()
        server_version = data.get("version", "0")
    except Exception as e:
        _LOG.debug("Update check skipped: %s", e)
        return

    if _ver(server_version) > _ver(AGENT_VERSION):
        _LOG.info("Update available: server v%s, running v%s", server_version, AGENT_VERSION)
        _write_update_status(server_version)
    else:
        _clear_update_status()


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


# ── launchctl helper ──────────────────────────────────────────────────────────────
def _launchctl(*args) -> None:
    try:
        subprocess.run(["launchctl"] + list(args),
                       capture_output=True, timeout=10)
    except Exception as e:
        _LOG.warning("launchctl %s: %s", " ".join(args), e)


# ── launchd plist template ────────────────────────────────────────────────────────
def _build_agent_plist(exec_args: list) -> dict:
    """
    Build a launchd agent plist dict.

    KeepAlive=true makes launchd act as a watchdog — it restarts the agent
    automatically on any exit/crash, replacing the separate watchdog service
    used on Linux.  ThrottleInterval=30 gives launchd room to hit its own
    circuit breaker (backs off after too-frequent exits) before the user
    notices a slow login — 10s was too aggressive on real hardware.
    """
    return {
        "Label":            LAUNCH_AGENT_LABEL,
        "ProgramArguments": exec_args,
        "RunAtLoad":        True,
        "KeepAlive":        True,
        "ThrottleInterval": 30,
        "StandardOutPath":  LOG_PATH,
        "StandardErrorPath": LOG_PATH,
    }


# ── Install / Uninstall ───────────────────────────────────────────────────────────
def install(server_url: str = None, admin_key: str = None,
            api_key: str = None) -> None:
    _LOG.info("=== Telemetry Agent Installation (macOS) ===")

    os.makedirs(DATA_DIR, exist_ok=True)
    _LOG.info("  Directory ready: %s", DATA_DIR)

    base      = (server_url or _base_url()).rstrip("/")
    agent_key = api_key or ""
    try:
        headers = {"X-API-Key": admin_key} if admin_key else {}
        resp = requests.get(f"{base}/agent-config", headers=headers, timeout=10, verify=True)
        if resp.ok:
            cfg     = resp.json()
            fetched = cfg.get("server_url", "").rstrip("/")
            if fetched:
                base = fetched
            fetched_key = cfg.get("agent_api_key", "")
            if fetched_key:
                agent_key = fetched_key
                _LOG.info("  Agent API key received from /agent-config")
        elif resp.status_code == 401:
            if agent_key:
                _LOG.info("  /agent-config requires --admin-key; using injected api_key")
            else:
                _LOG.warning("  /agent-config requires --admin-key; no api_key available — edit config.json manually")
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
    with open(USER_CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=4)
    _LOG.info("  Config written: %s", USER_CONFIG_PATH)

    # Prefer the wrapper script created by install.sh (~/.local/bin/telemetry-agent)
    _wrapper = os.path.join(_HOME, ".local", "bin", "telemetry-agent")
    if os.path.isfile(_wrapper) and os.access(_wrapper, os.X_OK):
        exec_args = [_wrapper]
    else:
        exec_args = [sys.executable, os.path.abspath(__file__)]

    # Write launchd plist
    try:
        os.makedirs(LAUNCH_AGENTS_DIR, exist_ok=True)
        plist_data = _build_agent_plist(exec_args)
        with open(LAUNCH_AGENT_PLIST, "wb") as f:
            plistlib.dump(plist_data, f)
        # Unload first in case a previous version is running
        _launchctl("unload", LAUNCH_AGENT_PLIST)
        _launchctl("load", LAUNCH_AGENT_PLIST)
        _LOG.info("  launchd agent registered: %s", LAUNCH_AGENT_PLIST)
    except Exception as e:
        _LOG.warning("  Could not write launchd plist to %s: %s", LAUNCH_AGENTS_DIR, e)
        _LOG.warning("  Agent will not auto-start — run manually: %s", " ".join(exec_args))

    if check_connection(base_url=base):
        _LOG.info("  Server connection: OK")
    else:
        _LOG.warning("  Server connection: FAILED — will retry at runtime")

    _LOG.info("=== Installation complete ===")
    _LOG.info("  Config  : %s", USER_CONFIG_PATH)
    _LOG.info("  Log     : %s", LOG_PATH)
    _LOG.info("  Data    : %s", DATA_DIR)
    _LOG.info("")
    _LOG.info("  Note: If window titles show as empty, grant Accessibility access to")
    _LOG.info("  your terminal / Python in System Settings → Privacy & Security → Accessibility")


def uninstall() -> None:
    _LOG.info("=== Telemetry Agent Uninstall (macOS) ===")

    _launchctl("unload", LAUNCH_AGENT_PLIST)

    try:
        os.remove(LAUNCH_AGENT_PLIST)
        _LOG.info("  Removed: %s", LAUNCH_AGENT_PLIST)
    except FileNotFoundError:
        pass

    shutil.rmtree(DATA_DIR, ignore_errors=True)
    _LOG.info("  Removed: %s", DATA_DIR)

    _LOG.info("=== Uninstall complete — cloud data not affected ===")


# ── Main loop ─────────────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="macOS Telemetry Agent")
    parser.add_argument("--install",    action="store_true")
    parser.add_argument("--uninstall",  action="store_true")
    parser.add_argument("--server-url", default=None)
    parser.add_argument("--admin-key",  default=None)
    parser.add_argument("--api-key",    default=None,
                        help="Agent ingest API key — injected by the server "
                             "during curl|bash installs so no --admin-key is needed.")
    args = parser.parse_args()

    _setup_logging()

    if args.install:
        install(server_url=args.server_url, admin_key=args.admin_key,
                api_key=args.api_key)
        return
    if args.uninstall:
        uninstall()
        return

    if not _acquire_singleton():
        sys.exit(0)

    check_for_update()

    username = getpass.getuser()
    hostname = platform.node()
    state    = TelemetryState()

    event_buffer:        list = []
    elapsed_since_log:   int  = 0
    elapsed_since_flush: int  = 0
    last_event_app:      str  = None

    _LOG.info("Agent v%s started — %s @ %s (macOS)", AGENT_VERSION, username, hostname)
    _LOG.info(
        "Tick: %ds | Event: %ds | Flush: %ds | URL: %s",
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
    _last_tick_mono = time.monotonic()
    state_eng = StateEngine(IDLE_THRESHOLD)
    sampler = _AsyncSampler()

    def _shutdown(sig, _frame):
        _LOG.info("Signal %d received — flushing and exiting", sig)
        sampler.shutdown()
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            if not flush_batch(username, hostname, compressed):
                save_to_backup(username, hostname, compressed)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT,  _shutdown)

    SLEEP_GAP_MIN_SEC = TICK_INTERVAL * 3
    SLEEP_GAP_MAX_SEC = 86_400

    _update_check_counter   = 0
    _UPDATE_CHECK_THRESHOLD = max(1, 1800 // TICK_INTERVAL)

    try:
        while True:
            _mono_now       = time.monotonic()
            _tick_elapsed   = _mono_now - _last_tick_mono
            _last_tick_mono = _mono_now

            _wall_now   = time.time()
            _wall_delta = max(0.0, _wall_now - _last_tick_wall)
            _last_tick_wall = _wall_now

            if _wall_delta > SLEEP_GAP_MIN_SEC:
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

            sampler.poll()
            is_locked = sampler.locked
            idle_secs = sampler.idle_secs
            app_name, win_title = sampler.window
            domain = extract_domain(app_name, win_title) if not is_locked else ""

            state.update(app_name)
            elapsed_since_log   += _tick_elapsed
            elapsed_since_flush += _tick_elapsed

            current_state = state_eng.tick(idle_secs, is_locked, _tick_elapsed)
            is_active = current_state == ActivityState.ACTIVE

            _write_status_file(app_name, is_active, is_locked, idle_secs, domain)

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

            _update_check_counter += 1
            if _update_check_counter >= _UPDATE_CHECK_THRESHOLD:
                _update_check_counter = 0
                check_for_update()

            time.sleep(TICK_INTERVAL)

    except Exception as e:
        _LOG.error("Agent crash: %s", e)
        sampler.shutdown()
        if event_buffer:
            compressed = aggregate_events(event_buffer)
            if not flush_batch(username, hostname, compressed):
                save_to_backup(username, hostname, compressed)


if __name__ == "__main__":
    main()
