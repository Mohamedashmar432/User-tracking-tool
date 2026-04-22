using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;

namespace TelemetryAgent.Data;

public sealed class ApiClient : IDisposable
{
    private readonly HttpClient _http;
    private readonly Config _cfg;

    public ApiClient(Config cfg)
    {
        _cfg  = cfg;
        _http = new HttpClient { BaseAddress = new Uri(cfg.ServerBaseUrl), Timeout = TimeSpan.FromSeconds(10) };
        if (!string.IsNullOrEmpty(cfg.ApiKey))
            _http.DefaultRequestHeaders.Add("X-API-Key", cfg.ApiKey);
    }

    public async Task<bool> PostEventsAsync(string user, string device, IReadOnlyList<TelemetryEvent> events)
    {
        if (events.Count == 0) return true;
        try
        {
            var payload  = new IngestPayload { User = user, Device = device, Events = events };
            var response = await _http.PostAsJsonAsync("/ingest", payload);
            return response.IsSuccessStatusCode;
        }
        catch { return false; }
    }

    public async Task<SummaryResponse?> GetSummaryAsync(string user, string date)
    {
        try
        {
            return await _http.GetFromJsonAsync<SummaryResponse>(
                $"/api/summary?user={Uri.EscapeDataString(user)}&date={date}",
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
        }
        catch { return null; }
    }

    public async Task<List<TimelineEntry>> GetTimelineAsync(string user, string date)
    {
        try
        {
            return await _http.GetFromJsonAsync<List<TimelineEntry>>(
                $"/api/timeline?user={Uri.EscapeDataString(user)}&date={date}",
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? [];
        }
        catch { return []; }
    }

    public async Task<List<AppEntry>> GetAppsAsync(string user, string date)
    {
        try
        {
            return await _http.GetFromJsonAsync<List<AppEntry>>(
                $"/api/apps?user={Uri.EscapeDataString(user)}&date={date}",
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true }) ?? [];
        }
        catch { return []; }
    }

    public void Dispose() => _http.Dispose();
}
