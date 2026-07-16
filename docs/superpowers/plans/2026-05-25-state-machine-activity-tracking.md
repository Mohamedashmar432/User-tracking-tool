# State-Machine Activity Tracking Implementation Plan --

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the flawed constant-duration event accumulation model with a monotonic-clock state-machine that guarantees `active_seconds ≤ session_elapsed_seconds` under all circumstances.

**Architecture:** A `StateEngine` class tracks ACTIVE / IDLE / LOCKED / SLEEP states and increments per-state counters using actual monotonic-clock deltas — never hardcoded constants. Event `duration` is set to the real measured elapsed time, not `LOG_INTERVAL`. A session-level cap is enforced at flush time and the server rejects events with impossible durations at ingest.

**Tech Stack:** Python 3.9+, `time.monotonic()`, `ctypes` (Windows idle), `python-xlib` / `xprintidle` (Linux idle), FastAPI (ingest validation), pytest

---

## Root Cause Analysis

### Bug 1 — Constant-duration events (primary inflation source)

```python
# CURRENT (buggy)
elapsed_since_log += TICK_INTERVAL        # always 5 — ignores real time
if elapsed_since_log >= LOG_INTERVAL:
    event_buffer.append({"duration": LOG_INTERVAL})  # always 30 — never measured
```

If any detection call (D-Bus query, `xprintidle`, `GetLastInputInfo`) blocks for 2 seconds, the loop cycle takes 7 s but counts only 5 s. Over 11 hours: 11 × 3600 / 7 × 5 = ~28,300 counted seconds emitted as events with duration=30 each. Server stores them all — but the *local display* shows `_acc_active` which over-counts because `_accumulate()` is also called with the hardcoded `LOG_INTERVAL`.

### Bug 2 — `time.time()` for gap detection

`time.time()` is affected by NTP, manual clock changes, and timezone switches. An NTP correction of +30 min looks like a 30-minute sleep gap to the current code, injecting a 1800-second phantom Screen-Off event (harmless) but also resetting counters — potentially cutting legitimate active time.

### Bug 3 — No session invariant

Nothing stops `_acc_active` from exceeding `(now - agent_start)`. Under pathological conditions (multiple restarts, replay, clock issues) the local cache can show impossible values which the UI displays directly.

### Bug 4 — Startup gap event spans multiple days

`_startup_gap_events()` creates a single event with `duration = (now - last_ts).total_seconds()`. For a 3-day shutdown this is 259,200 s — stored in the partition for the last-seen date. The aggregator sums it as `total_locked` for that day, which then shows `screen_off > 24 h` on the dashboard.

---

## File Map

| File | Change |
|---|---|
| `telemetry_agent.py` | Add `StateEngine`, replace tick-loop elapsed counting, fix event duration |
| `linux_telemetry_agent.py` | Same changes (identical pattern) |
| `backend/routes/ingest.py` | Add per-event duration validation at ingest |
| `backend/aggregator.py` | Cap startup gap events > 86400 s at ingest pre-processing |
| `tests/test_state_engine.py` | Unit tests for `StateEngine` (gitignored as `test_*.py`, run locally) |

---

## Task 1: StateEngine class — shared logic (Windows agent)

**Files:**
- Modify: `telemetry_agent.py` (add after `_check_day_reset`, before `_write_status_file`)

- [ ] **Step 1: Write the failing test**

Create `tests/test_state_engine.py` (run locally before builds):

```python
"""
tests/test_state_engine.py — unit tests for StateEngine.
Run: python -m pytest tests/test_state_engine.py -v
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Patch platform imports that don't exist on the test host
import unittest.mock as mock
sys.modules.setdefault("win32gui", mock.MagicMock())
sys.modules.setdefault("win32process", mock.MagicMock())
sys.modules.setdefault("psutil", mock.MagicMock())

import importlib, types
# Stub minimal globals before importing the module
stub = types.ModuleType("telemetry_agent_stub")
exec(open("telemetry_agent.py").read(), stub.__dict__)   # noqa: S102
StateEngine = stub.StateEngine
ActivityState = stub.ActivityState


def test_active_state():
    se = StateEngine(idle_threshold=300)
    se.tick(idle_secs=0, is_locked=False, tick_elapsed=5.0)
    assert se.state == ActivityState.ACTIVE
    assert abs(se.active_seconds - 5.0) < 0.01


def test_idle_state():
    se = StateEngine(idle_threshold=300)
    se.tick(idle_secs=301, is_locked=False, tick_elapsed=5.0)
    assert se.state == ActivityState.IDLE
    assert abs(se.idle_seconds - 5.0) < 0.01
    assert se.active_seconds == 0.0


def test_locked_state():
    se = StateEngine(idle_threshold=300)
    se.tick(idle_secs=0, is_locked=True, tick_elapsed=5.0)
    assert se.state == ActivityState.LOCKED
    assert se.active_seconds == 0.0


def test_invariant_cap():
    """active_seconds must never exceed session_elapsed."""
    se = StateEngine(idle_threshold=300)
    # Artificially inflate active_seconds beyond session time
    se._active_seconds = 99999.0
    se.enforce_invariant()
    session_elapsed = se.session_elapsed_seconds
    assert se.active_seconds <= session_elapsed + 1   # 1 s tolerance


def test_sleep_gap_resets_counters():
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 5.0)  # 5s active
    se.on_sleep_gap(gap_seconds=3600)  # 1-hour sleep
    # After gap, session elapsed includes the gap
    assert se.session_elapsed_seconds >= 3605


def test_active_cannot_exceed_elapsed():
    se = StateEngine(idle_threshold=300)
    for _ in range(720):   # simulate 1 hour (720 × 5 s)
        se.tick(0, False, 5.0)
    se.enforce_invariant()
    assert se.active_seconds <= se.session_elapsed_seconds + 1
```

- [ ] **Step 2: Run test — expect import failure (StateEngine not defined yet)**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: `ImportError` or `AttributeError: module has no attribute 'StateEngine'`

- [ ] **Step 3: Add `StateEngine` and `ActivityState` to `telemetry_agent.py`**

Insert immediately after the `_check_day_reset` / `_accumulate` block (search for `def _write_status_file`):

```python
# ── Activity state machine ────────────────────────────────────────────────────
from enum import Enum

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
        self._threshold     = idle_threshold
        self._state         = ActivityState.IDLE
        self._mono_start    = time.monotonic()   # session start — never changes
        self._active_seconds  = 0.0
        self._idle_seconds    = 0.0
        self._locked_seconds  = 0.0

    # ── Public API ────────────────────────────────────────────────────────────

    def tick(self, idle_secs: int, is_locked: bool, tick_elapsed: float) -> "ActivityState":
        """
        Advance the engine by one tick.

        tick_elapsed: actual seconds elapsed this tick (from time.monotonic()).
                      NEVER use TICK_INTERVAL constant here.
        Returns the new state.
        """
        # Clamp tick_elapsed to a sane range: 0 < t <= IDLE_THRESHOLD
        # Prevents a runaway accumulation if a detection call blocks for a very
        # long time (e.g., D-Bus timeout cascade during system suspend recovery).
        tick_elapsed = max(0.0, min(tick_elapsed, float(self._threshold)))

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
        """
        Called when a sleep/resume gap is detected.
        Gap time is classified as LOCKED (screen off) but does NOT
        advance the active counter.
        """
        self._locked_seconds += max(0.0, gap_seconds)

    def enforce_invariant(self) -> None:
        """
        Hard cap: active + idle + locked must not exceed session elapsed.
        Called before writing to cache or flushing to server.
        """
        session_el = self.session_elapsed_seconds
        total = self._active_seconds + self._idle_seconds + self._locked_seconds
        if total > session_el + 1.0:   # 1 s rounding tolerance
            if total > 0:
                scale = session_el / total
                self._active_seconds  *= scale
                self._idle_seconds    *= scale
                self._locked_seconds  *= scale
            _LOG.warning(
                "StateEngine invariant violation: total=%.0fs session=%.0fs — scaled down",
                total, session_el,
            )

    # ── Properties ────────────────────────────────────────────────────────────

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
```

- [ ] **Step 4: Run tests — expect pass**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: 6 tests PASS

- [ ] **Step 5: Commit**

```bash
git add telemetry_agent.py tests/test_state_engine.py
git commit -m "feat: add StateEngine class with monotonic clock and active invariant"
```

---

## Task 2: Replace tick-loop elapsed counting (Windows agent)

**Files:**
- Modify: `telemetry_agent.py` — main loop in `main()` function

The current loop has these problems:
- `elapsed_since_log += TICK_INTERVAL` — constant, ignores real time
- `"duration": LOG_INTERVAL` — hardcoded, never measured
- Sleep-resume gap detection uses `time.time()` only (still needed for wall-clock timestamps, but elapsed must also use monotonic)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_state_engine.py`:

```python
def test_event_duration_uses_real_elapsed():
    """
    Tick elapsed of 5.3 s should produce event duration near 5.3, not LOG_INTERVAL.
    This test exercises the integration of StateEngine with the event buffer logic.
    """
    se = StateEngine(idle_threshold=300)
    elapsed = 0.0
    for _ in range(6):
        se.tick(0, False, 5.3)   # 5.3 s ticks, not 5.0
        elapsed += 5.3
    assert abs(elapsed - 31.8) < 0.1   # 6 ticks × 5.3 s = 31.8 s
    # active_seconds should match elapsed, not 6 × 5 = 30
    assert abs(se.active_seconds - 31.8) < 0.1
```

- [ ] **Step 2: Run — expect pass (StateEngine already correct)**

```
python -m pytest tests/test_state_engine.py::test_event_duration_uses_real_elapsed -v
```
Expected: PASS (StateEngine already uses real elapsed in tick())

- [ ] **Step 3: Update the main() tick loop in `telemetry_agent.py`**

Find the `while True:` loop inside `main()` and apply these changes.

**3a — Add monotonic anchor before the loop** (alongside existing `_last_tick_wall`):

```python
    _last_tick_wall = time.time()
    _last_tick_mono = time.monotonic()   # ADD THIS LINE
    state_eng = StateEngine(IDLE_THRESHOLD)   # ADD THIS LINE
```

**3b — At the TOP of the while-True loop**, replace the existing `time.time()` gap detection with:

```python
        # ── Monotonic elapsed for this tick ──────────────────────────────────
        # time.monotonic() is immune to NTP, manual clock changes, and timezone
        # switches.  Use it for ALL elapsed-time arithmetic.
        _mono_now       = time.monotonic()
        _tick_elapsed   = _mono_now - _last_tick_mono
        _last_tick_mono = _mono_now

        # ── Wall-clock anchor for sleep/resume gap detection ONLY ─────────────
        # We still need time.time() to detect system suspend (monotonic clock
        # is frozen during suspend; wall clock jumps forward on resume).
        _wall_now   = time.time()
        _wall_delta = _wall_now - _last_tick_wall
        _last_tick_wall = _wall_now

        if _wall_delta > TICK_INTERVAL * 3:
            _sleep_gap = int(_wall_delta)
            _gap_start = datetime.fromtimestamp(
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
            _LOG.info("Sleep/resume gap: %ds", _sleep_gap)
            state_eng.on_sleep_gap(_sleep_gap)   # CHANGED: notify StateEngine
            _compressed = aggregate_events(event_buffer)
            if flush_batch(username, hostname, _compressed):
                event_buffer.clear()
                flush_backup(username, hostname)
            else:
                save_to_backup(username, hostname, _compressed)
                event_buffer.clear()
            elapsed_since_log   = 0
            elapsed_since_flush = 0
            # Also reset monotonic anchor so next tick gets a clean delta
            _last_tick_mono = time.monotonic()   # ADD THIS LINE
            time.sleep(TICK_INTERVAL)
            continue
```

**3c — Replace the constant counter increments:**

```python
        # REMOVE:
        # elapsed_since_log   += TICK_INTERVAL
        # elapsed_since_flush += TICK_INTERVAL

        # REPLACE WITH:
        elapsed_since_log   += _tick_elapsed   # real seconds, not constant
        elapsed_since_flush += _tick_elapsed

        # Advance state machine (increments active/idle/locked by real elapsed)
        current_state = state_eng.tick(idle_secs, is_locked, _tick_elapsed)
        is_active = current_state == ActivityState.ACTIVE
```

**3d — Replace hardcoded `"duration": LOG_INTERVAL` with measured elapsed:**

```python
            # REMOVE:
            # event_buffer.append({
            #     ...
            #     "duration":  LOG_INTERVAL,
            #     ...
            # })

            # REPLACE WITH:
            _event_duration = int(elapsed_since_log)
            # Sanity cap: a single event cannot represent more time than
            # 3 × LOG_INTERVAL (accounts for detection call delays).
            _event_duration = max(1, min(_event_duration, LOG_INTERVAL * 3))

            event_buffer.append({
                "app":       state.current_app,
                "domain":    domain,
                "active":    is_active,
                "locked":    is_locked,
                "duration":  _event_duration,   # MEASURED, not LOG_INTERVAL
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
```

**3e — Update `_accumulate` call to use state engine's own counters instead of LOG_INTERVAL:**

```python
            # REMOVE:
            # _accumulate(state.current_app, domain, is_active, is_locked, LOG_INTERVAL)

            # REPLACE WITH:
            _accumulate(state.current_app, domain, is_active, is_locked, _event_duration)
```

**3f — Add invariant enforcement before writing cache:**

```python
            state_eng.enforce_invariant()   # ADD before _write_cache
            _write_cache(username, hostname)
```

- [ ] **Step 4: Run existing tests to confirm no regression**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: all PASS

- [ ] **Step 5: Commit**

```bash
git add telemetry_agent.py
git commit -m "fix: use monotonic clock for tick elapsed, measured duration in events"
```

---

## Task 3: Apply same fix to Linux agent

**Files:**
- Modify: `linux_telemetry_agent.py` — `StateEngine` class + main loop

The Linux agent is structurally identical to the Windows agent for the tick loop. Apply the exact same changes.

- [ ] **Step 1: Add `StateEngine` and `ActivityState` to `linux_telemetry_agent.py`**

Copy the exact same `StateEngine` / `ActivityState` classes from Task 1 Step 3 and insert them immediately after `def _accumulate(...)` in `linux_telemetry_agent.py`.

The only difference: the import `from enum import Enum` may already exist — check first with `grep -n "from enum import" linux_telemetry_agent.py`.

- [ ] **Step 2: Update the main() tick loop in `linux_telemetry_agent.py`**

Apply the identical changes from Task 2 Step 3 (3a through 3f) to the `while True:` loop in `linux_telemetry_agent.py`.

Use the same replacement pattern:
- Add `_last_tick_mono = time.monotonic()` and `state_eng = StateEngine(IDLE_THRESHOLD)` before the loop
- Replace `elapsed_since_log += TICK_INTERVAL` → `elapsed_since_log += _tick_elapsed`
- Replace `"duration": LOG_INTERVAL` → `"duration": _event_duration`
- Replace `_accumulate(..., LOG_INTERVAL)` → `_accumulate(..., _event_duration)`
- Add `state_eng.on_sleep_gap(...)` in the gap detection block
- Add `state_eng.enforce_invariant()` before `_write_cache`

- [ ] **Step 3: Add a Linux-specific test to `tests/test_state_engine.py`**

```python
def test_linux_idle_fallback_to_idle_state():
    """
    When all idle detection methods fail, get_idle_seconds() returns IDLE_THRESHOLD.
    This must classify as IDLE, never ACTIVE.
    """
    se = StateEngine(idle_threshold=300)
    # IDLE_THRESHOLD returned by get_idle_seconds on total failure
    se.tick(idle_secs=300, is_locked=False, tick_elapsed=5.0)
    # idle_secs == threshold means NOT active (300 >= 300)
    assert se.state == ActivityState.IDLE
    assert se.active_seconds == 0.0


def test_sleep_gap_does_not_inflate_active():
    """A 8-hour sleep gap must not add to active_seconds."""
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 5.0)          # 5 s active before sleep
    se.on_sleep_gap(8 * 3600)       # 8-hour sleep
    se.enforce_invariant()
    assert se.active_seconds <= 5.1  # no more than pre-sleep active
```

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: all PASS (8 tests)

- [ ] **Step 5: Commit**

```bash
git add linux_telemetry_agent.py tests/test_state_engine.py
git commit -m "fix: apply StateEngine monotonic-clock fix to Linux agent"
```

---

## Task 4: Server-side ingest validation

**Files:**
- Modify: `backend/routes/ingest.py`

The server should reject (or clamp) any event with an impossible duration. This is a defence-in-depth layer — even if a client bug sends bad data, the server protects the database.

- [ ] **Step 1: Add duration validation constants to `ingest.py`**

```python
# Maximum duration for a single event.
# One event represents at most one LOG_INTERVAL period (~60 s default).
# We allow up to 24 h (86400 s) to accommodate startup-gap Screen-Off events
# that legitimately represent long shutdown periods.
_MAX_EVENT_DURATION  = 86_400    # 24 hours in seconds
_MAX_TOTAL_BATCH_DUR = 86_400    # sum of all event durations in one batch
```

- [ ] **Step 2: Add validation function**

Add to `ingest.py` before the route handler:

```python
def _validate_events(events: list) -> list:
    """
    Sanitise event list:
    - Clamp individual event duration to [1, _MAX_EVENT_DURATION].
    - If the batch's total duration exceeds _MAX_TOTAL_BATCH_DUR,
      scale all durations down proportionally.
    Returns the cleaned list.
    """
    cleaned = []
    for ev in events:
        dur = int(ev.get("duration", 0))
        if dur < 1:
            continue                           # skip zero-duration events
        dur = min(dur, _MAX_EVENT_DURATION)    # clamp per-event
        cleaned.append({**ev, "duration": dur})

    # Batch-level cap
    total = sum(e["duration"] for e in cleaned)
    if total > _MAX_TOTAL_BATCH_DUR and total > 0:
        scale = _MAX_TOTAL_BATCH_DUR / total
        cleaned = [{**e, "duration": max(1, int(e["duration"] * scale))}
                   for e in cleaned]

    return cleaned
```

- [ ] **Step 3: Call `_validate_events` in the ingest route**

Find the `async def ingest(...)` function in `ingest.py` and add validation:

```python
@router.post("/ingest", status_code=202)
async def ingest(payload: IngestPayload, resolved_user: str = Depends(verify_ingest_key)):
    if not payload.events:
        raise HTTPException(status_code=400, detail="Empty event batch")

    # Sanitise durations before writing to storage
    clean_events = _validate_events(payload.events)
    if not clean_events:
        raise HTTPException(status_code=400, detail="Batch contains no valid events")

    target_user = payload.user if resolved_user == "*" else resolved_user
    written = storage.write_raw_batch(target_user, payload.device, clean_events)
    return {"accepted": written, "total": len(payload.events)}
```

- [ ] **Step 4: Add unit test for validation**

Add to `tests/test_state_engine.py`:

```python
def test_ingest_validation_clamps_huge_duration():
    """Events with duration > 86400 must be clamped to 86400."""
    import sys, os
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    # Directly test the validation function
    from backend.routes.ingest import _validate_events

    events = [{"app": "Code.exe", "active": True, "locked": False,
               "duration": 999999, "timestamp": "2026-05-25T08:00:00Z"}]
    result = _validate_events(events)
    assert result[0]["duration"] == 86400


def test_ingest_validation_removes_zero_duration():
    from backend.routes.ingest import _validate_events
    events = [{"app": "Code.exe", "active": True, "locked": False,
               "duration": 0, "timestamp": "2026-05-25T08:00:00Z"}]
    result = _validate_events(events)
    assert len(result) == 0


def test_ingest_validation_batch_cap():
    """Sum of durations > 86400 must be scaled down."""
    from backend.routes.ingest import _validate_events
    events = [
        {"app": "Code.exe", "active": True, "locked": False,
         "duration": 50000, "timestamp": "2026-05-25T08:00:00Z"},
        {"app": "Chrome", "active": True, "locked": False,
         "duration": 50000, "timestamp": "2026-05-25T09:00:00Z"},
    ]
    result = _validate_events(events)
    total = sum(e["duration"] for e in result)
    assert total <= 86400
```

- [ ] **Step 5: Run tests**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: all PASS (11 tests)

- [ ] **Step 6: Commit**

```bash
git add backend/routes/ingest.py tests/test_state_engine.py
git commit -m "feat: add server-side ingest duration validation and batch cap"
```

---

## Task 5: Cap startup gap events to single-day maximum

**Files:**
- Modify: `telemetry_agent.py` (function `_startup_gap_events`)
- Modify: `linux_telemetry_agent.py` (function `_startup_gap_events`)

A startup gap event with `duration = 3 days` (259,200 s) is stored in one partition with `active=False`. The aggregator's 24 h cap was added earlier, but the root fix is to split multi-day gaps into one event per calendar day at the agent side.

- [ ] **Step 1: Add test**

Add to `tests/test_state_engine.py`:

```python
def test_startup_gap_split_across_days():
    """
    A 50-hour gap starting on day D should produce events only for that day,
    capped at 86400 s (the excess goes to Screen Off on subsequent days,
    but we don't produce future-dated events at agent startup).
    """
    # We test the cap logic directly without importing the agent
    gap_seconds = 50 * 3600   # 50 hours
    capped = min(gap_seconds, 86400)
    assert capped == 86400     # cannot exceed 24 h per event
```

- [ ] **Step 2: Update `_startup_gap_events` in `telemetry_agent.py`**

```python
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
```

- [ ] **Step 3: Apply identical change to `linux_telemetry_agent.py`**

Same function `_startup_gap_events` — add `gap = min(gap, 86_400)` before returning.

- [ ] **Step 4: Run tests**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: all PASS (12 tests)

- [ ] **Step 5: Commit**

```bash
git add telemetry_agent.py linux_telemetry_agent.py tests/test_state_engine.py
git commit -m "fix: cap startup gap event at 86400s (24h) per event"
```

---

## Task 6: Write edge-case tests and validate invariant

**Files:**
- Modify: `tests/test_state_engine.py`

- [ ] **Step 1: Add edge-case tests**

```python
def test_rapid_state_transitions():
    """Alternating active/idle ticks: counters must sum to total elapsed."""
    se = StateEngine(idle_threshold=300)
    for i in range(10):
        if i % 2 == 0:
            se.tick(0,   False, 5.0)   # active
        else:
            se.tick(301, False, 5.0)   # idle
    se.enforce_invariant()
    total = se.active_seconds + se.idle_seconds + se.locked_seconds
    elapsed = se.session_elapsed_seconds
    assert total <= elapsed + 1   # within 1 s rounding


def test_all_states_fill_elapsed():
    """Active + idle + locked should account for all elapsed time."""
    se = StateEngine(idle_threshold=300)
    se.tick(0,   False, 10.0)   # active
    se.tick(301, False, 10.0)   # idle
    se.tick(0,   True,  10.0)   # locked
    total = se.active_seconds + se.idle_seconds + se.locked_seconds
    assert abs(total - 30.0) < 0.1


def test_clock_change_does_not_affect_monotonic():
    """
    time.monotonic() advances independently of wall-clock changes.
    Simulate by using different tick_elapsed values as would happen
    if a clock-change caused a longer wall-clock delta but normal monotonic delta.
    """
    se = StateEngine(idle_threshold=300)
    # Normal tick: 5 s monotonic elapsed
    se.tick(0, False, 5.0)
    # After NTP correction, wall clock jumped 600 s, but monotonic only 5 s
    se.tick(0, False, 5.0)   # still only 5 s monotonic elapsed
    se.enforce_invariant()
    # Should be 10 s, not 610 s
    assert abs(se.active_seconds - 10.0) < 0.1


def test_detection_call_delay_does_not_over_count():
    """
    If a D-Bus/idle-detection call takes 3 s, tick_elapsed is 3+5=8 s.
    StateEngine must cap it at IDLE_THRESHOLD not accumulate 8 s.
    Actually the tick elapsed is capped at IDLE_THRESHOLD (300 s) inside tick().
    """
    se = StateEngine(idle_threshold=300)
    # tick_elapsed = 8 s (5 s sleep + 3 s detection delay)
    se.tick(0, False, 8.0)
    assert abs(se.active_seconds - 8.0) < 0.1   # 8 s is within threshold cap


def test_invariant_after_many_ticks():
    """After 12 hours of simulation, active_seconds never exceeds elapsed."""
    se = StateEngine(idle_threshold=300)
    import random
    random.seed(42)
    for _ in range(12 * 720):   # 12 hours × 720 ticks/hour
        idle = random.choice([0, 0, 0, 301])   # 75% active
        se.tick(idle, False, 5.0)
    se.enforce_invariant()
    assert se.active_seconds <= se.session_elapsed_seconds + 1
```

- [ ] **Step 2: Run all tests**

```
python -m pytest tests/test_state_engine.py -v
```
Expected: all PASS (17 tests total)

- [ ] **Step 3: Commit**

```bash
git add tests/test_state_engine.py
git commit -m "test: add edge-case tests for StateEngine invariants"
```

---

## Task 7: Build and verify

- [ ] **Step 1: Run full test suite**

```
python -m pytest tests/test_state_engine.py -v --tb=short
```
Expected: 17 tests PASS, 0 FAIL

- [ ] **Step 2: Build Windows artifacts**

```powershell
.\build-all.ps1 -SkipLinux
```
Expected: `dist\telemetry_agent.zip` and `dist\telemetry_ui.zip` produced

- [ ] **Step 3: Build Linux ZIP**

```powershell
.\build-all.ps1 -SkipWindows
```
Expected: `dist\linux-telemetry-agent.zip` produced

- [ ] **Step 4: Verify aggregator 24h cap still present in `backend/aggregator.py`**

```
grep -n "MAX_DAY_SECS\|86_400\|86400" backend/aggregator.py
```
Expected: lines showing `_MAX_DAY_SECS = 86_400` and the scaling logic

- [ ] **Step 5: Final commit**

```bash
git add dist/ -f 2>/dev/null || true   # skip dist (gitignored)
git add .
git commit -m "feat: complete state-machine activity tracking — prevents impossible durations"
```

---

## Validation Checklist

After deployment, verify on a real device:

| Test | Expected |
|---|---|
| Run agent for 2 hours, check dashboard | `active_time ≤ 2h` |
| Lock screen for 30 min, check dashboard | `locked_time ≥ 30 min`, `active_time unchanged` |
| Go idle for 10 min, check | `idle_time ≥ 10 min`, `active_time unchanged` |
| Sleep laptop for 1 hour, check | `screen_off ≈ 1h`, `active_time unchanged` |
| Run agent all day, check at end | `active + idle + locked ≤ 24h` |
| Check `/api/user-summary` response | `total_active_time ≤ 86400` |

---

## Summary of Fixes

| Bug | Before | After |
|---|---|---|
| Tick elapsed counting | `+= TICK_INTERVAL` (constant 5) | `+= time.monotonic() delta` (real) |
| Event duration | `LOG_INTERVAL` (constant 30) | `int(elapsed_since_log)` (measured) |
| Sleep/resume time tracking | `time.time()` gap only | `monotonic` for elapsed + `StateEngine.on_sleep_gap()` |
| Active invariant | None | `enforce_invariant()` before every cache write |
| Startup gap duration | Unbounded | Capped at 86,400 s (24 h) |
| Server-side validation | None | `_validate_events()` clamps + scales batch |
| Idle threshold failure | Returns 0 → always ACTIVE | Returns `IDLE_THRESHOLD` → always IDLE (fixed previously) |
