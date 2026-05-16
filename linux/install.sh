#!/usr/bin/env bash
# install.sh — Linux Telemetry Agent + UI installer
#
# RUNS ENTIRELY AS A REGULAR USER — NO SUDO REQUIRED.
# All paths are under ~/.local/ and ~/.config/ (XDG user dirs).
# System dependencies (xdotool, xprintidle) are optional; the agent
# falls back to D-Bus idle detection if they are not present.
#
# Usage:
#   # One-liner from dashboard:
#   curl -fsSL https://<server>/install-script-linux | bash
#
#   # Or download and run manually (so you can inspect first):
#   curl -fsSL https://<server>/install-script-linux -o install-telemetry.sh
#   bash install-telemetry.sh --server-url https://<server>
#
#   # Non-interactive:
#   bash install.sh --server-url https://your-server --admin-key YOUR_KEY
#
#   # Uninstall:
#   bash install.sh --uninstall

set -euo pipefail

# SERVER_URL is injected by the server when served via curl | bash.
# The --server-url flag or interactive prompt is the fallback.
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

while [[ $# -gt 0 ]]; do
    case "$1" in
        --server-url)  SERVER_URL="$2"; shift 2 ;;
        --admin-key)   ADMIN_KEY="$2";  shift 2 ;;
        --uninstall)   UNINSTALL=true;  shift ;;
        *) die "Unknown argument: $1" ;;
    esac
done

# ── XDG paths (all user-level, no root needed) ────────────────────────────────
XDG_DATA="${XDG_DATA_HOME:-$HOME/.local/share}"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
DATA_DIR="$XDG_DATA/telemetry-agent"
VENV_DIR="$DATA_DIR/venv"
BIN_DIR="$HOME/.local/bin"
AUTOSTART_DIR="$XDG_CONFIG/autostart"
SYSTEMD_USER_DIR="$XDG_CONFIG/systemd/user"

# ── Uninstall path ────────────────────────────────────────────────────────────
if $UNINSTALL; then
    info "Uninstalling Telemetry Agent..."
    systemctl --user disable --now telemetry-agent.service 2>/dev/null || true
    systemctl --user disable --now telemetry-agent-watchdog.timer 2>/dev/null || true
    systemctl --user daemon-reload 2>/dev/null || true
    rm -f "$AUTOSTART_DIR/telemetry-agent.desktop" \
          "$AUTOSTART_DIR/telemetry-ui.desktop" \
          "$SYSTEMD_USER_DIR/telemetry-agent.service" \
          "$SYSTEMD_USER_DIR/telemetry-agent-watchdog.service" \
          "$SYSTEMD_USER_DIR/telemetry-agent-watchdog.timer" \
          "$BIN_DIR/telemetry-agent" \
          "$BIN_DIR/telemetry-ui"
    rm -rf "$DATA_DIR" "$XDG_CONFIG/telemetry-agent"
    success "Uninstall complete. Cloud data is not affected."
    exit 0
fi

# ── Banner ────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}╔══════════════════════════════════════════════════════╗${NC}"
echo -e "${BOLD}║   Telemetry Agent — Linux Installer (user-level)    ║${NC}"
echo -e "${BOLD}║   No sudo · No root · Runs as you                   ║${NC}"
echo -e "${BOLD}╚══════════════════════════════════════════════════════╝${NC}"
echo ""

# ── Detect Python 3.9+ ───────────────────────────────────────────────────────
PYTHON=""
for cmd in python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c "import sys; print(sys.version_info >= (3,9))" 2>/dev/null || echo "False")
        if [[ "$ver" == "True" ]]; then PYTHON="$cmd"; break; fi
    fi
done
[[ -n "$PYTHON" ]] || die "Python 3.9+ is required but not found. Install it with your package manager."
info "Python: $PYTHON ($($PYTHON --version))"

# Ensure venv module is available
"$PYTHON" -c "import venv" 2>/dev/null || die "Python venv module missing. Install python3-venv (no sudo needed if Python is in your user environment)."

# ── Optional system tool check (NO sudo — just inform) ───────────────────────
# xdotool and xprintidle improve tracking accuracy on X11 but are not required.
# The agent falls back to D-Bus idle detection if they are absent.
MISSING_TOOLS=()
command -v xdotool    &>/dev/null || MISSING_TOOLS+=("xdotool")
command -v xprintidle &>/dev/null || MISSING_TOOLS+=("xprintidle")

if [[ ${#MISSING_TOOLS[@]} -gt 0 ]]; then
    warn "Optional tools not installed: ${MISSING_TOOLS[*]}"
    warn "The agent will use D-Bus fallback for idle/window detection — still fully functional."
    warn "For best accuracy your sysadmin can install them once (no ongoing access needed):"
    warn "  Debian/Ubuntu : sudo apt-get install -y ${MISSING_TOOLS[*]}"
    warn "  Fedora/RHEL   : sudo dnf  install -y ${MISSING_TOOLS[*]}"
    warn "  Arch          : sudo pacman -S --needed ${MISSING_TOOLS[*]//xprintidle/xorg-xprintidle}"
    echo ""
fi

# ── Prompt for server URL if not provided ─────────────────────────────────────
if [[ -z "$SERVER_URL" ]]; then
    echo -ne "${BOLD}Server URL${NC} (e.g. https://telemetry.company.com): "
    read -r SERVER_URL
fi
[[ -n "$SERVER_URL" ]] || die "Server URL is required."
SERVER_URL="${SERVER_URL%/}"   # strip trailing slash

# Temp dir used by both steps — created early so bootstrap can use it
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT

# ── Step 1: Create Python venv + bootstrap pip ────────────────────────────────
info "[1/4] Creating Python virtual environment..."
mkdir -p "$DATA_DIR"

if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    "$PYTHON" -m venv "$VENV_DIR"
fi

VENV_PY="$VENV_DIR/bin/python"

# Debian/Ubuntu ship python3 without ensurepip, so `python3 -m venv` creates
# the venv directory structure but omits pip entirely.  Detect and fix that.
if ! "$VENV_PY" -m pip --version &>/dev/null 2>&1; then
    info "pip absent in venv (Debian/Ubuntu pattern) — bootstrapping..."
    # Try ensurepip first (works on Fedora, RHEL, Arch, non-stripped Debian builds)
    if "$VENV_PY" -m ensurepip --upgrade &>/dev/null 2>&1; then
        success "pip bootstrapped via ensurepip"
    else
        # Distro has disabled ensurepip — fall back to the official get-pip.py
        info "Fetching get-pip.py from pypa.io..."
        curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$TMP_DIR/get-pip.py"
        "$VENV_PY" "$TMP_DIR/get-pip.py" --quiet
        success "pip bootstrapped via get-pip.py"
    fi
fi

# Use 'python -m pip' rather than the pip binary — works even when the
# pip symlink is absent (another Debian quirk).
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet requests pystray Pillow
success "Virtual environment ready: $VENV_DIR"

# ── Step 2: Download agent + UI scripts ───────────────────────────────────────
info "[2/4] Downloading agent scripts..."

curl -fsSL "$SERVER_URL/download-linux-agent" -o "$TMP_DIR/linux_telemetry_agent.py"
curl -fsSL "$SERVER_URL/download-linux-ui"    -o "$TMP_DIR/linux_telemetry_ui.py"

# Basic sanity check — reject suspiciously small files
[[ $(wc -c < "$TMP_DIR/linux_telemetry_agent.py") -gt 1024 ]] || die "Downloaded agent script is too small — server error?"
[[ $(wc -c < "$TMP_DIR/linux_telemetry_ui.py")    -gt 1024 ]] || die "Downloaded UI script is too small — server error?"

mkdir -p "$BIN_DIR"
cp "$TMP_DIR/linux_telemetry_agent.py" "$BIN_DIR/linux_telemetry_agent.py"
cp "$TMP_DIR/linux_telemetry_ui.py"    "$BIN_DIR/linux_telemetry_ui.py"
success "Scripts saved to $BIN_DIR"

# ── Step 3: Create executable wrapper scripts in ~/.local/bin ─────────────────
info "[3/4] Installing launcher wrappers..."
AGENT_WRAPPER="$BIN_DIR/telemetry-agent"
UI_WRAPPER="$BIN_DIR/telemetry-ui"

# Write wrapper — use printf to avoid any eval/exec confusion for EDR scanners
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/linux_telemetry_agent.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$AGENT_WRAPPER"
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/linux_telemetry_ui.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$UI_WRAPPER"

chmod 755 "$AGENT_WRAPPER" "$UI_WRAPPER"
success "Launchers: $AGENT_WRAPPER  $UI_WRAPPER"

# Ensure ~/.local/bin is on PATH this session
export PATH="$BIN_DIR:$PATH"

# ── Step 4: Register agent (systemd user service + XDG autostart) ─────────────
info "[4/4] Registering agent (user-level systemd + XDG autostart)..."

# ── Fix ~/.config permissions if needed (Ubuntu Desktop: snap/flatpak can corrupt them) ──
_fix_dir() {
    local d="$1"
    [[ -e "$d" ]] || { mkdir -p "$d" 2>/dev/null; return; }
    [[ -w "$d" ]] && return   # already writable, nothing to do
    # Dir exists but not writable — try to fix if we own it
    if [[ "$(stat -c '%U' "$d" 2>/dev/null)" == "$(whoami)" ]]; then
        chmod u+rwx "$d" 2>/dev/null && info "Fixed permissions on $d" && return
    fi
    # We don't own it (root owns it) — can't fix without sudo
    warn "Cannot write to $d (owned by root)."
    warn "Run once to fix:  sudo chown -R $(whoami):$(whoami) $HOME/.config"
    warn "Then re-run this installer to enable autostart. The agent will still start below."
}

_fix_dir "$XDG_CONFIG"
_fix_dir "$SYSTEMD_USER_DIR"
_fix_dir "$AUTOSTART_DIR"

INSTALL_ARGS=("--install" "--server-url" "$SERVER_URL")
[[ -n "$ADMIN_KEY" ]] && INSTALL_ARGS+=("--admin-key" "$ADMIN_KEY")

"$VENV_PY" "$BIN_DIR/linux_telemetry_agent.py" "${INSTALL_ARGS[@]}"

# XDG autostart for the UI companion
mkdir -p "$AUTOSTART_DIR"
cat > "$AUTOSTART_DIR/telemetry-ui.desktop" << 'DESKTOP_EOF'
[Desktop Entry]
Type=Application
Name=TelemetryAgent UI
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
Comment=Telemetry tray companion (user activity dashboard)
DESKTOP_EOF
# Write Exec line separately to avoid heredoc variable expansion issues
echo "Exec=$UI_WRAPPER" >> "$AUTOSTART_DIR/telemetry-ui.desktop"

# ── Start agent now — try systemd first, fall back to direct background launch ──
# The agent MUST be running for the user to appear in the dashboard.
AGENT_STARTED=false

if systemctl --user start telemetry-agent.service 2>/dev/null; then
    success "Agent started via systemd"
    AGENT_STARTED=true
fi

if ! $AGENT_STARTED; then
    info "systemd unavailable — starting agent as background process..."
    # Kill any stale instance first
    if [[ -f "$DATA_DIR/agent.pid" ]]; then
        OLD_PID=$(cat "$DATA_DIR/agent.pid" 2>/dev/null)
        kill "$OLD_PID" 2>/dev/null || true
    fi
    nohup "$VENV_PY" "$BIN_DIR/linux_telemetry_agent.py" >> "$DATA_DIR/agent.log" 2>&1 &
    AGENT_PID=$!
    disown "$AGENT_PID" 2>/dev/null || true
    echo "$AGENT_PID" > "$DATA_DIR/agent.pid"
    sleep 2
    if kill -0 "$AGENT_PID" 2>/dev/null; then
        success "Agent running in background (PID $AGENT_PID) — data will appear in dashboard shortly"
    else
        warn "Agent could not start — check log: $DATA_DIR/agent.log"
        warn "Start manually: $VENV_PY $BIN_DIR/linux_telemetry_agent.py"
    fi
fi

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
echo -e "  ${BOLD}Manage the agent:${NC}"
echo -e "    systemctl --user status  telemetry-agent"
echo -e "    systemctl --user restart telemetry-agent"
echo -e "    kill \$(cat $DATA_DIR/agent.pid)   # if started via fallback"
echo ""
echo -e "  ${BOLD}Uninstall:${NC}"
echo -e "    curl -fsSL '$SERVER_URL/install-script-linux' | bash -s -- --uninstall"
echo ""
