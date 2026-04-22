"""
telemetry_ui.py — Lightweight Windows system-tray UI companion for TelemetryAgent.

Architecture
------------
  Agent (telemetry_agent.exe)
    └─ writes  C:\\ProgramData\\TelemetryAgent\\status.json   every ~5 s  (current state)
    └─ writes  C:\\ProgramData\\TelemetryAgent\\cache.json    every ~15 s (daily summary)

  This process (telemetry_ui.exe)
    └─ reads status.json + cache.json  → primary data source (works offline)
    └─ calls server /api/*             → richer data when reachable
    └─ shows system-tray icon + popup window

Dependencies (install in the build venv):
    pip install pystray pillow requests

Build:
    pyinstaller telemetry_ui.spec
"""

from __future__ import annotations

import json
import math
import os
import queue
import sys
import threading
import time
import tkinter as tk
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pystray
import requests
from PIL import Image, ImageDraw

# ── Paths (shared with agent) ────────────────────────────────────────────────
PROGRAM_DATA = r"C:\ProgramData\TelemetryAgent"
STATUS_PATH  = os.path.join(PROGRAM_DATA, "status.json")
CACHE_PATH   = os.path.join(PROGRAM_DATA, "cache.json")
CONFIG_PATH  = os.path.join(PROGRAM_DATA, "config.json")

# ── Config ───────────────────────────────────────────────────────────────────
def _load_config() -> dict:
    for path in [CONFIG_PATH,
                 os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent.config.json")]:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            continue
    return {}

_cfg          = _load_config()
_SERVER_BASE  = (_cfg.get("ingest_url", "") or "").replace("/ingest", "").rstrip("/")
_DEVICE_KEY   = _cfg.get("api_key", "")   # per-user device key — same key the agent uses for /ingest
_AUTO_REFRESH = 30   # seconds between auto-refreshes
# NOTE: no admin key is read or stored here — the device key is sufficient for /api/me/*

# ── Thread-safe message queue (tray thread → tkinter main thread) ────────────
_ui_queue: queue.Queue = queue.Queue()

# ── Colors ───────────────────────────────────────────────────────────────────
BG       = "#ffffff"
BG2      = "#f8fafc"
BORDER   = "#e2e8f0"
TEXT     = "#1e293b"
MUTED    = "#64748b"
GREEN    = "#16a34a"
RED      = "#dc2626"
BLUE     = "#3b82f6"
YELLOW   = "#d97706"


# ═══════════════════════════════════════════════════════════════════════════════
# Data layer
# ═══════════════════════════════════════════════════════════════════════════════

def read_local() -> tuple[dict | None, dict | None]:
    """Read status.json + cache.json. Returns (status, cache) — either may be None."""
    status = cache = None
    try:
        with open(STATUS_PATH, encoding="utf-8") as f:
            status = json.load(f)
    except Exception:
        pass
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            cache = json.load(f)
    except Exception:
        pass
    return status, cache


def fetch_server(date_str: str) -> tuple[dict | None, list, list]:
    """
    GET /api/me/summary, /api/me/apps, /api/me/timeline from server.

    Uses the per-user device key (same key the agent uses for POST /ingest).
    The server scopes the response to the key owner automatically — no username
    parameter is sent and no admin key is required.
    Falls back gracefully to (None, [], []) when unreachable.
    """
    if not _SERVER_BASE or not _DEVICE_KEY:
        return None, [], []

    headers = {"X-API-Key": _DEVICE_KEY}
    base    = _SERVER_BASE

    def _get(path: str, default):
        try:
            r = requests.get(f"{base}{path}?date={date_str}",
                             headers=headers, timeout=5)
            return r.json() if r.ok else default
        except Exception:
            return default

    summary  = _get("/api/me/summary",  None)
    apps     = _get("/api/me/apps",     [])
    timeline = _get("/api/me/timeline", [])
    return summary, apps, timeline


def build_display_data(date_str: str | None = None) -> dict[str, Any]:
    """
    Merge local cache + server data into a single dict for the popup.
    For today: local files are the fast path; server enriches when reachable.
    For past dates: server only (local files only hold today's state).
    """
    today    = datetime.now(timezone.utc).date().isoformat()
    if not date_str:
        date_str = today
    is_today = (date_str == today)

    status, cache = (read_local() if is_today else (None, None))
    summary, apps, timeline = fetch_server(date_str)

    # ── Live status (today only) ───────────────────────────────────────────
    if is_today:
        app       = (status or {}).get("app",     "Unknown")
        active    = (status or {}).get("active",  False)
        locked    = (status or {}).get("locked",  False)
        status_ts = (status or {}).get("timestamp", "")
        if locked:
            status_label, status_color = "Away",   RED
        elif not active:
            status_label, status_color = "Idle",   YELLOW
        else:
            status_label, status_color = "Active", GREEN
    else:
        app = "—"
        status_label, status_color = "Historical", MUTED
        status_ts = ""

    # ── Summary (server preferred; cache fallback only for today) ──────────
    if summary is None and is_today and cache:
        summary = cache.get("summary")

    active_secs = (summary or {}).get("total_active_time", 0)
    idle_secs   = (summary or {}).get("total_idle_time",   0)
    score       = (summary or {}).get("productivity_score", 0.0)
    top_app     = (summary or {}).get("top_app", "—")

    # ── Top apps (server preferred; cache fallback only for today) ─────────
    if not apps and is_today and cache:
        apps = cache.get("top_apps", [])

    prod_secs   = sum(a.get("time", 0) for a in apps if a.get("category") == "Productive")
    unprod_secs = sum(a.get("time", 0) for a in apps if a.get("category") == "Unproductive")

    # ── Hourly activity ────────────────────────────────────────────────────
    if timeline:
        hourly = _timeline_to_hourly(timeline)
    elif is_today and cache:
        hourly = cache.get("hourly_active", [0] * 24)
    else:
        hourly = [0] * 24

    # ── Last-updated label ─────────────────────────────────────────────────
    server_ok = summary is not None and bool(_SERVER_BASE)
    if not is_today:
        last_updated = f"Server — {date_str}"
    elif server_ok:
        last_updated = f"Server  {datetime.now().strftime('%H:%M:%S')}"
    elif status_ts:
        try:
            ts = datetime.fromisoformat(status_ts).astimezone()
            last_updated = f"Local cache  {ts.strftime('%H:%M:%S')}"
        except Exception:
            last_updated = "Local cache"
    else:
        last_updated = "No data"

    return {
        "app":          app,
        "is_today":     is_today,
        "status_label": status_label,
        "status_color": status_color,
        "score":        score,
        "active_secs":  active_secs + idle_secs,   # screen-on time
        "idle_secs":    idle_secs,
        "top_app":      top_app.replace(".exe", ""),
        "prod_secs":    prod_secs,
        "unprod_secs":  unprod_secs,
        "hourly":       hourly,
        "top_apps":     apps[:6],
        "last_updated": last_updated,
        "server_ok":    server_ok,
    }


def _timeline_to_hourly(timeline: list) -> list[int]:
    """Distribute active durations from a timeline into 24 hourly buckets."""
    hourly = [0] * 24
    for entry in timeline:
        if not entry.get("active"):
            continue
        try:
            ts = datetime.fromisoformat(entry["timestamp"]).astimezone()
            h  = ts.hour
            hourly[h] = min(hourly[h] + entry.get("duration", 0), 3600)
        except Exception:
            pass
    return hourly


# ═══════════════════════════════════════════════════════════════════════════════
# Popup window
# ═══════════════════════════════════════════════════════════════════════════════

class PopupWindow:
    W, H = 320, 570

    def __init__(self, root: tk.Tk):
        self._root = root
        self._win  = tk.Toplevel(root)
        self._win.overrideredirect(True)       # no title bar
        self._win.configure(bg=BG)
        self._win.attributes("-topmost", True)
        self._win.attributes("-alpha", 0.99)
        self._selected_date: str = datetime.now(timezone.utc).date().isoformat()
        self._position()
        self._build_ui()
        self._drag_start: tuple[int, int] | None = None
        self._win.bind("<ButtonPress-1>",   self._on_drag_start)
        self._win.bind("<B1-Motion>",       self._on_drag_move)
        self._win.bind("<FocusOut>",        lambda _: self.close())
        self._refresh_job: str | None = None
        self.refresh()

    # ── Layout ────────────────────────────────────────────────────────────────
    def _build_ui(self):
        w = self._win

        # ── Fixed header (always visible) ─────────────────────────────────
        hdr = tk.Frame(w, bg=BG2, height=46)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        self._dot = tk.Label(hdr, text="●", fg=GREEN, bg=BG2, font=("Segoe UI", 12))
        self._dot.place(x=14, y=13)
        self._status_lbl = tk.Label(hdr, text="Active", fg=TEXT, bg=BG2,
                                    font=("Segoe UI", 11, "bold"))
        self._status_lbl.place(x=34, y=11)
        tk.Button(hdr, text="✕", fg=MUTED, bg=BG2, bd=0, activebackground=BG2,
                  activeforeground=TEXT, font=("Segoe UI", 12), cursor="hand2",
                  command=self.close).place(x=290, y=8)

        # ── Date nav bar (always visible) ─────────────────────────────────
        nav = tk.Frame(w, bg=BG2)
        nav.pack(fill="x")
        self._cal_btn = tk.Button(
            nav, text="📅  Today", fg=TEXT, bg=BG2, bd=0,
            activebackground=BG2, activeforeground=BLUE,
            font=("Segoe UI", 9, "bold"), cursor="hand2",
            anchor="center", command=self._toggle_calendar,
        )
        self._cal_btn.pack(side="left", expand=True, padx=8, pady=5)
        self._today_btn = tk.Button(
            nav, text="← Today", fg=BLUE, bg=BG2, bd=0,
            activebackground=BG2, activeforeground=BLUE,
            font=("Segoe UI", 8), cursor="hand2",
            command=self._go_today,
        )
        # hidden until navigated to a past date
        tk.Frame(w, bg=BORDER, height=1).pack(fill="x")

        # ── Footer (always visible, anchored to bottom) ───────────────────
        ftr = tk.Frame(w, bg=BG2, height=36)
        ftr.pack(fill="x", side="bottom")
        ftr.pack_propagate(False)
        self._last_upd = tk.Label(ftr, text="Refreshing…", fg=MUTED, bg=BG2,
                                  font=("Segoe UI", 8))
        self._last_upd.place(x=14, y=10)
        tk.Button(ftr, text="↻", fg=MUTED, bg=BG2, bd=0,
                  activebackground=BG2, activeforeground=TEXT,
                  font=("Segoe UI", 12), cursor="hand2",
                  command=self.refresh).place(x=292, y=4)

        # ── Inline calendar panel (hidden; swaps with body on demand) ─────
        self._cal_frame = tk.Frame(w, bg=BG)
        self._cal_visible = False
        self._cal_year  = 0
        self._cal_month = 0
        self._cal_day_btns: list[tk.Button] = []
        self._build_calendar_panel()

        # ── Body (stats) ───────────────────────────────────────────────────
        self._body_frame = tk.Frame(w, bg=BG)
        self._body_frame.pack(fill="both", expand=True)
        body = self._body_frame

        self._donut_cv = tk.Canvas(body, width=130, height=130, bg=BG,
                                   highlightthickness=0)
        self._donut_cv.pack(pady=(16, 2))

        legend_row = tk.Frame(body, bg=BG)
        legend_row.pack(pady=(0, 4))
        self._prod_legend   = tk.Label(legend_row, text="● 0h Productive",
                                       fg=GREEN, bg=BG, font=("Segoe UI", 8, "bold"))
        self._prod_legend.pack(side="left", padx=6)
        self._unprod_legend = tk.Label(legend_row, text="● 0h Unproductive",
                                       fg=RED, bg=BG, font=("Segoe UI", 8, "bold"))
        self._unprod_legend.pack(side="left", padx=6)

        kpi = tk.Frame(body, bg=BG)
        kpi.pack(fill="x", padx=16)
        self._kpi_labels: dict[str, tk.Label] = {}
        for col, (key, label) in enumerate([("active_secs", "Active"),
                                             ("top_app",     "Top App"),
                                             ("idle_secs",   "Idle")]):
            cell = tk.Frame(kpi, bg=BG)
            cell.grid(row=0, column=col, sticky="ew", padx=4)
            kpi.columnconfigure(col, weight=1)
            val = tk.Label(cell, text="—", fg=TEXT, bg=BG,
                           font=("Segoe UI", 11, "bold"))
            val.pack()
            tk.Label(cell, text=label, fg=MUTED, bg=BG,
                     font=("Segoe UI", 9)).pack()
            self._kpi_labels[key] = val

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=16, pady=10)

        tk.Label(body, text="24-Hour Activity", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        self._activity_cv = tk.Canvas(body, width=288, height=64, bg=BG,
                                      highlightthickness=0)
        self._activity_cv.pack(padx=16, pady=(4, 2))

        hour_row = tk.Frame(body, bg=BG)
        hour_row.pack(fill="x", padx=16)
        tk.Label(hour_row, text="12am", fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left")
        tk.Label(hour_row, text="12pm", fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(side="left", expand=True)
        tk.Label(hour_row, text="11pm", fg=MUTED, bg=BG,
                 font=("Segoe UI", 8)).pack(side="right")

        tk.Frame(body, bg=BORDER, height=1).pack(fill="x", padx=16, pady=10)

        tk.Label(body, text="Top Apps", fg=MUTED, bg=BG,
                 font=("Segoe UI", 9)).pack(anchor="w", padx=16)
        self._apps_frame = tk.Frame(body, bg=BG)
        self._apps_frame.pack(fill="x", padx=16, pady=(4, 0))

    def _build_calendar_panel(self):
        """Build the reusable inline calendar inside _cal_frame (not packed yet)."""
        import calendar as _cal_mod
        self._cal_mod = _cal_mod

        f = self._cal_frame

        # Month nav
        mnav = tk.Frame(f, bg=BG2)
        mnav.pack(fill="x", padx=12, pady=(10, 4))
        tk.Button(mnav, text="‹", fg=TEXT, bg=BG2, bd=0, activebackground=BG2,
                  activeforeground=BLUE, font=("Segoe UI", 14), cursor="hand2",
                  command=self._cal_prev_month).pack(side="left")
        tk.Button(mnav, text="›", fg=TEXT, bg=BG2, bd=0, activebackground=BG2,
                  activeforeground=BLUE, font=("Segoe UI", 14), cursor="hand2",
                  command=self._cal_next_month).pack(side="right")
        self._cal_hdr_lbl = tk.Label(mnav, text="", fg=TEXT, bg=BG2,
                                     font=("Segoe UI", 10, "bold"), width=14,
                                     anchor="center")
        self._cal_hdr_lbl.pack(side="left", expand=True)

        # Day-of-week header
        dow_row = tk.Frame(f, bg=BG)
        dow_row.pack(fill="x", padx=12)
        for d in ("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"):
            tk.Label(dow_row, text=d, fg=MUTED, bg=BG,
                     font=("Segoe UI", 8, "bold"), width=4,
                     anchor="center").pack(side="left", expand=True)

        tk.Frame(f, bg=BORDER, height=1).pack(fill="x", padx=12, pady=2)

        # 6-row × 7-col day grid (pre-allocated, reconfigured on month change)
        grid_f = tk.Frame(f, bg=BG)
        grid_f.pack(fill="x", padx=12, pady=(2, 8))
        for row in range(6):
            grid_f.rowconfigure(row, weight=1)
        for col in range(7):
            grid_f.columnconfigure(col, weight=1)

        self._cal_day_btns = []
        for row in range(6):
            for col in range(7):
                btn = tk.Button(
                    grid_f, text="", width=3, height=1, bd=0,
                    font=("Segoe UI", 9), cursor="hand2",
                    bg=BG, fg=TEXT,
                    activebackground=BLUE, activeforeground="#ffffff",
                    relief="flat",
                )
                btn.grid(row=row, column=col, padx=2, pady=2, sticky="nsew")
                self._cal_day_btns.append(btn)

        # Cancel button
        tk.Button(
            f, text="Cancel", bg=BG2, fg=MUTED, bd=0,
            activebackground=BG2, activeforeground=TEXT,
            font=("Segoe UI", 8), cursor="hand2",
            command=self._toggle_calendar,
        ).pack(pady=(0, 10))

    # ── Refresh ───────────────────────────────────────────────────────────────
    def refresh(self):
        if self._refresh_job:
            self._win.after_cancel(self._refresh_job)
        threading.Thread(target=self._fetch_and_update, daemon=True).start()
        self._refresh_job = self._win.after(_AUTO_REFRESH * 1000, self.refresh)

    def _fetch_and_update(self):
        try:
            data = build_display_data(self._selected_date)
            self._win.after(0, lambda: self._apply(data))
        except Exception:
            pass

    def _apply(self, d: dict):
        # Date nav bar
        is_today = d["is_today"]
        if is_today:
            self._cal_btn.configure(text="📅  Today")
            self._today_btn.pack_forget()
        else:
            try:
                from datetime import date as _date
                dt = _date.fromisoformat(self._selected_date)
                self._cal_btn.configure(text=f"📅  {dt.strftime('%b %d, %Y')}")
            except Exception:
                self._cal_btn.configure(text=f"📅  {self._selected_date}")
            self._today_btn.pack(side="right", padx=(0, 6), pady=5)

        # Header status
        self._dot.configure(fg=d["status_color"])
        self._status_lbl.configure(text=d["status_label"])

        # Donut
        self._draw_donut(d["prod_secs"], d["unprod_secs"])
        self._prod_legend.configure(text=f"● {_fmt_time(d['prod_secs'])} Productive")
        self._unprod_legend.configure(text=f"● {_fmt_time(d['unprod_secs'])} Unproductive")

        # KPIs
        self._kpi_labels["active_secs"].configure(text=_fmt_time(d["active_secs"]))
        self._kpi_labels["idle_secs"].configure(text=_fmt_time(d["idle_secs"]))
        app_short = (d["top_app"] or "—")[:12]
        self._kpi_labels["top_app"].configure(text=app_short)

        # 24h chart
        self._draw_activity(d["hourly"])

        # Top apps list
        for w in self._apps_frame.winfo_children():
            w.destroy()
        for entry in d["top_apps"][:5]:
            row = tk.Frame(self._apps_frame, bg=BG)
            row.pack(fill="x", pady=1)
            color = GREEN if entry.get("category") == "Productive" else RED
            tk.Label(row, text="●", fg=color, bg=BG,
                     font=("Segoe UI", 8)).pack(side="left")
            name = entry.get("app", "").replace(".exe", "")
            tk.Label(row, text=name, fg=TEXT, bg=BG,
                     font=("Segoe UI", 9), anchor="w").pack(side="left", padx=4)
            tk.Label(row, text=_fmt_time(entry.get("time", 0)), fg=MUTED, bg=BG,
                     font=("Segoe UI", 9)).pack(side="right")

        # Footer
        self._last_upd.configure(text=d["last_updated"])

    # ── Date navigation ───────────────────────────────────────────────────────
    def _toggle_calendar(self):
        """Show/hide the inline calendar, swapping it with the body frame."""
        if self._cal_visible:
            # Hide calendar, show body
            self._cal_frame.pack_forget()
            self._body_frame.pack(fill="both", expand=True)
            self._cal_visible = False
            # Restore FocusOut-to-close
            self._win.bind("<FocusOut>", lambda _: self.close())
        else:
            # Seed the calendar to the currently selected month
            from datetime import date as _date
            try:
                sel = _date.fromisoformat(self._selected_date)
            except Exception:
                sel = datetime.now(timezone.utc).date()
            self._cal_year  = sel.year
            self._cal_month = sel.month
            self._cal_refresh(selected_iso=self._selected_date)
            # Swap frames
            self._body_frame.pack_forget()
            self._cal_frame.pack(fill="both", expand=True)
            self._cal_visible = True
            # Disable FocusOut-to-close while calendar is open so clicking
            # a day button (which briefly changes focus) doesn't close the popup
            self._win.unbind("<FocusOut>")

    def _cal_refresh(self, selected_iso: str = ""):
        """Repopulate the 42 day-buttons for _cal_year / _cal_month."""
        import calendar as _cal_mod
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()

        self._cal_hdr_lbl.configure(
            text=_date(self._cal_year, self._cal_month, 1).strftime("%B %Y")
        )

        # Build a flat list of (day_number_or_0, date_obj_or_None) for 6×7 cells
        first_wd = _cal_mod.weekday(self._cal_year, self._cal_month, 1)  # 0=Mon
        days_in  = _cal_mod.monthrange(self._cal_year, self._cal_month)[1]
        cells: list[tuple[int, str | None]] = []
        cells.extend((0, None) for _ in range(first_wd))       # leading blanks
        for d in range(1, days_in + 1):
            iso = _date(self._cal_year, self._cal_month, d).isoformat()
            cells.append((d, iso))
        cells.extend((0, None) for _ in range(42 - len(cells)))  # trailing blanks

        for idx, btn in enumerate(self._cal_day_btns):
            day_num, iso = cells[idx]
            if day_num == 0:
                btn.configure(text="", state="disabled", bg=BG,
                              disabledforeground=BG, cursor="arrow")
                btn.config(command=lambda: None)
            else:
                is_today    = (iso == today.isoformat())
                is_selected = (iso == selected_iso)
                is_future   = (iso > today.isoformat())

                if is_future:
                    bg_c, fg_c, state = BG, BORDER, "disabled"
                elif is_selected:
                    bg_c, fg_c, state = BLUE, "#ffffff", "normal"
                elif is_today:
                    bg_c, fg_c, state = "#e0e7ff", BLUE, "normal"
                else:
                    bg_c, fg_c, state = BG, TEXT, "normal"

                btn.configure(text=str(day_num), state=state,
                              bg=bg_c, fg=fg_c,
                              disabledforeground=BORDER, cursor="hand2")
                if state == "normal":
                    btn.config(command=lambda d=iso: self._cal_pick(d))

    def _cal_prev_month(self):
        if self._cal_month == 1:
            self._cal_year  -= 1
            self._cal_month  = 12
        else:
            self._cal_month -= 1
        self._cal_refresh(selected_iso=self._selected_date)

    def _cal_next_month(self):
        from datetime import date as _date
        today = datetime.now(timezone.utc).date()
        next_y = self._cal_year  + (1 if self._cal_month == 12 else 0)
        next_m = 1 if self._cal_month == 12 else self._cal_month + 1
        if _date(next_y, next_m, 1) <= today:
            self._cal_year  = next_y
            self._cal_month = next_m
            self._cal_refresh(selected_iso=self._selected_date)

    def _cal_pick(self, iso: str):
        """User clicked a day — accept it, hide calendar, refresh data."""
        self._selected_date = iso
        self._toggle_calendar()   # hides calendar, shows body, restores FocusOut
        self.refresh()

    def _go_today(self):
        self._selected_date = datetime.now(timezone.utc).date().isoformat()
        if self._cal_visible:
            self._toggle_calendar()
        self.refresh()

    # ── Charts ────────────────────────────────────────────────────────────────
    def _draw_donut(self, prod: int, unprod: int):
        cv = self._donut_cv
        cv.delete("all")
        size   = 130
        cx, cy = size // 2, size // 2
        r_out  = 55   # ring thickness = 19 px

        total = prod + unprod
        if total <= 0:
            cv.create_oval(cx - r_out, cy - r_out, cx + r_out, cy + r_out,
                           outline=BORDER, width=19)
        else:
            prod_angle = prod / total * 360

            def arc(start, extent, color):
                cv.create_arc(cx - r_out, cy - r_out, cx + r_out, cy + r_out,
                              start=start, extent=extent,
                              outline=color, width=19, style="arc")

            arc(90, -360, RED)          # unproductive base ring
            if prod_angle > 0:
                arc(90, -prod_angle, GREEN)   # productive overlay

        # Center: show productive hours
        prod_text = _fmt_time(prod) if prod > 0 else "0m"
        cv.create_text(cx, cy - 9, text=prod_text,
                       fill=TEXT, font=("Segoe UI", 13, "bold"))
        cv.create_text(cx, cy + 10, text="productive",
                       fill=MUTED, font=("Segoe UI", 8))

    def _draw_activity(self, hourly: list[int]):
        cv  = self._activity_cv
        cv.delete("all")
        W, H = 288, 64
        max_v = max(hourly) if max(hourly) > 0 else 1
        bar_w = W / 24

        for h, val in enumerate(hourly):
            bar_h = max(2, (val / max_v) * (H - 4)) if val > 0 else 0
            x0 = h * bar_w + 1
            x1 = x0 + bar_w - 2
            y0 = H - bar_h
            y1 = H
            color = GREEN if val > 1800 else BLUE if val > 300 else "#cbd5e1"
            cv.create_rectangle(x0, y0, x1, y1, fill=color, outline="")

    # ── Window management ─────────────────────────────────────────────────────
    def _position(self):
        wa_x = self._root.winfo_screenwidth()
        wa_y = self._root.winfo_screenheight()
        x = wa_x - self.W - 14
        y = wa_y - self.H - 48   # 48 = approx taskbar height
        self._win.geometry(f"{self.W}x{self.H}+{x}+{y}")

    def _on_drag_start(self, e):
        self._drag_start = (e.x_root - self._win.winfo_x(),
                            e.y_root - self._win.winfo_y())

    def _on_drag_move(self, e):
        if self._drag_start:
            dx, dy = self._drag_start
            self._win.geometry(f"+{e.x_root - dx}+{e.y_root - dy}")

    def close(self):
        if self._refresh_job:
            self._win.after_cancel(self._refresh_job)
        self._win.destroy()


# ═══════════════════════════════════════════════════════════════════════════════
# System tray
# ═══════════════════════════════════════════════════════════════════════════════

def _make_tray_icon(color: str = GREEN) -> Image.Image:
    """Create a 64×64 donut icon. Colour reflects current status."""
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    r    = (color.lstrip("#"),)
    rgb  = tuple(int(color.lstrip("#")[i:i+2], 16) for i in (0, 2, 4))
    draw.ellipse([4,  4,  size - 4,  size - 4],  fill=rgb)
    draw.ellipse([18, 18, size - 18, size - 18], fill=(0, 0, 0, 0))
    return img


def _tooltip_text() -> str:
    status, cache = read_local()
    if status:
        app    = status.get("app", "Unknown")
        locked = status.get("locked", False)
        active = status.get("active", False)
        state  = "Away" if locked else ("Active" if active else "Idle")
        score  = (cache or {}).get("summary", {}).get("productivity_score", 0)
        return f"TelemetryAgent — {state}\n{app}  |  Score: {score:.0f}%"
    return "TelemetryAgent — waiting for agent…"


def run_tray(root: tk.Tk):
    """Runs in a background daemon thread."""
    _popup_ref: list[PopupWindow | None] = [None]

    def show_popup():
        if _popup_ref[0] and _popup_ref[0]._win.winfo_exists():
            return
        root.after(0, lambda: _open_popup(root, _popup_ref))

    def open_dashboard():
        if _SERVER_BASE:
            import webbrowser
            webbrowser.open(_SERVER_BASE)

    def do_refresh():
        if _popup_ref[0] and _popup_ref[0]._win.winfo_exists():
            root.after(0, _popup_ref[0].refresh)

    def do_exit(icon, _item):
        icon.stop()
        root.after(0, root.quit)

    menu = pystray.Menu(
        pystray.MenuItem("Show Stats",       lambda *_: show_popup(),      default=True),
        pystray.MenuItem("Open Dashboard",   lambda *_: open_dashboard()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Refresh",          lambda *_: do_refresh()),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Exit Agent UI",    do_exit),
    )

    icon = pystray.Icon(
        "TelemetryAgent",
        _make_tray_icon(GREEN),
        "TelemetryAgent",
        menu,
    )

    # Update icon colour + tooltip every 10 s
    def _updater():
        while True:
            time.sleep(10)
            try:
                status, _ = read_local()
                if status:
                    locked = status.get("locked", False)
                    active = status.get("active", False)
                    col = RED if locked else (GREEN if active else YELLOW)
                    icon.icon = _make_tray_icon(col)
                icon.title = _tooltip_text()
            except Exception:
                pass

    threading.Thread(target=_updater, daemon=True).start()
    icon.run()


def _open_popup(root: tk.Tk, ref: list):
    popup = PopupWindow(root)
    ref[0] = popup


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _fmt_time(secs: int) -> str:
    if secs <= 0:
        return "0m"
    h, m = divmod(secs, 3600)
    m = m // 60
    return f"{h}h {m}m" if h else f"{m}m"


# ═══════════════════════════════════════════════════════════════════════════════
# Entry point
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    # Tkinter must live on the main thread
    root = tk.Tk()
    root.withdraw()            # hidden root — we only show Toplevel popups
    root.title("TelemetryAgent UI")

    # System tray runs in background daemon thread
    tray_thread = threading.Thread(target=run_tray, args=(root,), daemon=True)
    tray_thread.start()

    root.mainloop()


if __name__ == "__main__":
    main()
