#!/usr/bin/env bash
# Install the poll loop as a systemd --user service (NO sudo). Run as your user.
# Requires deploy/install.sh first (system install + /etc env + ACLs), and
# `loginctl enable-linger $USER` so it runs while logged out.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
REAL_HOME="$(getent passwd "$(id -un)" | cut -d: -f6)"
if [ -z "${HOME:-}" ] || [ "$HOME" = "/root" ]; then
  export HOME="$REAL_HOME"
fi
case "${XDG_CONFIG_HOME:-}" in
  /root/*) unset XDG_CONFIG_HOME ;;
esac
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$UNIT_DIR"

# Remove the pre-0.3 tick timer if it's still installed.
systemctl --user disable --now ups-orchestrator-tick.timer 2>/dev/null || true
rm -f "$UNIT_DIR/ups-orchestrator-tick.service" "$UNIT_DIR/ups-orchestrator-tick.timer"

install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-watch.service" "$UNIT_DIR/"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-recorder.service" "$UNIT_DIR/"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-boot-audit.service" "$UNIT_DIR/"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-report.service" "$UNIT_DIR/"
install -m 0644 "$REPO/deploy/systemd/ups-orchestrator-report.timer" "$UNIT_DIR/"
systemctl --user daemon-reload
systemctl --user enable --now ups-orchestrator-watch.service
systemctl --user restart ups-orchestrator-watch.service
systemctl --user enable --now ups-orchestrator-recorder.service
systemctl --user restart ups-orchestrator-recorder.service
systemctl --user enable --now ups-orchestrator-boot-audit.service
systemctl --user start ups-orchestrator-boot-audit.service
systemctl --user enable --now ups-orchestrator-report.timer
systemctl --user status ups-orchestrator-watch.service --no-pager | head -5 || true
systemctl --user status ups-orchestrator-recorder.service --no-pager | head -5 || true
systemctl --user list-timers ups-orchestrator-report.timer --no-pager || true

if [ "$(loginctl show-user "$USER" -p Linger --value 2>/dev/null)" != "yes" ]; then
  echo "NOTE: linger is OFF — run 'loginctl enable-linger $USER' so it runs while logged out."
fi
