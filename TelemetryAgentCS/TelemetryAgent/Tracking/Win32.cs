using System.Runtime.InteropServices;

namespace TelemetryAgent.Tracking;

internal static class Win32
{
    // ── Foreground window ────────────────────────────────────────────────────
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, out int lpdwProcessId);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int GetWindowText(IntPtr hWnd, System.Text.StringBuilder lpString, int nMaxCount);

    // ── Idle / last-input ────────────────────────────────────────────────────
    [DllImport("user32.dll")] public static extern bool GetLastInputInfo(ref LASTINPUTINFO plii);
    [DllImport("kernel32.dll")] public static extern uint GetTickCount();

    [StructLayout(LayoutKind.Sequential)]
    public struct LASTINPUTINFO
    {
        public uint cbSize;
        public uint dwTime;
    }

    // ── WinEvent hook (instant app-switch notification) ──────────────────────
    public delegate void WinEventDelegate(IntPtr hWinEventHook, uint eventType,
        IntPtr hwnd, int idObject, int idChild, uint dwEventThread, uint dwmsEventTime);

    [DllImport("user32.dll")] public static extern IntPtr SetWinEventHook(
        uint eventMin, uint eventMax, IntPtr hmodWinEventProc,
        WinEventDelegate lpfnWinEventProc, uint idProcess, uint idThread, uint dwFlags);

    [DllImport("user32.dll")] public static extern bool UnhookWinEvent(IntPtr hWinEventHook);

    public const uint EVENT_SYSTEM_FOREGROUND = 0x0003;
    public const uint WINEVENT_OUTOFCONTEXT   = 0x0000;

    // ── Desktop / lock detection ─────────────────────────────────────────────
    [DllImport("user32.dll")] public static extern IntPtr OpenInputDesktop(uint dwFlags, bool fInherit, uint dwDesiredAccess);
    [DllImport("user32.dll")] public static extern bool CloseDesktop(IntPtr hDesktop);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)]
    public static extern bool GetUserObjectInformation(IntPtr hObj, int nIndex, System.Text.StringBuilder pvInfo, uint nLength, out uint lpnLengthNeeded);

    public const int UOI_NAME = 2;
    public const uint DESKTOP_READOBJECTS = 0x0001;
}
