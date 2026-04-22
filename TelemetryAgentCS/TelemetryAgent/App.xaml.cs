using System.Threading;
using System.Windows;
using TelemetryAgent.Data;
using TelemetryAgent.Install;
using TelemetryAgent.SystemTray;
using TelemetryAgent.Tracking;

namespace TelemetryAgent;

public partial class App : Application
{
    private static Mutex? _instanceMutex;
    private TrayManager? _tray;
    private WindowTracker? _windowTracker;
    private LockDetector? _lockDetector;
    private EventBuffer? _buffer;
    private System.Threading.Timer? _heartbeatTimer;

    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // ── Single-instance guard ────────────────────────────────────────────
        _instanceMutex = new Mutex(true, "TelemetryAgent_SingleInstance", out bool isNew);
        if (!isNew)
        {
            MessageBox.Show("TelemetryAgent is already running.", "TelemetryAgent",
                MessageBoxButton.OK, MessageBoxImage.Information);
            Shutdown();
            return;
        }

        // ── CLI: --install / --uninstall ─────────────────────────────────────
        var args = e.Args;
        if (args.Length > 0)
        {
            string serverUrl = args.Length > 1 && args[0] == "--install" ? args[1] : string.Empty;
            if (args[0] == "--install")  { Installer.Install(serverUrl); Shutdown(); return; }
            if (args[0] == "--uninstall") { Installer.Uninstall();       Shutdown(); return; }
        }

        var cfg    = Config.Load();
        var client = new ApiClient(cfg);
        var backup = new BackupManager();
        _buffer        = new EventBuffer(cfg, client, backup);
        _windowTracker = new WindowTracker();
        _lockDetector  = new LockDetector();

        // Wire tracking events → buffer
        _windowTracker.StateChanged += (s, st) => _buffer.OnStateChanged(st);
        _lockDetector.LockChanged   += (s, locked) => _buffer.OnLockChanged(locked);

        _windowTracker.Start();
        _lockDetector.Start();

        // Heartbeat timer: flush every 30s regardless of state changes
        _heartbeatTimer = new System.Threading.Timer(
            _ => _buffer.FlushAsync(),
            null,
            TimeSpan.FromSeconds(30),
            TimeSpan.FromSeconds(30));

        _tray = new TrayManager(cfg, _buffer, client);
        _tray.ExitRequested += OnExitRequested;
    }

    private void OnExitRequested(object? sender, EventArgs e)
    {
        _buffer?.FlushAsync().Wait(TimeSpan.FromSeconds(5));
        Shutdown();
    }

    protected override void OnExit(ExitEventArgs e)
    {
        _heartbeatTimer?.Dispose();
        _windowTracker?.Stop();
        _lockDetector?.Stop();
        _tray?.Dispose();
        _instanceMutex?.ReleaseMutex();
        base.OnExit(e);
    }
}
