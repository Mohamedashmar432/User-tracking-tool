#!/usr/bin/env bash
# build-deb.sh — Build a .deb package for the Linux telemetry agent + UI.
#
# Output: dist/telemetry-agent_<version>_all.deb
#
# The .deb installs:
#   /usr/lib/telemetry-agent/linux_telemetry_agent.py
#   /usr/lib/telemetry-agent/linux_telemetry_ui.py
#   /usr/lib/telemetry-agent/requirements-linux.txt
#   /usr/share/telemetry-agent/install.sh
#   /usr/bin/telemetry-agent  (wrapper invoking system python3 + pip venv per user)
#   /usr/bin/telemetry-ui
#
# Post-install script (debian/postinst) is called at package install time to
# create per-user venv + run --install when the package is installed as root
# (enterprise push scenario).

set -euo pipefail

VERSION="1.0"
ARCH="all"
PACKAGE="telemetry-agent"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$SCRIPT_DIR/.."
DIST_DIR="$ROOT_DIR/dist"
PKG_DIR="$DIST_DIR/${PACKAGE}_${VERSION}_${ARCH}"

echo "[deb] Building $PACKAGE $VERSION ..."

rm -rf "$PKG_DIR"
mkdir -p \
    "$PKG_DIR/DEBIAN" \
    "$PKG_DIR/usr/lib/telemetry-agent" \
    "$PKG_DIR/usr/share/telemetry-agent" \
    "$PKG_DIR/usr/bin"

# ── Copy payload files ─────────────────────────────────────────────────────────────
cp "$ROOT_DIR/linux_telemetry_agent.py" "$PKG_DIR/usr/lib/telemetry-agent/"
cp "$ROOT_DIR/linux_telemetry_ui.py"    "$PKG_DIR/usr/lib/telemetry-agent/"
cp "$SCRIPT_DIR/requirements-linux.txt" "$PKG_DIR/usr/lib/telemetry-agent/"
cp "$SCRIPT_DIR/install.sh"             "$PKG_DIR/usr/share/telemetry-agent/"
chmod +x "$PKG_DIR/usr/share/telemetry-agent/install.sh"

# ── Wrapper scripts ────────────────────────────────────────────────────────────────
cat > "$PKG_DIR/usr/bin/telemetry-agent" <<'EOF'
#!/bin/bash
VENV="$HOME/.local/share/telemetry-agent/venv"
if [[ ! -f "$VENV/bin/python" ]]; then
    echo "[telemetry-agent] No venv found. Run: /usr/share/telemetry-agent/install.sh" >&2
    exit 1
fi
exec "$VENV/bin/python" /usr/lib/telemetry-agent/linux_telemetry_agent.py "$@"
EOF

cat > "$PKG_DIR/usr/bin/telemetry-ui" <<'EOF'
#!/bin/bash
VENV="$HOME/.local/share/telemetry-agent/venv"
if [[ ! -f "$VENV/bin/python" ]]; then
    echo "[telemetry-ui] No venv found. Run: /usr/share/telemetry-agent/install.sh" >&2
    exit 1
fi
exec "$VENV/bin/python" /usr/lib/telemetry-agent/linux_telemetry_ui.py "$@"
EOF

chmod +x "$PKG_DIR/usr/bin/telemetry-agent" "$PKG_DIR/usr/bin/telemetry-ui"

# ── DEBIAN/control ─────────────────────────────────────────────────────────────────
cat > "$PKG_DIR/DEBIAN/control" <<EOF
Package: $PACKAGE
Version: $VERSION
Architecture: $ARCH
Maintainer: TelemetryAgent <admin@example.com>
Depends: python3 (>= 3.9), python3-pip, python3-venv, xdotool, dbus-x11
Recommends: xprintidle
Section: utils
Priority: optional
Description: Linux user activity telemetry agent and tray UI
 Tracks active application time, idle time, and productivity score.
 Syncs to a central telemetry server. Works offline via local SQLite cache.
EOF

# ── DEBIAN/postinst ────────────────────────────────────────────────────────────────
cat > "$PKG_DIR/DEBIAN/postinst" <<'POSTINST'
#!/bin/bash
set -e
echo "[telemetry-agent] Package installed."
echo "[telemetry-agent] To complete per-user setup, run as your regular user:"
echo "  /usr/share/telemetry-agent/install.sh --server-url https://your-server"
POSTINST
chmod 755 "$PKG_DIR/DEBIAN/postinst"

# ── Build ──────────────────────────────────────────────────────────────────────────
dpkg-deb --build "$PKG_DIR"
echo "[deb] Built: $DIST_DIR/${PACKAGE}_${VERSION}_${ARCH}.deb"
