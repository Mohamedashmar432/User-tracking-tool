using TelemetryAgent.Tracking;

namespace TelemetryAgent.Data;

/// <summary>
/// Thread-safe event accumulator.
///
/// Key improvements over the Python agent:
/// - Flush triggered IMMEDIATELY on any state change (app switch, lock, idle toggle)
///   → dashboard sees the change within the next server poll, not after 120s.
/// - Event granularity: 15s (vs 60s) → finer timeline resolution.
/// - Flush interval: 30s (vs 120s) → max lag ~30s instead of ~3min.
/// </summary>
public sealed class EventBuffer
{
    private readonly Config        _cfg;
    private readonly ApiClient     _client;
    private readonly BackupManager _backup;
    private readonly SemaphoreSlim _lock = new(1, 1);

    private readonly List<TelemetryEvent> _pending = [];

    // Current window state (updated by WindowTracker events)
    private AgentState _current = new();
    private DateTime   _windowStart = DateTime.UtcNow;
    private bool       _flushing;

    public string CurrentApp    => _current.App;
    public bool   IsActive      => _current.Active;
    public bool   IsLocked      => _current.Locked;

    public EventBuffer(Config cfg, ApiClient client, BackupManager backup)
    {
        _cfg    = cfg;
        _client = client;
        _backup = backup;
    }

    // Called by WindowTracker when the foreground app or active state changes
    public void OnStateChanged(AgentState newState)
    {
        var prev = _current;
        _current = newState;

        // Seal the previous window as a completed event
        int duration = Math.Max(1, (int)(DateTime.UtcNow - _windowStart).TotalSeconds);
        _windowStart = DateTime.UtcNow;

        if (prev.App != "Unknown" && duration >= 1)
        {
            var ev = new TelemetryEvent
            {
                App       = prev.App,
                Domain    = prev.Domain,
                Active    = prev.Active,
                Locked    = prev.Locked,
                Duration  = duration,
                Timestamp = DateTime.UtcNow.AddSeconds(-duration).ToString("o"),
            };
            lock (_pending) _pending.Add(ev);
        }

        // Flush immediately on any meaningful state change
        _ = FlushAsync();
    }

    // Called by LockDetector on session lock/unlock
    public void OnLockChanged(bool locked)
    {
        _current = new AgentState
        {
            App    = locked ? "Screen Off" : _current.App,
            Domain = locked ? string.Empty  : _current.Domain,
            Active = !locked,
            Locked = locked,
        };
        OnStateChanged(_current); // seal current window + flush
    }

    // Called by the heartbeat timer every 30s
    public async Task FlushAsync()
    {
        await _lock.WaitAsync();
        try
        {
            if (_flushing) return;
            _flushing = true;

            // Add an in-progress event covering the current open window
            SealCurrentWindow();

            List<TelemetryEvent> batch;
            lock (_pending)
            {
                if (_pending.Count == 0) { _flushing = false; return; }
                batch = new List<TelemetryEvent>(_pending);
                _pending.Clear();
            }

            var merged = Merge(batch);
            bool ok = await _client.PostEventsAsync(_cfg.Username, _cfg.Device, merged);
            if (ok)
                await _backup.ReplayAsync(_cfg.Username, _cfg.Device, _client);
            else
            {
                _backup.Save(_cfg.Username, _cfg.Device, merged);
                // Re-queue so we don't lose events if backup also fails
            }

            _flushing = false;
        }
        finally { _lock.Release(); }
    }

    private void SealCurrentWindow()
    {
        int duration = Math.Max(1, (int)(DateTime.UtcNow - _windowStart).TotalSeconds);
        if (duration < 5) return; // too short — noise

        var ev = new TelemetryEvent
        {
            App       = _current.App,
            Domain    = _current.Domain,
            Active    = _current.Active,
            Locked    = _current.Locked,
            Duration  = duration,
            Timestamp = DateTime.UtcNow.AddSeconds(-duration).ToString("o"),
        };
        lock (_pending) _pending.Add(ev);
        _windowStart = DateTime.UtcNow; // reset for next interval
    }

    // Merge consecutive same-state events (mirrors Python aggregate_events)
    private static List<TelemetryEvent> Merge(List<TelemetryEvent> events)
    {
        if (events.Count == 0) return events;
        var result = new List<TelemetryEvent>();
        var cur = events[0];

        foreach (var ev in events.Skip(1))
        {
            if (ev.App == cur.App && ev.Active == cur.Active && ev.Locked == cur.Locked)
            {
                cur = cur with
                {
                    Duration = cur.Duration + ev.Duration,
                    Domain   = !string.IsNullOrEmpty(ev.Domain) ? ev.Domain : cur.Domain,
                };
            }
            else { result.Add(cur); cur = ev; }
        }
        result.Add(cur);
        return result;
    }
}
