using System.Text.Json.Serialization;

namespace TelemetryAgent.Data;

public sealed class TelemetryEvent
{
    [JsonPropertyName("app")]       public string App       { get; init; } = "Unknown";
    [JsonPropertyName("domain")]    public string Domain    { get; init; } = string.Empty;
    [JsonPropertyName("active")]    public bool   Active    { get; init; }
    [JsonPropertyName("locked")]    public bool   Locked    { get; init; }
    [JsonPropertyName("duration")]  public int    Duration  { get; init; }
    [JsonPropertyName("timestamp")] public string Timestamp { get; init; } = string.Empty;
}

public sealed class IngestPayload
{
    [JsonPropertyName("user")]   public string User   { get; init; } = string.Empty;
    [JsonPropertyName("device")] public string Device { get; init; } = string.Empty;
    [JsonPropertyName("events")] public IReadOnlyList<TelemetryEvent> Events { get; init; } = [];
}

// ── Server API response shapes ───────────────────────────────────────────────

public sealed class SummaryResponse
{
    [JsonPropertyName("total_active_time")]     public int    TotalActiveTime    { get; init; }
    [JsonPropertyName("total_idle_time")]       public int    TotalIdleTime      { get; init; }
    [JsonPropertyName("total_screen_off_time")] public int    TotalScreenOffTime { get; init; }
    [JsonPropertyName("productivity_score")]    public double ProductivityScore  { get; init; }
    [JsonPropertyName("top_app")]               public string TopApp             { get; init; } = string.Empty;
}

public sealed class TimelineEntry
{
    [JsonPropertyName("timestamp")]      public string Timestamp     { get; init; } = string.Empty;
    [JsonPropertyName("last_timestamp")] public string LastTimestamp { get; init; } = string.Empty;
    [JsonPropertyName("app")]            public string App           { get; init; } = string.Empty;
    [JsonPropertyName("active")]         public bool   Active        { get; init; }
    [JsonPropertyName("locked")]         public bool   Locked        { get; init; }
    [JsonPropertyName("duration")]       public int    Duration      { get; init; }
}

public sealed class AppEntry
{
    [JsonPropertyName("app")]      public string App      { get; init; } = string.Empty;
    [JsonPropertyName("time")]     public int    Time     { get; init; }
    [JsonPropertyName("category")] public string Category { get; init; } = "Productive";
}
