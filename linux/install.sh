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
# AGENT_API_KEY may be pre-injected by the server into this script at serve
# time (see /install-script-linux route) so curl|bash installs work without
# needing --admin-key.  It can also be passed explicitly: --api-key VALUE.
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
          "$BIN_DIR/telemetry-ui" \
          "$BIN_DIR/user-prod"
    rm -rf "$DATA_DIR" "$XDG_CONFIG/telemetry-agent"
    success "Uninstall complete. Cloud data is not affected."
    info "Removed commands: telemetry-agent, telemetry-ui (alias), user-prod"
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
"$PYTHON" -c "import venv" 2>/dev/null || die "Python venv module missing.\n  Debian/Ubuntu: sudo apt-get install -y python3-venv\n  Fedora/RHEL  : sudo dnf install -y python3-virtualenv"

# ── Required system packages check ───────────────────────────────────────────
# No hard-required system packages remain after the tray UI was removed.
# python3-venv is checked directly above; all X11/AT-SPI tools are optional.
# These arrays are intentionally empty — kept as scaffolding in case a future
# dependency is added.  The guard below fires only if something appends to them.
DESKTOP="${XDG_CURRENT_DESKTOP:-}"
MISSING_REQUIRED=()
MISSING_APT=()
MISSING_DNF=()
MISSING_PACMAN=()


if [[ ${#MISSING_REQUIRED[@]} -gt 0 ]]; then
    echo ""
    echo -e "${RED}[error]${NC} ${BOLD}Required system packages are missing:${NC}"
    for pkg in "${MISSING_REQUIRED[@]}"; do
        echo -e "  ${RED}✗${NC}  $pkg"
    done
    echo ""
    echo -e "  Install them (one-time, requires sudo) and then re-run this installer:"
    echo ""
    echo -e "  ${BOLD}Debian / Ubuntu / MATE:${NC}"
    echo -e "    sudo apt-get install -y ${MISSING_APT[*]}"
    echo ""
    if [[ ${#MISSING_DNF[@]} -gt 0 ]]; then
        echo -e "  ${BOLD}Fedora / RHEL:${NC}"
        echo -e "    sudo dnf install -y ${MISSING_DNF[*]}"
        echo ""
    fi
    if [[ ${#MISSING_PACMAN[@]} -gt 0 ]]; then
        echo -e "  ${BOLD}Arch:${NC}"
        echo -e "    sudo pacman -S --needed ${MISSING_PACMAN[*]}"
        echo ""
    fi
    exit 1
fi

# ── Optional system tools (warn only — fallbacks exist) ───────────────────────
MISSING_OPT=()
command -v xdotool    &>/dev/null || MISSING_OPT+=("xdotool")
command -v xprintidle &>/dev/null || MISSING_OPT+=("xprintidle")
command -v xprop      &>/dev/null || MISSING_OPT+=("x11-utils")   # fallback window detector

# gir1.2-atspi-2.0 — AT-SPI2 accessibility bus bindings.
# Primary window-tracking method on GNOME Wayland (works without Shell.Eval).
# Without it, native Wayland apps (Terminal, Firefox, Files) will show as Unknown.
if ! "$PYTHON" -c "import gi; gi.require_version('Atspi','2.0'); from gi.repository import Atspi" \
        &>/dev/null 2>&1; then
    MISSING_OPT+=("gir1.2-atspi-2.0")
    warn ""
    warn "⚠  gir1.2-atspi-2.0 is NOT installed."
    warn "   On Wayland (Ubuntu 22.04+), app names will show as Unknown without it."
    warn "   Fix: sudo apt install -y gir1.2-atspi-2.0"
    warn ""
fi


if [[ ${#MISSING_OPT[@]} -gt 0 ]]; then
    warn "Optional packages not installed: ${MISSING_OPT[*]}"
    warn "The agent will run but window-tracking accuracy is reduced without them."
    warn "Install when convenient (no re-run needed):"
    warn "  Debian/Ubuntu: sudo apt-get install -y ${MISSING_OPT[*]}"
    warn "  Fedora/RHEL  : sudo dnf install -y xdotool xprintidle xorg-x11-utils"
    warn "  Arch         : sudo pacman -S --needed xdotool xorg-xprintidle xorg-xproputils python-gobject"
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

# Use --system-site-packages so the venv can access gi.repository (Wnck, Atspi)
# for Wayland window detection without requiring extra pip packages.
_needs_venv=false
if [[ ! -f "$VENV_DIR/bin/python" ]]; then
    _needs_venv=true
fi

if $_needs_venv; then
    "$PYTHON" -m venv --system-site-packages "$VENV_DIR"
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
"$VENV_PY" -m pip install --quiet requests
success "Virtual environment ready: $VENV_DIR"

# ── Step 2: Download agent + UI scripts ───────────────────────────────────────
info "[2/4] Downloading agent scripts..."

curl -fsSL "$SERVER_URL/download-linux-agent"     -o "$TMP_DIR/linux_telemetry_agent.py"
curl -fsSL "$SERVER_URL/download-linux-dashboard" -o "$TMP_DIR/linux_telemetry_dashboard.py"

# Basic sanity check — reject suspiciously small files
[[ $(wc -c < "$TMP_DIR/linux_telemetry_agent.py")     -gt 1024 ]] || die "Downloaded agent script is too small — server error?"
[[ $(wc -c < "$TMP_DIR/linux_telemetry_dashboard.py") -gt 1024 ]] || die "Downloaded dashboard script is too small — server error?"

mkdir -p "$BIN_DIR"
cp "$TMP_DIR/linux_telemetry_agent.py"     "$BIN_DIR/linux_telemetry_agent.py"
cp "$TMP_DIR/linux_telemetry_dashboard.py" "$BIN_DIR/linux_telemetry_dashboard.py"
success "Scripts saved to $BIN_DIR"

# ── Step 3: Create executable wrapper scripts in ~/.local/bin ─────────────────
info "[3/4] Installing launcher wrappers..."
AGENT_WRAPPER="$BIN_DIR/telemetry-agent"
UI_WRAPPER="$BIN_DIR/telemetry-ui"   # backward compat alias — points to dashboard
USERPROD_WRAPPER="$BIN_DIR/user-prod"

# Write wrapper — use printf to avoid any eval/exec confusion for EDR scanners
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/linux_telemetry_agent.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$AGENT_WRAPPER"
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/linux_telemetry_dashboard.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$UI_WRAPPER"
printf '#!/usr/bin/env bash\nexec "%s/bin/python" "%s/linux_telemetry_dashboard.py" "$@"\n' \
    "$VENV_DIR" "$BIN_DIR" > "$USERPROD_WRAPPER"

chmod 755 "$AGENT_WRAPPER" "$UI_WRAPPER" "$USERPROD_WRAPPER"
success "Launchers: $AGENT_WRAPPER  $USERPROD_WRAPPER"

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
[[ -n "$ADMIN_KEY"      ]] && INSTALL_ARGS+=("--admin-key" "$ADMIN_KEY")
[[ -n "$AGENT_API_KEY"  ]] && INSTALL_ARGS+=("--api-key"   "$AGENT_API_KEY")

"$VENV_PY" "$BIN_DIR/linux_telemetry_agent.py" "${INSTALL_ARGS[@]}"

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

# ── Enable GNOME Shell window tracking on Wayland ─────────────────────────────
# On GNOME Wayland, native apps (Terminal, Firefox, Files, etc.) are invisible
# to xdotool and xprop because they run in Wayland mode, not XWayland.
# org.gnome.Shell.Eval is the only way to get the focused window on Wayland —
# but it requires development-tools to be enabled.  This is safe: the agent
# runs as the same user and already has full access to the session.
if command -v gsettings &>/dev/null; then
    _SESSION="${XDG_SESSION_TYPE:-}"
    _DESKTOP="${XDG_CURRENT_DESKTOP:-}"
    if [[ "$_SESSION" == "wayland" ]] || [[ "${_DESKTOP^^}" == *"GNOME"* ]] || [[ "${_DESKTOP^^}" == *"UBUNTU"* ]]; then
        # development-tools enables Shell.Eval (fallback for older GNOME)
        gsettings set org.gnome.shell development-tools true 2>/dev/null || true

        # toolkit-accessibility=true makes GTK apps (Firefox, Chrome, Files, etc.)
        # register with AT-SPI2 so the agent can detect them on Wayland.
        # Without this, snap-packaged apps are invisible to window tracking.
        if gsettings set org.gnome.desktop.interface toolkit-accessibility true 2>/dev/null; then
            success "AT-SPI accessibility enabled — all apps including Firefox will be tracked"
        else
            warn "Could not enable toolkit-accessibility."
            warn "Firefox and other GTK apps may show as Unknown. Fix manually:"
            warn "  gsettings set org.gnome.desktop.interface toolkit-accessibility true"
            warn "  Then restart any open apps (Firefox, etc.)"
        fi
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
echo -e "  ${BOLD}Dashboard${NC}    : user-prod"
echo -e "  ${BOLD}Quick view${NC}   : user-prod --once"
echo ""
echo -e "  ${BOLD}Manage the agent:${NC}"
echo -e "    systemctl --user status  telemetry-agent"
echo -e "    systemctl --user restart telemetry-agent"
echo -e "    kill \$(cat $DATA_DIR/agent.pid)   # if started via fallback"
echo ""
echo -e "  ${BOLD}Uninstall:${NC}"
echo -e "    curl -fsSL '$SERVER_URL/install-script-linux' | bash -s -- --uninstall"
echo ""
