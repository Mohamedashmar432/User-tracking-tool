"""
mac_telemetry_ui.py — macOS terminal dashboard for telemetry agent.

Lightweight alternative to tkinter/pystray. Reads the same status.json and cache.json
files written by mac_telemetry_agent.py, displays live terminal dashboard with rich.

IPC contract (unchanged):
  ~/Library/Application Support/TelemetryAgent/status.json  — agent state every ~5 s
  ~/Library/Application Support/TelemetryAgent/cache.json   — daily summary every ~60 s

Dependencies:
    pip install rich
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    from rich.console import Console
    from rich.layout import Layout
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich import box
except ImportError:
    print(
        "[error] rich is not installed.\n"
        "  Install with: pip install rich",
        file=sys.stderr,
    )
    sys.exit(1)

# ── macOS paths ───────────────────────────────────────────────────────────────────
_HOME = os.path.expanduser("~")
DATA_DIR = os.path.join(_HOME, "Library", "Application Support", "TelemetryAgent")
STATUS_PATH = os.path.join(DATA_DIR, "status.json")
CACHE_PATH = os.path.join(DATA_DIR, "cache.json")

# ── Theme ─────────────────────────────────────────────────────────────────────────
STYLE_ACTIVE = "bold green"
STYLE_IDLE = "dim yellow"
STYLE_LOCKED = "bold red"
STYLE_HEADER = "bold cyan"
STYLE_TIME = "cyan"
STYLE_PERCENT = "yellow"


def _format_duration(seconds: int) -> str:
    """Convert seconds to h:mm:ss format."""
    if seconds <= 0:
        return "0h 00m"
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def _read_status() -> dict:
    """Read latest agent status. Returns empty dict if missing/stale."""
    try:
        if not os.path.exists(STATUS_PATH):
            return {}
        with open(STATUS_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _read_cache() -> dict:
    """Read latest daily cache. Returns empty dict if missing."""
    try:
        if not os.path.exists(CACHE_PATH):
            return {}
        with open(CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def _is_agent_online(status: dict, cache: dict) -> bool:
    """Check if agent is running (recent updates)."""
    if not status or not cache:
        return False
    try:
        status_ts = datetime.fromisoformat(status.get("timestamp", ""))
        now = datetime.now(timezone.utc)
        # Agent offline if no update in 35 seconds (longer than log interval)
        return (now - status_ts).total_seconds() < 35
    except Exception:
        return False


def _build_header(status: dict, cache: dict) -> Panel:
    """Build header panel with user/device info and agent status."""
    username = cache.get("username", "Unknown")
    device = cache.get("device", "Unknown")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    online = _is_agent_online(status, cache)
    status_indicator = "[bold green]●[/bold green] ONLINE" if online else "[bold red]●[/bold red] OFFLINE"

    header_text = f"[{STYLE_HEADER}]TelemetryAgent[/] {status_indicator}\n"
    header_text += f"User: {username:20s}  Device: {device:20s}  {now}"

    return Panel(
        header_text,
        style="blue",
        expand=True,
    )


def _build_now_row(status: dict) -> Panel:
    """Build 'NOW' row showing current state."""
    if not status:
        now_text = "No data"
    else:
        state = "ACTIVE" if status.get("active") else "IDLE"
        state_style = STYLE_ACTIVE if status.get("active") else STYLE_IDLE

        if status.get("locked"):
            state = "LOCKED"
            state_style = STYLE_LOCKED

        app = status.get("app", "Unknown")
        idle = status.get("idle_seconds", 0)

        now_text = (
            f"[{state_style}]{state}[/]  "
            f"App: [bold]{app}[/bold]  "
            f"Idle: {idle}s"
        )

    return Panel(
        now_text,
        title="NOW",
        style="cyan",
        expand=True,
    )


def _build_summary_row(cache: dict) -> Panel:
    """Build summary with active/idle/locked/session time."""
    if not cache:
        summary = Table(show_header=False, expand=True)
        for label in ["Active", "Idle", "Locked", "Session"]:
            summary.add_column(label, justify="center", style="dim")
        summary.add_row("–", "–", "–", "–")
    else:
        summary_data = cache.get("summary", {})
        active_secs = summary_data.get("active_seconds", 0)
        idle_secs = summary_data.get("idle_seconds", 0)
        locked_secs = summary_data.get("locked_seconds", 0)
        session_secs = summary_data.get("session_elapsed", 0)

        summary = Table(show_header=False, expand=True, box=box.SIMPLE)
        summary.add_column("Active", justify="center", style="green")
        summary.add_column("Idle", justify="center", style="dim yellow")
        summary.add_column("Locked", justify="center", style="red")
        summary.add_column("Session", justify="center", style="cyan")

        summary.add_row(
            _format_duration(active_secs),
            _format_duration(idle_secs),
            _format_duration(locked_secs),
            _format_duration(session_secs),
        )

    return Panel(
        summary,
        title="TODAY SUMMARY",
        style="cyan",
        expand=True,
    )


def _build_top_apps(cache: dict) -> Panel:
    """Build top apps table."""
    if not cache:
        apps_table = Table(show_header=False, expand=True)
        apps_table.add_row("No data")
    else:
        top_apps = cache.get("top_apps", [])

        if not top_apps:
            apps_table = Table(show_header=False, expand=True)
            apps_table.add_row("No activity today")
        else:
            apps_table = Table(
                "Rank", "App", "Time", "Percent",
                show_header=True,
                expand=True,
                box=box.SIMPLE,
            )

            for i, app_info in enumerate(top_apps[:5], 1):
                app_name = app_info.get("app", "Unknown")
                duration = app_info.get("duration", 0)
                percent = app_info.get("percent", 0)

                # Build progress bar
                bar_width = 20
                filled = int(bar_width * percent / 100)
                bar = "█" * filled + "░" * (bar_width - filled)

                apps_table.add_row(
                    str(i),
                    app_name,
                    f"{bar}  {_format_duration(duration)}",
                    f"{percent}%",
                )

    return Panel(
        apps_table,
        title="TOP APPS TODAY",
        style="cyan",
        expand=True,
    )


def main() -> None:
    """Run live dashboard."""
    console = Console()

    # Clear screen once
    console.clear()

    try:
        while True:
            status = _read_status()
            cache = _read_cache()

            # Build layout
            layout = Layout()
            layout.split_column(
                Layout(name="header", size=3),
                Layout(name="now", size=3),
                Layout(name="summary", size=3),
                Layout(name="apps", size=15),
                Layout(name="footer", size=1),
            )

            layout["header"].update(_build_header(status, cache))
            layout["now"].update(_build_now_row(status))
            layout["summary"].update(_build_summary_row(cache))
            layout["apps"].update(_build_top_apps(cache))

            footer_text = "[dim]Press [bold]Ctrl+C[/] to quit[/]"
            layout["footer"].update(Text(footer_text, style="dim"))

            # Print layout
            console.print(layout, end="")

            # Refresh every 5 seconds
            time.sleep(5)
    except KeyboardInterrupt:
        console.print("\n[yellow]Dashboard closed.[/]")
        sys.exit(0)


if __name__ == "__main__":
    main()
