import os
import sys
import time
import json
import platform
import getpass
import re
from datetime import datetime

try:
    import win32api
    import win32gui
    import win32process
    import psutil
except ImportError:
    print("Error: Required libraries not found. Please install 'pywin32' and 'psutil'.")
    sys.exit(1)

# Configuration
LOG_DIR = "."
LOG_FILE = "logs.txt"
IDLE_THRESHOLD = 300  # 5 minutes
TICK_INTERVAL = 5     # Internal tracking interval in seconds
LOG_INTERVAL = 30     # Logging interval in seconds
MAX_LOG_SIZE = 10 * 1024 * 1024  # 10 MB

BROWSER_PROCESSES = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe"}

class TelemetryState:
    def __init__(self):
        self.current_app = "Unknown"
        self.app_usage_total = {}
        self.last_tick_timestamp = time.time()
        self.last_switch_timestamp = time.time()

    def update(self, app_name, is_active):
        now = time.time()
        delta = now - self.last_tick_timestamp

        # 1. Track accumulation if user is active
        if is_active and self.current_app != "Unknown":
            self.app_usage_total[self.current_app] = self.app_usage_total.get(self.current_app, 0) + delta

        # 2. Handle app switch
        if app_name != self.current_app:
            self.current_app = app_name
            self.last_switch_timestamp = now

        self.last_tick_timestamp = now

    def get_session_duration(self):
        return int(time.time() - self.last_switch_timestamp)

def get_user_info():
    try:
        return {"hostname": platform.node(), "username": getpass.getuser()}
    except Exception:
        return {"hostname": "Unknown", "username": "Unknown"}

def get_idle_time():
    try:
        last_input_info = win32api.GetLastInputInfo()
        current_ticks = win32api.GetTickCount()
        return (current_ticks - last_input_info) // 1000
    except Exception:
        return 0

def extract_domain_from_title(hwnd, process_name):
    """Approximate domain extraction from window title for browsers."""
    if process_name.lower() not in BROWSER_PROCESSES:
        return "N/A"

    try:
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return "Unknown"

        # Remove common browser suffixes
        # e.g., "YouTube - Google Chrome" -> "YouTube"
        clean_title = re.sub(r'\s- (Google Chrome|Microsoft Edge|Firefox|Brave).*$', '', title, flags=re.IGNORECASE).strip()

        # Simple heuristic: look for something that looks like a domain or a known site name
        # If there's a '.' in the first part of the title, it might be a domain.
        # Otherwise, we return the cleaned page title.
        return clean_title
    except Exception:
        return "Unknown"

def get_foreground_app():
    """Returns (process_name, window_title, hwnd)"""
    try:
        hwnd = win32gui.GetForegroundWindow()
        if not hwnd:
            return "Unknown", "Unknown", None

        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        process = psutil.Process(pid)
        name = process.name()
        title = win32gui.GetWindowText(hwnd)
        return name, title, hwnd
    except Exception:
        return "Unknown", "Unknown", None

def rotate_logs():
    try:
        if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_SIZE:
            old_log = LOG_FILE + ".old"
            if os.path.exists(old_log):
                os.remove(old_log)
            os.rename(LOG_FILE, old_log)
    except Exception as e:
        print(f"Log rotation failed: {e}")

def main():
    user_info = get_user_info()
    state = TelemetryState()

    print(f"Agent started for {user_info['username']} on {user_info['hostname']}")
    print(f"Stateful tracking active. Logging every {LOG_INTERVAL}s...")

    elapsed_since_log = 0

    try:
        while True:
            # 1. Data Collection
            idle_seconds = get_idle_time()
            is_active = idle_seconds < IDLE_THRESHOLD
            app_name, window_title, hwnd = get_foreground_app()

            # Domain extraction logic
            domain = "N/A"
            if hwnd:
                domain = extract_domain_from_title(hwnd, app_name)

            # 2. Update Internal State
            state.update(app_name, is_active)

            # 3. Handle Logging
            elapsed_since_log += TICK_INTERVAL
            if elapsed_since_log >= LOG_INTERVAL:
                rotate_logs()

                telemetry = {
                    "timestamp": datetime.now().isoformat(),
                    "hostname": user_info['hostname'],
                    "username": user_info['username'],
                    "current_app": state.current_app,
                    "domain": domain,
                    "idle_seconds": idle_seconds,
                    "active": is_active,
                    "session_duration": state.get_session_duration(),
                    "app_usage_total": state.app_usage_total
                }

                log_entry = json.dumps(telemetry)
                print(log_entry)

                try:
                    with open(LOG_FILE, "a", encoding="utf-8") as f:
                        f.write(log_entry + "\n")
                except Exception as e:
                    print(f"Error writing log: {e}")

                elapsed_since_log = 0

            time.sleep(TICK_INTERVAL)

    except KeyboardInterrupt:
        print("\nAgent stopped by user.")
    except Exception as e:
        print(f"Unexpected agent crash: {e}")
    finally:
        print("Exiting.")

if __name__ == "__main__":
    main()
