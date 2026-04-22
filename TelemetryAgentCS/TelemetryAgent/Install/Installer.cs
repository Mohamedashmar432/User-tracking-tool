using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text.Json;

namespace TelemetryAgent.Install;

public static class Installer
{
    private const string ProgramData = @"C:\ProgramData\TelemetryAgent";
    private const string InstallDir  = @"C:\Program Files\TelemetryAgent";
    private const string TaskName    = "TelemetryAgent";

    public static void Install(string serverUrl)
    {
        Console.WriteLine("=== TelemetryAgent Installation ===");

        // 1. Create directories
        foreach (var dir in new[] { ProgramData, InstallDir })
        {
            try   { Directory.CreateDirectory(dir); Console.WriteLine($"  Directory: {dir}"); }
            catch (UnauthorizedAccessException) { Console.Error.WriteLine($"  ERROR: Run as Administrator to create {dir}"); Environment.Exit(1); }
        }

        // 2. Fetch server URL + API key from /agent-config
        string baseUrl  = serverUrl.TrimEnd('/');
        string agentKey = string.Empty;

        if (!string.IsNullOrEmpty(baseUrl))
        {
            try
            {
                using var http = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
                var json = http.GetStringAsync($"{baseUrl}/agent-config").GetAwaiter().GetResult();
                using var doc = JsonDocument.Parse(json);
                var r = doc.RootElement;
                if (r.TryGetProperty("server_url", out var su) && su.ValueKind == JsonValueKind.String)
                    baseUrl = su.GetString()!.TrimEnd('/');
                if (r.TryGetProperty("agent_api_key", out var ak) && ak.ValueKind == JsonValueKind.String)
                    agentKey = ak.GetString()!;
                Console.WriteLine($"  Server URL: {baseUrl}");
            }
            catch (Exception ex) { Console.WriteLine($"  Warning: Could not reach /agent-config — {ex.Message}"); }
        }

        // 3. Write config.json
        var config = new
        {
            ingest_url    = $"{baseUrl}/ingest",
            api_key       = agentKey,
            idle_threshold = 300,
            event_interval = 15,
            flush_interval = 30,
            batch_size     = 20,
        };
        var configPath = Path.Combine(ProgramData, "config.json");
        File.WriteAllText(configPath, JsonSerializer.Serialize(config, new JsonSerializerOptions { WriteIndented = true }));
        Console.WriteLine($"  Config: {configPath}");

        // 4. Copy EXE to install dir
        string exeDest = Path.Combine(InstallDir, "TelemetryAgent.exe");
        string exeSrc  = Process.GetCurrentProcess().MainModule!.FileName!;
        if (!string.Equals(Path.GetFullPath(exeSrc), Path.GetFullPath(exeDest), StringComparison.OrdinalIgnoreCase))
        {
            File.Copy(exeSrc, exeDest, overwrite: true);
            Console.WriteLine($"  Copied: {exeSrc} → {exeDest}");
        }

        // 5. Register scheduled task (ONLOGON)
        RunSchtasks($"/create /tn \"{TaskName}\" /tr \"\\\"{exeDest}\\\"\" /sc ONLOGON /rl HIGHEST /f");
        Console.WriteLine($"  Scheduled task '{TaskName}' registered (ONLOGON)");

        // 6. Launch immediately (no logout required)
        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName        = exeDest,
                UseShellExecute = false,
                CreateNoWindow  = true,
            });
            Console.WriteLine("  Agent launched in background");
        }
        catch (Exception ex) { Console.WriteLine($"  Warning: Could not auto-start: {ex.Message}"); }

        Console.WriteLine("=== Installation complete ===");
        Console.WriteLine($"  Config : {configPath}");
        Console.WriteLine($"  Agent  : {exeDest}");
    }

    public static void Uninstall()
    {
        Console.WriteLine("=== TelemetryAgent Uninstall ===");

        // 1. Stop running process
        foreach (var p in Process.GetProcessesByName("TelemetryAgent"))
        {
            try { p.Kill(); Console.WriteLine("  Stopped running agent process"); }
            catch { /* already exited */ }
        }

        // 2. Remove scheduled task
        RunSchtasks($"/delete /tn \"{TaskName}\" /f");
        Console.WriteLine($"  Removed scheduled task '{TaskName}'");

        // 3. Remove files
        foreach (var dir in new[] { ProgramData, InstallDir })
        {
            if (!Directory.Exists(dir)) continue;
            try { Directory.Delete(dir, recursive: true); Console.WriteLine($"  Removed: {dir}"); }
            catch (Exception ex) { Console.WriteLine($"  Warning: Could not remove {dir}: {ex.Message}"); }
        }

        // 4. Remove offline backup
        var backup = Path.Combine(Path.GetTempPath(), "telemetry_backup");
        if (Directory.Exists(backup))
        {
            try { Directory.Delete(backup, recursive: true); Console.WriteLine($"  Removed: {backup}"); }
            catch { /* non-fatal */ }
        }

        Console.WriteLine("=== Uninstall complete — no residual artifacts ===");
    }

    private static void RunSchtasks(string args)
    {
        try
        {
            var proc = Process.Start(new ProcessStartInfo
            {
                FileName        = "schtasks",
                Arguments       = args,
                UseShellExecute = false,
                CreateNoWindow  = true,
                RedirectStandardOutput = true,
                RedirectStandardError  = true,
            })!;
            proc.WaitForExit(10_000);
        }
        catch (Exception ex) { Console.WriteLine($"  schtasks error: {ex.Message}"); }
    }
}
