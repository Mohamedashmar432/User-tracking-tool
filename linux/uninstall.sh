#!/usr/bin/env bash
# uninstall.sh — fully remove the Linux telemetry agent + UI from this user.
#
# This is the mirror of install.sh.  It removes every layer install.sh created:
#   1. systemd user service (telemetry-agent.service)
#   2. systemd watchdog timer (telemetry-agent-watchdog.timer + .service)
#   3. XDG autostart .desktop entries (agent + UI)
#   4. Wrapper scripts in ~/.local/bin
#   5. The .py executables those wrappers point to
#   6. All data dirs (~/.local/share/telemetry-agent, ~/.config/telemetry-agent)
#   7. Locked SQLite backup.db (offline event buffer)
#
# What it does NOT touch:
#   - System packages (gir1.2-wnck-*, xdotool, xprintidle, etc.) — those stay
#     installed in case other apps on the box need them.  Use your distro's
#     package manager to remove them if you really want to.
#   - Your /etc/telemetry-agent/config.json (system-wide config, if any).
#   - Anything outside ~/.local and ~/.config for the current user.
#
# Usage:
#   bash uninstall.sh                    # interactive (asks before deleting)
#   bash uninstall.sh --yes              # non-interactive, no prompt
#   bash uninstall.sh --keep-data        # uninstall code, keep events/log
#   bash uninstall.sh --purge            # also remove packages (apt/dnf/pacman)
#
# Served from the dashboard via:
#   curl -fsSL <server>/uninstall-script-linux | bash
#   curl -fsSL <server>/uninstall-script-linux | bash -s -- --yes
#   curl -fsSL <server>/uninstall-script-linux | YES=1 bash
#
#   YES=1   equivalent to --yes / -y (non-interactive)
#   Note: when piped via `curl | bash` on a TTY-less context (e.g. SSH from
#   a non-interactive shell), the script will still prompt unless you pass
#   --yes or set YES=1.
#
# Exit code 0 = clean uninstall, 1 = user aborted or pre-flight failed.

set -u  # NOTE: -e disabled — we want to keep going through individual rm errors
set -o pipefail

# ── Colour / output helpers ──────────────────────────────────────────────────
BOLD="\033[1m"
RED="\033[31m"
GRN="\033[32m"
YLW="\033[33m"
DIM="\033[2m"
NC="\033[0m"

info() { echo -e "  ${GRN}✓${NC}  $*"; }
warn() { echo -e "  ${YLW}!${NC}  $*"; }
err()  { echo -e "  ${RED}✗${NC}  $*"; }
hdr()  { echo -e "\n${BOLD}$*${NC}"; }
dim()  { echo -e "  ${DIM}$*${NC}"; }

# ── Args ────────────────────────────────────────────────────────────────────
ASSUME_YES=0
KEEP_DATA=0
PURGE_PKGS=0
# YES=1 env var is the convenience flag for `curl ... | YES=1 bash`
if [ "${YES:-0}" = "1" ]; then
    ASSUME_YES=1
fi
for arg in "$@"; do
    case "$arg" in
        --yes|-y)    ASSUME_YES=1 ;;
        --keep-data) KEEP_DATA=1  ;;
        --purge)     PURGE_PKGS=1 ;;
        -h|--help)
            sed -n '2,32p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) err "Unknown argument: $arg"; exit 1 ;;
    esac
done

# ── Resolve paths (mirror install.sh) ───────────────────────────────────────
HOME_DIR="${HOME:-$(eval echo "~$(id -un)")}"
XDG_DATA="${XDG_DATA_HOME:-$HOME_DIR/.local/share}"
XDG_CONFIG="${XDG_CONFIG_HOME:-$HOME_DIR/.config}"
BIN_DIR="$HOME_DIR/.local/bin"
DATA_DIR="$XDG_DATA/telemetry-agent"
CONFIG_DIR_LEGACY="$XDG_CONFIG/telemetry-agent"  # older install.sh versions
AUTOSTART_DIR="$XDG_CONFIG/autostart"
SYSTEMD_USER_DIR="$XDG_CONFIG/systemd/user"

# ── What we are about to remove ─────────────────────────────────────────────
hdr "Telemetry Agent — Uninstall"
echo "  This will remove every layer of the Linux telemetry agent + UI."
echo ""
dim "Layers to remove:"
[ -d "$SYSTEMD_USER_DIR" ] && dim "  systemd units  : $SYSTEMD_USER_DIR/{telemetry-agent.service,telemetry-agent-watchdog.service,telemetry-agent-watchdog.timer}"
[ -d "$AUTOSTART_DIR" ]    && dim "  XDG autostart  : $AUTOSTART_DIR/{telemetry-agent.desktop,telemetry-ui.desktop}"
dim "  Wrapper scripts: $BIN_DIR/telemetry-agent, $BIN_DIR/telemetry-ui"
dim "  Python sources : $BIN_DIR/linux_telemetry_agent.py, $BIN_DIR/linux_telemetry_ui.py"
if [ "$KEEP_DATA" -eq 0 ]; then
    dim "  Data + logs    : $DATA_DIR  (config.json, status.json, cache.json, agent.log, backup.db, ...)"
    dim "  Legacy config  : $CONFIG_DIR_LEGACY"
fi
echo ""

# ── Confirm ─────────────────────────────────────────────────────────────────
if [ "$ASSUME_YES" -eq 0 ]; then
    echo -ne "${BOLD}Proceed? [y/N]${NC} "
    read -r ans
    case "$ans" in
        y|Y|yes|YES) ;;
        *) echo "Aborted."; exit 1 ;;
    esac
fi

# ── 1. Stop systemd units first (idempotent — ignore "unit not loaded") ──────
hdr "1. Stopping systemd units"
if command -v systemctl &>/dev/null; then
    if systemctl --user status telemetry-agent.service &>/dev/null 2>&1; then
        systemctl --user disable --now telemetry-agent.service         2>/dev/null && info "Stopped telemetry-agent.service"
        systemctl --user disable --now telemetry-agent-watchdog.timer  2>/dev/null && info "Stopped telemetry-agent-watchdog.timer"
    else
        dim "  No active systemd user units found"
    fi
    # Best-effort cleanup of any stuck wrapper-launched processes
    pkill -f linux_telemetry_agent.py 2>/dev/null && dim "  Killed stray linux_telemetry_agent.py"  || true
    pkill -f linux_telemetry_ui.py    2>/dev/null && dim "  Killed stray linux_telemetry_ui.py"     || true
    # Reload so the daemon sees the unit is gone after we rm the file
    systemctl --user daemon-reload 2>/dev/null || true
else
    warn "systemctl not available — relying on pkill"
    pkill -f linux_telemetry_agent.py 2>/dev/null || true
    pkill -f linux_telemetry_ui.py    2>/dev/null || true
fi

# ── 2. Remove systemd unit files ───────────────────────────────────────────
hdr "2. Removing systemd unit files"
for fname in telemetry-agent.service \
             telemetry-agent-watchdog.service \
             telemetry-agent-watchdog.timer; do
    f="$SYSTEMD_USER_DIR/$fname"
    if [ -f "$f" ]; then
        rm -f "$f" && info "Removed $f" || warn "Failed to remove $f"
    else
        dim "  Skipped $f (not present)"
    fi
done

# ── 3. Remove XDG autostart .desktop entries ────────────────────────────────
hdr "3. Removing XDG autostart entries"
for fname in telemetry-agent.desktop telemetry-ui.desktop; do
    f="$AUTOSTART_DIR/$fname"
    if [ -f "$f" ]; then
        rm -f "$f" && info "Removed $f" || warn "Failed to remove $f"
    else
        dim "  Skipped $f (not present)"
    fi
done

# ── 4. Remove wrapper scripts and source .py files ─────────────────────────
hdr "4. Removing wrapper scripts and source files"
for fname in telemetry-agent telemetry-ui \
             linux_telemetry_agent.py linux_telemetry_ui.py; do
    f="$BIN_DIR/$fname"
    if [ -e "$f" ]; then
        rm -f "$f" && info "Removed $f" || warn "Failed to remove $f"
    else
        dim "  Skipped $f (not present)"
    fi
done

# ── 5. Remove data directory (unless --keep-data) ───────────────────────────
hdr "5. Removing data directory"
if [ "$KEEP_DATA" -eq 1 ]; then
    warn "--keep-data: leaving $DATA_DIR in place"
    if [ -d "$DATA_DIR" ]; then
        dim "  Contents preserved:"
        ls -la "$DATA_DIR" 2>/dev/null | sed 's/^/    /' || true
    fi
else
    if [ -d "$DATA_DIR" ]; then
        # Show what we're about to drop
        dim "  Contents being removed:"
        ls -la "$DATA_DIR" 2>/dev/null | sed 's/^/    /' || true
        rm -rf "$DATA_DIR" && info "Removed $DATA_DIR" || warn "Failed to remove $DATA_DIR"
    else
        dim "  Skipped $DATA_DIR (not present)"
    fi
    if [ -d "$CONFIG_DIR_LEGACY" ]; then
        rm -rf "$CONFIG_DIR_LEGACY" && info "Removed $CONFIG_DIR_LEGACY" || warn "Failed to remove $CONFIG_DIR_LEGACY"
    fi
fi

# ── 6. Optional: remove system packages ─────────────────────────────────────
hdr "6. System packages"
if [ "$PURGE_PKGS" -eq 1 ]; then
    if command -v apt-get &>/dev/null; then
        warn "Running: sudo apt-get purge -y xdotool xprintidle x11-utils gir1.2-wnck-4.0 gir1.2-wnck-3.0"
        sudo apt-get purge -y xdotool xprintidle x11-utils gir1.2-wnck-4.0 gir1.2-wnck-3.0 2>/dev/null \
            && info "Purged apt packages" || warn "apt purge failed (continuing)"
    elif command -v dnf &>/dev/null; then
        warn "Running: sudo dnf remove -y xdotool xprintidle xorg-x11-utils gir1.2-wnck-3.0"
        sudo dnf remove -y xdotool xprintidle xorg-x11-utils gir1.2-wnck-3.0 2>/dev/null \
            && info "Removed dnf packages" || warn "dnf remove failed (continuing)"
    elif command -v pacman &>/dev/null; then
        warn "Running: sudo pacman -Rns --noconfirm xdotool xorg-xprintidle xorg-xproputils libwnck3"
        sudo pacman -Rns --noconfirm xdotool xorg-xprintidle xorg-xproputils libwnck3 2>/dev/null \
            && info "Removed pacman packages" || warn "pacman -Rns failed (continuing)"
    else
        warn "No supported package manager (apt/dnf/pacman) found — skipping --purge"
    fi
else
    dim "  Skipped (use --purge to also remove xdotool, xprintidle, x11-utils, gir1.2-wnck-*)"
fi

# ── 7. Verify ──────────────────────────────────────────────────────────────
hdr "7. Verifying removal"
LEFT=0
for p in \
    "$SYSTEMD_USER_DIR/telemetry-agent.service" \
    "$SYSTEMD_USER_DIR/telemetry-agent-watchdog.service" \
    "$SYSTEMD_USER_DIR/telemetry-agent-watchdog.timer" \
    "$AUTOSTART_DIR/telemetry-agent.desktop" \
    "$AUTOSTART_DIR/telemetry-ui.desktop" \
    "$BIN_DIR/telemetry-agent" \
    "$BIN_DIR/telemetry-ui" \
    "$BIN_DIR/linux_telemetry_agent.py" \
    "$BIN_DIR/linux_telemetry_ui.py"
do
    [ -e "$p" ] && { warn "Still present: $p"; LEFT=$((LEFT+1)); }
done
if [ "$KEEP_DATA" -eq 0 ] && [ -d "$DATA_DIR" ]; then
    warn "Still present: $DATA_DIR"; LEFT=$((LEFT+1))
fi
if [ "$LEFT" -eq 0 ]; then
    info "All layers removed cleanly."
    echo ""
    echo -e "${BOLD}${GRN}Uninstall complete.${NC} Reinstall with:"
    echo "  curl -fsSL <your-server>/install-script-linux | bash -s -- --server <your-server>"
    echo ""
    exit 0
else
    err "$LEFT layer(s) still present — check warnings above"
    echo ""
    echo -e "${BOLD}${YLW}Uninstall finished with leftovers.${NC} They are usually harmless."
    echo "  Reinstall: curl -fsSL <your-server>/install-script-linux | bash"
    exit 1
fi
