using System.IO;
using System.Text.Json;

namespace TelemetryAgent.Data;

/// <summary>
/// Persists failed batches to %TEMP%/telemetry_backup/{user}/ and replays them
/// oldest-first when the server becomes reachable. Mirrors Python agent behaviour.
/// </summary>
public sealed class BackupManager
{
    private const int MaxBackupEvents = 100;

    private string BackupDir(string user)
    {
        var dir = Path.Combine(Path.GetTempPath(), "telemetry_backup", user);
        Directory.CreateDirectory(dir);
        return dir;
    }

    private IEnumerable<string> BackupFiles(string user)
        => Directory.GetFiles(BackupDir(user), "batch_*.json").OrderBy(f => f);

    public void Save(string user, string device, IReadOnlyList<TelemetryEvent> events)
    {
        if (events.Count == 0) return;
        var files = BackupFiles(user).ToList();

        // Evict oldest files until adding `events` stays within cap
        int total = files.Sum(f =>
        {
            try { return JsonSerializer.Deserialize<IngestPayload>(File.ReadAllText(f))?.Events.Count ?? 0; }
            catch { return 0; }
        });

        foreach (var f in files)
        {
            if (total + events.Count <= MaxBackupEvents) break;
            try
            {
                int n = JsonSerializer.Deserialize<IngestPayload>(File.ReadAllText(f))?.Events.Count ?? 0;
                File.Delete(f);
                total -= n;
            }
            catch { /* skip */ }
        }

        var ts    = DateTime.UtcNow.ToString("yyyyMMddTHHmmssffffff");
        var path  = Path.Combine(BackupDir(user), $"batch_{ts}.json");
        var payload = new IngestPayload { User = user, Device = device, Events = events };
        File.WriteAllText(path, JsonSerializer.Serialize(payload));
    }

    public async Task<int> ReplayAsync(string user, string device, ApiClient client)
    {
        int recovered = 0;
        foreach (var file in BackupFiles(user))
        {
            IngestPayload? payload;
            try { payload = JsonSerializer.Deserialize<IngestPayload>(File.ReadAllText(file)); }
            catch { continue; }

            if (payload is null || payload.Events.Count == 0) { File.Delete(file); continue; }

            bool ok = await client.PostEventsAsync(
                payload.User.Length > 0 ? payload.User : user,
                payload.Device.Length > 0 ? payload.Device : device,
                payload.Events);

            if (ok) { File.Delete(file); recovered += payload.Events.Count; }
            else    break; // server still down — stop, keep remaining files
        }
        return recovered;
    }
}
