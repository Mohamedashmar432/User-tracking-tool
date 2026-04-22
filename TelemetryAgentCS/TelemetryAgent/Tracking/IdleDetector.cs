namespace TelemetryAgent.Tracking;

public static class IdleDetector
{
    /// <summary>
    /// Returns seconds since last keyboard or mouse input for this session.
    /// Uses the unsigned 32-bit tick-count arithmetic to handle wrap-around safely.
    /// </summary>
    public static int IdleSeconds()
    {
        try
        {
            var lii = new Win32.LASTINPUTINFO { cbSize = (uint)System.Runtime.InteropServices.Marshal.SizeOf<Win32.LASTINPUTINFO>() };
            if (!Win32.GetLastInputInfo(ref lii)) return 0;
            uint elapsed = (Win32.GetTickCount() - lii.dwTime) & 0xFFFF_FFFF;
            return (int)(elapsed / 1000);
        }
        catch { return 0; }
    }

    public static bool IsIdle(int thresholdSeconds) => IdleSeconds() >= thresholdSeconds;
}
