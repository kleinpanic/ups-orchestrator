#!/usr/bin/env bash
# Install the on-battery countdown as a systemd --user timer. Run as your normal
# user (NO sudo). Requires deploy/install.sh to have run first (system install +
# /etc/ups-orchestrator.env + ACLs), and `loginctl enable-linger $USER` so it
# runs while logged out.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"

mkdir -p "$UNIT_DIR"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-tick.service" "$UNIT_DIR/"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-tick.timer"   "$UNIT_DIR/"

systemctl --user daemon-reload
systemctl --user enable --now ups-orchestrator-tick.timer
systemctl --user list-timers ups-orchestrator-tick.timer --no-pager

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — run 'loginctl enable-linger $USER' so the timer runs while logged out."
fi
