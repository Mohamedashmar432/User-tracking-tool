using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;
using System.Windows.Threading;

namespace TelemetryAgent.Tracking;

public sealed class AgentState
{
    public string App     { get; init; } = "Unknown";
    public string Domain  { get; init; } = string.Empty;
    public bool   Active  { get; init; }
    public bool   Locked  { get; init; }
}

/// <summary>
/// Uses SetWinEventHook(EVENT_SYSTEM_FOREGROUND) for instant app-switch events
/// instead of polling, reducing detection latency from 5s → ~50ms.
/// Also samples idle state on a 5s timer for the active/idle transition.
/// </summary>
public sealed class WindowTracker : IDisposable
{
    private static readonly HashSet<string> BrowserProcesses = new(StringComparer.OrdinalIgnoreCase)
        { "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe" };

    private static readonly Regex BrowserTitleSuffix = new(
        @"\s[-–]\s(Google Chrome|Microsoft Edge|Firefox|Brave|Opera|Vivaldi).*$",
        RegexOptions.IgnoreCase | RegexOptions.Compiled);

    public event EventHandler<AgentState>? StateChanged;

    private Win32.WinEventDelegate? _hookDelegate; // held to prevent GC
    private IntPtr _hookHandle = IntPtr.Zero;
    private DispatcherTimer? _idleTimer;
    private AgentState _lastEmitted = new();
    private bool _isLocked;

    public void Start()
    {
        // Hook must be set on a thread with a message loop — use WPF dispatcher
        System.Windows.Application.Current.Dispatcher.Invoke(() =>
        {
            _hookDelegate = OnWinEvent;
            _hookHandle   = Win32.SetWinEventHook(
                Win32.EVENT_SYSTEM_FOREGROUND, Win32.EVENT_SYSTEM_FOREGROUND,
                IntPtr.Zero, _hookDelegate, 0, 0, Win32.WINEVENT_OUTOFCONTEXT);
        });

        // 5-second idle poll — catches active↔idle transitions
        _idleTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _idleTimer.Tick += (_, _) => EmitCurrentState();
        _idleTimer.Start();

        EmitCurrentState(); // emit once immediately on startup
    }

    public void Stop()
    {
        _idleTimer?.Stop();
        if (_hookHandle != IntPtr.Zero)
        {
            Win32.UnhookWinEvent(_hookHandle);
            _hookHandle = IntPtr.Zero;
        }
    }

    public void SetLocked(bool locked) { _isLocked = locked; EmitCurrentState(); }

    private void OnWinEvent(IntPtr hHook, uint eventType, IntPtr hwnd,
        int idObject, int idChild, uint thread, uint time)
        => EmitCurrentState(); // foreground window changed → sample immediately

    private void EmitCurrentState()
    {
        var state = Sample();
        // Only fire event when something meaningful changed
        if (state.App    == _lastEmitted.App   &&
            state.Active == _lastEmitted.Active &&
            state.Locked == _lastEmitted.Locked)
            return;

        _lastEmitted = state;
        StateChanged?.Invoke(this, state);
    }

    private AgentState Sample()
    {
        bool locked = _isLocked || IsDesktopLocked();
        if (locked)
            return new AgentState { App = "Screen Off", Active = false, Locked = true };

        var hwnd = Win32.GetForegroundWindow();
        if (hwnd == IntPtr.Zero)
            return new AgentState { App = "Screen Off", Active = false, Locked = true };

        string appName = GetProcessName(hwnd);
        string domain  = ExtractDomain(hwnd, appName);
        bool   active  = IdleDetector.IdleSeconds() < 300; // use config later

        return new AgentState { App = appName, Domain = domain, Active = active, Locked = false };
    }

    private static string GetProcessName(IntPtr hwnd)
    {
        try
        {
            Win32.GetWindowThreadProcessId(hwnd, out int pid);
            return Process.GetProcessById(pid).ProcessName + ".exe";
        }
        catch { return "Unknown"; }
    }

    private static string ExtractDomain(IntPtr hwnd, string processName)
    {
        if (!BrowserProcesses.Contains(processName)) return string.Empty;
        var sb = new StringBuilder(512);
        Win32.GetWindowText(hwnd, sb, sb.Capacity);
        return BrowserTitleSuffix.Replace(sb.ToString(), string.Empty).Trim();
    }

    private static bool IsDesktopLocked()
    {
        try
        {
            var hwnd = Win32.GetForegroundWindow();
            if (hwnd == IntPtr.Zero) return true;
            Win32.GetWindowThreadProcessId(hwnd, out int pid);
            string name = Process.GetProcessById(pid).ProcessName.ToLower();
            if (name is "lockapp" or "logonui") return true;
        }
        catch { /* ignore */ }

        try
        {
            var hDesk = Win32.OpenInputDesktop(0, false, Win32.DESKTOP_READOBJECTS);
            if (hDesk == IntPtr.Zero) return true;
            var sb = new StringBuilder(256);
            Win32.GetUserObjectInformation(hDesk, Win32.UOI_NAME, sb, (uint)sb.Capacity * 2, out _);
            Win32.CloseDesktop(hDesk);
            return !string.Equals(sb.ToString(), "Default", StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    public void Dispose() => Stop();
}
