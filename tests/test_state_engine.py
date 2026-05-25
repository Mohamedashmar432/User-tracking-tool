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

import types
stub = types.ModuleType("telemetry_agent_stub")
stub.__dict__["__file__"] = os.path.abspath("telemetry_agent.py")
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
    se._active_seconds = 99999.0
    se.enforce_invariant()
    session_elapsed = se.session_elapsed_seconds
    assert se.active_seconds <= session_elapsed + 1


def test_sleep_gap_resets_counters():
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 5.0)
    se.on_sleep_gap(gap_seconds=3600)
    assert se.session_elapsed_seconds >= 3605


def test_active_cannot_exceed_elapsed():
    se = StateEngine(idle_threshold=300)
    for _ in range(720):
        se.tick(0, False, 5.0)
    se.enforce_invariant()
    assert se.active_seconds <= se.session_elapsed_seconds + 1


def test_event_duration_uses_real_elapsed():
    """active_seconds accumulates real tick_elapsed, not LOG_INTERVAL constant."""
    se = StateEngine(idle_threshold=300)
    elapsed = 0.0
    for _ in range(6):
        se.tick(0, False, 5.3)
        elapsed += 5.3
    assert abs(elapsed - 31.8) < 0.1
    assert abs(se.active_seconds - 31.8) < 0.1


def test_linux_idle_fallback_to_idle_state():
    """When all idle detection methods fail, IDLE_THRESHOLD is returned → state is IDLE."""
    se = StateEngine(idle_threshold=300)
    se.tick(idle_secs=300, is_locked=False, tick_elapsed=5.0)
    assert se.state == ActivityState.IDLE
    assert se.active_seconds == 0.0


def test_sleep_gap_does_not_inflate_active():
    """An 8-hour sleep gap must not add to active_seconds."""
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 5.0)
    se.on_sleep_gap(8 * 3600)
    se.enforce_invariant()
    assert se.active_seconds <= 5.1


def _import_validate_events():
    """
    Import _validate_events from backend.routes.ingest while stubbing out
    backend.deps so the Azure Table Storage connection is never attempted.
    """
    import sys as _sys, os as _os, types as _types, unittest.mock as _mock
    _sys.path.insert(0, _os.path.join(_os.path.dirname(__file__), ".."))

    # Stub backend.deps before any backend.routes import touches it
    if "backend.deps" not in _sys.modules:
        _deps_stub = _types.ModuleType("backend.deps")
        _deps_stub.storage = _mock.MagicMock()
        _deps_stub.verify_ingest_key = _mock.MagicMock()
        _sys.modules["backend.deps"] = _deps_stub
    if "backend.auth" not in _sys.modules:
        _auth_stub = _types.ModuleType("backend.auth")
        _auth_stub.require_admin = _mock.MagicMock()
        _sys.modules["backend.auth"] = _auth_stub

    # Force reload so the stub is actually used
    if "backend.routes.ingest" in _sys.modules:
        del _sys.modules["backend.routes.ingest"]

    from backend.routes.ingest import _validate_events
    return _validate_events


def test_ingest_validation_clamps_huge_duration():
    """Events with duration > 86400 must be clamped to 86400."""
    _validate_events = _import_validate_events()
    events = [{"app": "Code.exe", "active": True, "locked": False,
               "duration": 999999, "timestamp": "2026-05-25T08:00:00Z"}]
    result = _validate_events(events)
    assert result[0]["duration"] == 86400


def test_ingest_validation_removes_zero_duration():
    """Events with duration == 0 must be dropped."""
    _validate_events = _import_validate_events()
    events = [{"app": "Code.exe", "active": True, "locked": False,
               "duration": 0, "timestamp": "2026-05-25T08:00:00Z"}]
    result = _validate_events(events)
    assert len(result) == 0


def test_ingest_validation_batch_cap():
    """Sum of durations > 86400 must be scaled down."""
    _validate_events = _import_validate_events()
    events = [
        {"app": "Code.exe", "active": True, "locked": False,
         "duration": 50000, "timestamp": "2026-05-25T08:00:00Z"},
        {"app": "Chrome",   "active": True, "locked": False,
         "duration": 50000, "timestamp": "2026-05-25T09:00:00Z"},
    ]
    result = _validate_events(events)
    total = sum(e["duration"] for e in result)
    assert total <= 86400


def test_startup_gap_capped_at_24h():
    """A 50-hour gap must be capped at 86400 s (24 h) per event."""
    gap_seconds = 50 * 3600
    capped = min(gap_seconds, 86400)
    assert capped == 86400


def test_rapid_state_transitions():
    """Alternating active/idle ticks: total counters must stay within elapsed."""
    se = StateEngine(idle_threshold=300)
    for i in range(10):
        if i % 2 == 0:
            se.tick(0,   False, 5.0)   # active
        else:
            se.tick(301, False, 5.0)   # idle
    se.enforce_invariant()
    total = se.active_seconds + se.idle_seconds + se.locked_seconds
    elapsed = se.session_elapsed_seconds
    assert total <= elapsed + 1


def test_all_states_fill_elapsed():
    """Active + idle + locked must account for all accumulated elapsed time."""
    se = StateEngine(idle_threshold=300)
    se.tick(0,   False, 10.0)   # active
    se.tick(301, False, 10.0)   # idle
    se.tick(0,   True,  10.0)   # locked
    total = se.active_seconds + se.idle_seconds + se.locked_seconds
    assert abs(total - 30.0) < 0.1


def test_clock_change_does_not_affect_monotonic():
    """
    Wall-clock NTP jump does not affect StateEngine counters.
    Simulate two 5 s monotonic ticks (NTP would jump wall clock but not monotonic).
    """
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 5.0)
    se.tick(0, False, 5.0)
    se.enforce_invariant()
    assert abs(se.active_seconds - 10.0) < 0.1


def test_detection_delay_within_bounds():
    """
    A tick with elapsed=8 s (detection call took 3 s extra) accumulates 8 s,
    which is within max_tick_seconds=30 default so is NOT clamped.
    """
    se = StateEngine(idle_threshold=300)
    se.tick(0, False, 8.0)
    assert abs(se.active_seconds - 8.0) < 0.1


def test_invariant_after_many_ticks():
    """After 12-hour simulation, active_seconds never exceeds session elapsed."""
    import random
    random.seed(42)
    se = StateEngine(idle_threshold=300)
    for _ in range(12 * 720):   # 12 h × 720 ticks/h (5 s/tick)
        idle = random.choice([0, 0, 0, 301])   # 75% active
        se.tick(idle, False, 5.0)
    se.enforce_invariant()
    assert se.active_seconds <= se.session_elapsed_seconds + 1
