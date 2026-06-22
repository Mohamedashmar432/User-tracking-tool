#!/usr/bin/env bash
# mac/install.sh — macOS Telemetry Agent + UI installer
#
# RUNS ENTIRELY AS A REGULAR USER — NO SUDO REQUIRED.
# All paths are under ~/Library/ (macOS user dirs).
#
# Usage:
#   # One-liner from dashboard:
#   curl -fsSL https://<server>/install-script-mac | bash
#
#   # Or download and run manually:
#   curl -fsSL https://<server>/install-script-mac -o install-telemetry-mac.sh
#   bash install-telemetry-mac.sh --server-url https://<server>
#
#   # Non-interactive:
#   bash install.sh --server-url https://your-server --admin-key YOUR_KEY
#
#   # Uninstall:
#   bash install.sh --uninstall

set -euo pipefail

# SERVER_URL is injected by the server when served via curl | bash.
SERVER_URL="${SERVER_URL:-}"

# ── Colours ───────────────────────────────────────────────────────────────────
BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; RED='\033[0;31m'; NC='\033[0m'

info()    { echo -e "${BLUE}[info]${NC}  $*"; }
success() { echo -e "${GREEN}[ok]${NC}    $*"; }
warn()    { echo -e "${YELLOW}[warn]${NC}  $*"; }
die()     { echo -e "${RED}[error]${NC} $*" >&2; exit 1; }

# ── Argument parsing ──────────────────────────────────────────────────────────
ADMIN_KEY=""
UNINSTALL=false
AGENT_API_KEY="${AGENT_API_KEY:-}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server-url)  SERVER_URL="$2";      shift 2 ;;
        --admin-key)   ADMIN_KEY="$2";       shift 2 ;;
        --api-key)     AGENT_API_KEY="$2";   shift 2 ;;
        --uninstall)   UNINSTALL=true;       shift ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# ── macOS paths ───────────────────────────────────────────────────────────────
DATA_DIR="$HOME/Library/Application Support/TelemetryAgent"
VENV_DIR="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
LAUNCH_AGENT_LABEL="com.telemetry.agent"
LAUNCH_AGENT_PLIST="$LAUNCH_AGENTS_DIR/$LAUNCH_AGENT_LABEL.plist"

# ── Uninstall path ────────────────────────────────────────────────────────────
if $UNINSTALL; then
    info "Uninstalling Telemetry Agent..."
    launchctl unload "$LAUNCH_AGENT_PLIST" 2>/dev/null || true
    rm -f "$LAUNCH_AGENT_PLIST"
    rm -f "$BIN_DIR/telemetry-agent" "$BIN_DIR/telemetry-ui" "$BIN_DIR/user-prod"
    rm -rf "$DATA_DIR"
    success "Uninstall complete. Cloud data is not affected."
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Telemetry Agent — macOS Installer (user-level)    ║${NC}"
echo -e "${BOLD}║   No sudo · No root · Runs as you                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Check macOS version (10.15+ Catalina required for TCC/osascript) ─────────
SW_VERS=$(sw_vers -productVersion 2>/dev/null || echo "0")
info "macOS version: $SW_VERS"

# ── Detect Python 3.9+ ───────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(sys.version_info >= (3,9))" 2>/dev/null || echo "False")
        if [[ "$ver" == "True" ]]; then PYTHON="$cmd"; break; fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo ""
    die "Python 3.9+ is required but not found.\n\n  Install options:\n  1. Homebrew:  brew install python@3.11\n  2. python.org: https://www.python.org/downloads/\n  3. pyenv:      pyenv install 3.11.8 && pyenv global 3.11.8"
fi
info "Python: $PYTHON ($($PYTHON --version))"

# ── Ensure venv module ────────────────────────────────────────────────────────
"$PYTHON" -c "import venv" 2>/dev/null || \
    die "Python venv module missing. Reinstall Python from python.org or via Homebrew."

# ── Prompt for server URL if not provided ─────────────────────────────────────
if [[ -z "$SERVER_URL" ]]; then
    echo -ne "${BOLD}Server URL${NC} (e.g. https://telemetry.company.com): "
    read -r SERVER_URL
fi
[[ -n "$SERVER_URL" ]] || die "Server URL is required."
SERVER_URL="${SERVER_URL%/}"

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Step 1: Create Python venv ────────────────────────────────────────────────
info "[1/4] Creating Python virtual environment..."
mkdir -p "$DATA_DIR"

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"

# Bootstrap pip if missing
if ! "$VENV_PY" -m pip --version &>/dev/null 2>&1; then
    info "Bootstrapping pip..."
    if "$VENV_PY" -m ensurepip --upgrade &>/dev/null 2>&1; then
        success "pip bootstrapped via ensurepip"
    else
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$TMP_DIR/get-pip.py"
        "$VENV_PY" "$TMP_DIR/get-pip.py" --quiet
        success "pip bootstrapped via get-pip.py"
    fi
fi

"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet requests rich
success "Virtual environment ready: $VENV_DIR"

# ── Step 2: Download agent + UI scripts ───────────────────────────────────────
info "[2/4] Downloading agent scripts..."

curl -fsSL "$SERVER_URL/download-mac-agent"     -o "$TMP_DIR/mac_telemetry_agent.py"
curl -fsSL "$SERVER_URL/download-mac-dashboard" -o "$TMP_DIR/mac_telemetry_ui.py"

[[ $(wc -c < "$TMP_DIR/mac_telemetry_agent.py") -gt 1024 ]] || die "Downloaded agent script is too small — server error?"
[[ $(wc -c < "$TMP_DIR/mac_telemetry_ui.py")    -gt 1024 ]] || die "Downloaded dashboard script is too small — server error?"

mkdir -p "$BIN_DIR"
cp "$TMP_DIR/mac_telemetry_agent.py" "$BIN_DIR/mac_telemetry_agent.py"
cp "$TMP_DIR/mac_telemetry_ui.py"    "$BIN_DIR/mac_telemetry_ui.py"
success "Scripts saved to $BIN_DIR"

# ── Step 3: Create executable wrapper scripts ─────────────────────────────────
info "[3/4] Installing launcher wrappers..."
AGENT_WRAPPER="$BIN_DIR/telemetry-agent"
UI_WRAPPER="$BIN_DIR/telemetry-ui"
USERPROD_WRAPPER="$BIN_DIR/user-prod"

printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/mac_telemetry_agent.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$AGENT_WRAPPER"
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/mac_telemetry_ui.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$UI_WRAPPER"
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/mac_telemetry_ui.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$USERPROD_WRAPPER"

chmod 755 "$AGENT_WRAPPER" "$UI_WRAPPER" "$USERPROD_WRAPPER"
success "Launchers: $AGENT_WRAPPER  $USERPROD_WRAPPER"

export PATH="$BIN_DIR:$PATH"

# ── Step 4: Register agent (launchd user agent) ───────────────────────────────
info "[4/4] Registering agent (launchd user agent with KeepAlive)..."

INSTALL_ARGS=("--install" "--server-url" "$SERVER_URL")
[[ -n "$ADMIN_KEY"     ]] && INSTALL_ARGS+=("--admin-key" "$ADMIN_KEY")
[[ -n "$AGENT_API_KEY" ]] && INSTALL_ARGS+=("--api-key"   "$AGENT_API_KEY")

# agent --install writes + loads the launchd plist itself (unload+load inside)
"$VENV_PY" "$BIN_DIR/mac_telemetry_agent.py" "${INSTALL_ARGS[@]}"

# Verify agent started (launchd KeepAlive will keep it running)
sleep 2
if launchctl list "$LAUNCH_AGENT_LABEL" &>/dev/null; then
    success "Agent running via launchd ($LAUNCH_AGENT_LABEL)"
else
    warn "launchd entry not found — starting agent as background process..."
    nohup "$VENV_PY" "$BIN_DIR/mac_telemetry_agent.py" >> "$DATA_DIR/agent.log" 2>&1 &
    AGENT_PID=$!
    disown "$AGENT_PID" 2>/dev/null || true
    sleep 2
    if kill -0 "$AGENT_PID" 2>/dev/null; then
        success "Agent running in background (PID $AGENT_PID)"
    else
        warn "Agent could not start — check log: $DATA_DIR/agent.log"
    fi
fi

# ── Accessibility permission hint ─────────────────────────────────────────────
echo ""
echo -e "${YELLOW}[hint]${NC} To enable window title tracking on macOS 10.15+:"
echo -e "       Open ${BOLD}System Settings → Privacy & Security → Accessibility${NC}"
echo -e "       and grant access to your Terminal (or the Python launcher)."
echo -e "       App names will still be tracked without this — only titles require it."
echo ""

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}${GREEN}║   Installation complete!                             ║${NC}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════╝${NC}"
echo ""
echo -e "  ${BOLD}Agent log${NC}    : $DATA_DIR/agent.log"
echo -e "  ${BOLD}Config${NC}       : $DATA_DIR/config.json"
echo -e "  ${BOLD}Data dir${NC}     : $DATA_DIR"
echo ""
echo -e "  ${BOLD}Dashboard${NC}    : user-prod"
echo ""
echo -e "  ${BOLD}Manage the agent:${NC}"
echo -e "    launchctl list $LAUNCH_AGENT_LABEL"
echo -e "    launchctl unload  $LAUNCH_AGENT_PLIST"
echo -e "    launchctl load    $LAUNCH_AGENT_PLIST"
echo ""
echo -e "  ${BOLD}Uninstall:${NC}"
echo -e "    curl -fsSL '$SERVER_URL/install-script-mac' | bash -s -- --uninstall"
echo ""
