using System.Drawing;
using System.Windows.Forms;
using TelemetryAgent.Data;
using TelemetryAgent.UI;

namespace TelemetryAgent.SystemTray;

public sealed class TrayManager : IDisposable
{
    private readonly NotifyIcon  _icon;
    private readonly Config      _cfg;
    private readonly EventBuffer _buffer;
    private readonly ApiClient   _client;
    private PopupWindow?         _popup;

    public event EventHandler? ExitRequested;

    public TrayManager(Config cfg, EventBuffer buffer, ApiClient client)
    {
        _cfg    = cfg;
        _buffer = buffer;
        _client = client;

        _icon = new NotifyIcon
        {
            Icon    = CreateIcon(),
            Text    = "TelemetryAgent",
            Visible = true,
        };

        _icon.MouseClick      += OnIconClick;
        _icon.ContextMenuStrip = BuildContextMenu();

        // Update tooltip every 15s
        var timer = new System.Windows.Forms.Timer { Interval = 15_000 };
        timer.Tick += (_, _) => UpdateTooltip();
        timer.Start();
        UpdateTooltip();
    }

    private void OnIconClick(object? sender, MouseEventArgs e)
    {
        if (e.Button == MouseButtons.Left) ShowPopup();
    }

    private void ShowPopup()
    {
        if (_popup is { IsVisible: true }) { _popup.Activate(); return; }
        _popup = new PopupWindow(_cfg, _buffer, _client);
        _popup.Show();
    }

    private ContextMenuStrip BuildContextMenu()
    {
        var menu = new ContextMenuStrip();

        menu.Items.Add("Open Dashboard", null, (_, _) =>
        {
            try { System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
                { FileName = _cfg.ServerBaseUrl, UseShellExecute = true }); }
            catch { /* browser not available */ }
        });

        menu.Items.Add("Show Stats", null, (_, _) => ShowPopup());
        menu.Items.Add(new ToolStripSeparator());

        menu.Items.Add("Refresh Now", null, (_, _) =>
        {
            _ = _buffer.FlushAsync();
            _popup?.RefreshDataAsync();
        });

        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Exit Agent", null, (_, _) => ExitRequested?.Invoke(this, EventArgs.Empty));

        return menu;
    }

    private void UpdateTooltip()
    {
        string status = _buffer.IsLocked ? "Away" : _buffer.IsActive ? "Active" : "Idle";
        string app    = _buffer.CurrentApp;
        // NotifyIcon.Text is capped at 63 chars
        string tip = $"TelemetryAgent — {status}\n{app}";
        _icon.Text = tip.Length > 63 ? tip[..63] : tip;
    }

    private static Icon CreateIcon()
    {
        // Generate a simple 32×32 icon programmatically — replace with a real .ico in production
        using var bmp = new Bitmap(32, 32);
        using var g   = Graphics.FromImage(bmp);
        g.Clear(Color.FromArgb(30, 215, 96));          // Spotify-green background
        g.FillEllipse(Brushes.White, 6, 6, 20, 20);   // white circle
        g.FillEllipse(new SolidBrush(Color.FromArgb(30, 215, 96)), 10, 10, 12, 12); // inner green hole (donut)
        return Icon.FromHandle(bmp.GetHicon());
    }

    public void Dispose()
    {
        _icon.Visible = false;
        _icon.Dispose();
    }
}
