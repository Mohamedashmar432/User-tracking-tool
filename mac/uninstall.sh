#!/usr/bin/env bash
# mac/uninstall.sh — macOS Telemetry Agent uninstaller (user-level, no sudo)

set -euo pipefail

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'

DATA_DIR="$HOME/Library/Application Support/TelemetryAgent"
BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENT_PLIST="$HOME/Library/LaunchAgents/com.telemetry.agent.plist"

echo -e "${BOLD}Uninstalling Telemetry Agent (macOS)...${NC}"

launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null && \
    echo -e "${GREEN}[ok]${NC}    launchd agent stopped" || \
    echo -e "${YELLOW}[skip]${NC} launchd agent was not loaded"

rm -f "$LAUNCH_AGENT_PLIST" && \
    echo -e "${GREEN}[ok]${NC}    Removed plist: $LAUNCH_AGENT_PLIST" || true

for f in telemetry-agent telemetry-ui user-prod mac_telemetry_agent.py mac_telemetry_dashboard.py; do
    rm -f "$BIN_DIR/$f" 2>/dev/null && \
        echo -e "${GREEN}[ok]${NC}    Removed: $BIN_DIR/$f" || true
done

rm -rf "$DATA_DIR" && \
    echo -e "${GREEN}[ok]${NC}    Removed: $DATA_DIR" || true

echo ""
echo -e "${GREEN}Uninstall complete.${NC} Cloud data is not affected."
