#!/usr/bin/env bash
# Install the poll loop as a systemd --user service (NO sudo). Run as your user.
# Requires deploy/install.sh first (system install + /etc env + ACLs), and
# `loginctl enable-linger $USER` so it runs while logged out.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

# Remove the pre-0.3 tick timer if it's still installed.
systemctl --user disable --now ups-orchestrator-tick.timer 2>/dev/null || true
rm -f "$UNIT_DIR/ups-orchestrator-tick.service" "$UNIT_DIR/ups-orchestrator-tick.timer"

install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-watch.service" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now ups-orchestrator-watch.service
systemctl --user status ups-orchestrator-watch.service --no-pager | head -5 || true

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — run 'loginctl enable-linger $USER' so it runs while logged out."
fi
