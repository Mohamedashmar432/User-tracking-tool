using System.Windows;
using System.Windows.Media;
using System.Windows.Media.Animation;
using System.Windows.Shapes;
using System.Windows.Threading;
using TelemetryAgent.Data;

namespace TelemetryAgent.UI;

public partial class PopupWindow : Window
{
    private readonly Config      _cfg;
    private readonly EventBuffer _buffer;
    private readonly ApiClient   _client;
    private readonly DispatcherTimer _autoRefresh;

    public PopupWindow(Config cfg, EventBuffer buffer, ApiClient client)
    {
        InitializeComponent();
        _cfg    = cfg;
        _buffer = buffer;
        _client = client;

        PositionNearTray();
        Loaded += async (_, _) => await RefreshDataAsync();

        // Auto-refresh every 30s
        _autoRefresh = new DispatcherTimer { Interval = TimeSpan.FromSeconds(30) };
        _autoRefresh.Tick += async (_, _) => await RefreshDataAsync();
        _autoRefresh.Start();

        // Drag to move (click anywhere on header region)
        MouseLeftButtonDown += (_, e) => { if (e.ClickCount == 1) DragMove(); };
    }

    public async Task RefreshDataAsync()
    {
        string today = DateTime.Today.ToString("yyyy-MM-dd");

        // Update current status from local buffer (instant — no network)
        UpdateStatus();

        // Fetch server data
        var summary  = await _client.GetSummaryAsync(_cfg.Username, today);
        var timeline = await _client.GetTimelineAsync(_cfg.Username, today);
        var apps     = await _client.GetAppsAsync(_cfg.Username, today);

        if (summary != null) UpdateSummary(summary, apps);
        if (timeline.Count > 0) DrawActivityChart(timeline);
        DrawDonut(summary, apps);

        LastUpdatedText.Text = $"Updated {DateTime.Now:HH:mm:ss}";
    }

    // ── Status indicator (from local buffer — no server latency) ────────────

    private void UpdateStatus()
    {
        string label;
        Color  dot;
        if (_buffer.IsLocked)         { label = "Away";   dot = Color.FromRgb(239, 68, 68); }
        else if (!_buffer.IsActive)   { label = "Idle";   dot = Color.FromRgb(234, 179, 8); }
        else                          { label = "Active";  dot = Color.FromRgb(74, 222, 128); }

        StatusLabel.Text = label;
        StatusDot.Fill   = new SolidColorBrush(dot);
        CurrentAppText.Text = _buffer.CurrentApp;
    }

    // ── Summary KPI cards ────────────────────────────────────────────────────

    private void UpdateSummary(SummaryResponse s, List<AppEntry> apps)
    {
        ScoreText.Text = $"{s.ProductivityScore:0}%";
        ActiveTimeText.Text = FormatTime(s.TotalActiveTime + s.TotalIdleTime);
        IdleTimeText.Text   = FormatTime(s.TotalIdleTime);

        string top = s.TopApp.Replace(".exe", string.Empty, StringComparison.OrdinalIgnoreCase);
        TopAppText.Text = top.Length > 0 ? top : "—";
    }

    // ── Donut chart (productive vs unproductive) ─────────────────────────────

    private void DrawDonut(SummaryResponse? summary, List<AppEntry> apps)
    {
        DonutCanvas.Children.Clear();
        double size = 140;
        double cx = size / 2, cy = size / 2;
        double outer = 62, inner = 40;

        double prod   = apps.Where(a => a.Category == "Productive").Sum(a => a.Time);
        double unprod = apps.Where(a => a.Category == "Unproductive").Sum(a => a.Time);
        double total  = prod + unprod;
        if (total <= 0)
        {
            // Draw placeholder ring
            DrawArc(cx, cy, outer, inner, 0, 360, "#374151");
            return;
        }

        double prodAngle = prod / total * 360;
        DrawArc(cx, cy, outer, inner, -90, prodAngle - 90, "#4ade80");
        if (unprod > 0)
            DrawArc(cx, cy, outer, inner, prodAngle - 90, 270, "#f87171");
    }

    private void DrawArc(double cx, double cy, double outerR, double innerR,
                         double startDeg, double endDeg, string hexColor)
    {
        double startRad = startDeg * Math.PI / 180;
        double endRad   = endDeg   * Math.PI / 180;
        bool   large    = (endDeg - startDeg) > 180;

        Point outerStart = Polar(cx, cy, outerR, startRad);
        Point outerEnd   = Polar(cx, cy, outerR, endRad);
        Point innerStart = Polar(cx, cy, innerR, endRad);
        Point innerEnd   = Polar(cx, cy, innerR, startRad);

        var figure = new PathFigure { StartPoint = outerStart, IsClosed = true };
        figure.Segments.Add(new ArcSegment(outerEnd, new Size(outerR, outerR), 0, large, SweepDirection.Clockwise, true));
        figure.Segments.Add(new LineSegment(innerStart, true));
        figure.Segments.Add(new ArcSegment(innerEnd, new Size(innerR, innerR), 0, large, SweepDirection.Counterclockwise, true));

        var path = new System.Windows.Shapes.Path
        {
            Data = new PathGeometry(new[] { figure }),
            Fill = new SolidColorBrush((Color)ColorConverter.ConvertFromString(hexColor)),
        };
        DonutCanvas.Children.Add(path);
    }

    private static Point Polar(double cx, double cy, double r, double rad)
        => new(cx + r * Math.Cos(rad), cy + r * Math.Sin(rad));

    // ── 24h activity bar chart ────────────────────────────────────────────────

    private void DrawActivityChart(List<TimelineEntry> timeline)
    {
        ActivityCanvas.Children.Clear();
        double[] hours = new double[24];

        foreach (var entry in timeline.Where(e => e.Active))
        {
            if (!DateTime.TryParse(entry.Timestamp, out var ts)) continue;
            int h = ts.ToLocalTime().Hour;
            hours[h] = Math.Min(hours[h] + entry.Duration, 3600);
        }

        double maxSec  = hours.Max();
        if (maxSec <= 0) return;

        double canvasW = 288; // 320 - 2*16 margin
        double canvasH = 70;
        double barW    = canvasW / 24 - 1;

        for (int h = 0; h < 24; h++)
        {
            double barH = (hours[h] / maxSec) * canvasH;
            if (barH < 1) barH = 1;

            string fill = hours[h] > 1800 ? "#4ade80" : hours[h] > 600 ? "#60a5fa" : "#1f2937";

            var rect = new Rectangle
            {
                Width   = barW,
                Height  = barH,
                Fill    = new SolidColorBrush((Color)ColorConverter.ConvertFromString(fill)),
                RadiusX = 2,
                RadiusY = 2,
            };
            System.Windows.Controls.Canvas.SetLeft(rect, h * (barW + 1));
            System.Windows.Controls.Canvas.SetBottom(rect, 0);
            ActivityCanvas.Children.Add(rect);
        }
    }

    // ── Helpers ──────────────────────────────────────────────────────────────

    private static string FormatTime(int seconds)
    {
        if (seconds <= 0) return "0m";
        int h = seconds / 3600, m = (seconds % 3600) / 60;
        return h > 0 ? $"{h}h {m}m" : $"{m}m";
    }

    private void PositionNearTray()
    {
        var wa = SystemParameters.WorkArea;
        Left = wa.Right  - Width  - 12;
        Top  = wa.Bottom - Height - 12;
    }

    private void CloseBtn_Click(object sender, RoutedEventArgs e)
    {
        _autoRefresh.Stop();
        Close();
    }

    private async void RefreshBtn_Click(object sender, RoutedEventArgs e)
        => await RefreshDataAsync();

    protected override void OnDeactivated(EventArgs e)
    {
        base.OnDeactivated(e);
        // Auto-close when user clicks elsewhere (like a tray popup)
        Close();
    }
}
