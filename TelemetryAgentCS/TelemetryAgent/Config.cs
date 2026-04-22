using System.IO;
using System.Text.Json;

namespace TelemetryAgent;

public sealed class Config
{
    public string IngestUrl       { get; init; } = "http://localhost:8000/ingest";
    public string ApiKey          { get; init; } = string.Empty;
    public string AdminApiKey     { get; init; } = string.Empty;
    public int    IdleThresholdSec { get; init; } = 300;
    public int    EventIntervalSec { get; init; } = 15;    // faster than Python's 60s
    public int    FlushIntervalSec { get; init; } = 30;    // faster than Python's 120s
    public int    BatchSize        { get; init; } = 20;
    public string Username         { get; init; } = Environment.UserName;
    public string Device           { get; init; } = Environment.MachineName;

    public string ServerBaseUrl =>
        IngestUrl.Contains("/ingest", StringComparison.OrdinalIgnoreCase)
            ? IngestUrl[..IngestUrl.LastIndexOf("/ingest", StringComparison.OrdinalIgnoreCase)]
            : IngestUrl.TrimEnd('/');

    // Derived: admin-facing API base (same host, /api prefix)
    public string ApiBase => ServerBaseUrl;

    public static Config Load()
    {
        var candidates = new[]
        {
            @"C:\ProgramData\TelemetryAgent\config.json",
            Path.Combine(AppContext.BaseDirectory, "agent.config.json"),
        };

        foreach (var path in candidates)
        {
            if (!File.Exists(path)) continue;
            try
            {
                var raw = File.ReadAllText(path);
                using var doc = JsonDocument.Parse(raw);
                var r = doc.RootElement;
                return new Config
                {
                    IngestUrl        = r.TryGet("ingest_url",       out var iu) ? iu!  : "http://localhost:8000/ingest",
                    ApiKey           = r.TryGet("api_key",          out var ak) ? ak!  : string.Empty,
                    AdminApiKey      = r.TryGet("admin_api_key",    out var aa) ? aa!  : string.Empty,
                    IdleThresholdSec = r.TryGetInt("idle_threshold", 300),
                    EventIntervalSec = r.TryGetInt("event_interval", 15),
                    FlushIntervalSec = r.TryGetInt("flush_interval", 30),
                    BatchSize        = r.TryGetInt("batch_size",     20),
                    Username         = r.TryGet("username",         out var un) ? un!  : Environment.UserName,
                    Device           = r.TryGet("device",           out var dv) ? dv!  : Environment.MachineName,
                };
            }
            catch { /* malformed config — try next */ }
        }

        return new Config(); // all defaults
    }
}

internal static class JsonElementExt
{
    public static bool TryGet(this JsonElement el, string name, out string? value)
    {
        if (el.TryGetProperty(name, out var p) && p.ValueKind == JsonValueKind.String)
        { value = p.GetString(); return true; }
        value = null; return false;
    }

    public static int TryGetInt(this JsonElement el, string name, int fallback)
        => el.TryGetProperty(name, out var p) && p.TryGetInt32(out int v) ? v : fallback;
}
