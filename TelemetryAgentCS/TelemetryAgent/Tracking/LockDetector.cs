using Microsoft.Win32;

namespace TelemetryAgent.Tracking;

/// <summary>
/// Listens to SessionSwitch events from the Windows session manager.
/// This is instant (0ms latency) — no polling required.
/// Works for: Win+L lock, lid close, remote desktop disconnect, fast user switching.
/// </summary>
public sealed class LockDetector : IDisposable
{
    public event EventHandler<bool>? LockChanged; // true = locked

    private bool _running;

    public void Start()
    {
        if (_running) return;
        _running = true;
        SystemEvents.SessionSwitch += OnSessionSwitch;
    }

    public void Stop()
    {
        if (!_running) return;
        _running = false;
        SystemEvents.SessionSwitch -= OnSessionSwitch;
    }

    private void OnSessionSwitch(object sender, SessionSwitchEventArgs e)
    {
        bool locked = e.Reason is SessionSwitchReason.SessionLock
                                or SessionSwitchReason.SessionLogoff
                                or SessionSwitchReason.RemoteDisconnect
                                or SessionSwitchReason.ConsoleDisconnect;

        bool unlocked = e.Reason is SessionSwitchReason.SessionUnlock
                                  or SessionSwitchReason.SessionLogon
                                  or SessionSwitchReason.RemoteConnect
                                  or SessionSwitchReason.ConsoleConnect;

        if (locked)   LockChanged?.Invoke(this, true);
        if (unlocked) LockChanged?.Invoke(this, false);
    }

    public void Dispose() => Stop();
}
