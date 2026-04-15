# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview
A lightweight Windows background agent that collects user activity telemetry (machine name, logged-in user, idle time, and active process) and visualizes it via a static dashboard.

## Architecture
- **Telemetry Agent (`telemetry_agent.py`)**: A polling-based service that uses `pywin32` and `psutil` to query Windows API for user interaction and active window state. It outputs structured JSONL logs.
- **Visualization Dashboard (`dashboard.html`)**: A standalone HTML/JS application that parses the JSONL logs and renders them using Chart.js and Tailwind CSS.
- **Data Flow**: `Windows API` $\rightarrow$ `telemetry_agent.py` $\rightarrow$ `logs.txt` $\rightarrow$ `dashboard.html`.

## Common Commands

### Running the Agent
The agent requires a virtual environment and specific Windows libraries.
```bash
# Activate venv
.\user-track\Scripts\activate

# Install dependencies
pip install pywin32 psutil

# Run the agent
python telemetry_agent.py
```

### Viewing Telemetry
1. Run the agent to generate `logs.txt`.
2. Open `dashboard.html` in a web browser.
3. Upload `logs.txt` using the file picker.

## Technical Constraints
- **Platform**: Windows 10/11 only.
- **Log Format**: JSON Lines (JSONL) for append-efficient logging.
- **Idle Detection**: Uses `GetLastInputInfo` to calculate time since last input.
- **Process Tracking**: Resolves `GetForegroundWindow` $\rightarrow$ `PID` $\rightarrow$ `Process Name`.
