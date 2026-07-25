#!/usr/bin/env bash
# Create the least-privilege NUT user the orchestrator's `control` command uses
# (beeper + battery test only — deliberately NO load.off / shutdown.* /
# killpower), wire its creds into the orchestrator env file, and reload upsd.
# Idempotent: safe to re-run. Run as root (e.g. `sudo make nut-control-user`).
#
# Pass --fire to also exercise beeper.mute + a quick battery test on every UPS
# right after setup, printing each result (the password is never echoed).
set -euo pipefail

USERS=/etc/nut/upsd.users
ENVF=/etc/ups-orchestrator.env
NUTUSER=upsctl
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo root)}"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (try: sudo make nut-control-user)" >&2
  exit 1
fi

# 1. Control user — least privilege. Reuse the existing password on re-run so
#    upsd.users and the env file never drift apart.
if grep -q '^\[upsctl\]' "$USERS"; then
  PW=$(awk '/^\[upsctl\]/{f=1;next} /^\[/{f=0} f && /password/{print $3; exit}' "$USERS")
  echo ">> upsctl already present in $USERS"
else
  PW=$(openssl rand -hex 20)
  cat >> "$USERS" <<EOF

[upsctl]
  password = $PW
  instcmds = beeper.mute
  instcmds = beeper.disable
  instcmds = beeper.enable
  instcmds = test.battery.start.quick
  instcmds = test.battery.start.deep
  instcmds = test.battery.stop
EOF
  chown root:nut "$USERS"; chmod 640 "$USERS"
  echo ">> created upsctl in $USERS"
fi

# 2. Wire creds into the orchestrator env (640 root:nut; nut-group members read).
touch "$ENVF"; chown root:nut "$ENVF"; chmod 640 "$ENVF"
grep -q '^UPS_NUT_ADMIN_USER='     "$ENVF" || echo "UPS_NUT_ADMIN_USER=$NUTUSER" >> "$ENVF"
grep -q '^UPS_NUT_ADMIN_PASSWORD=' "$ENVF" || echo "UPS_NUT_ADMIN_PASSWORD=$PW"  >> "$ENVF"
echo ">> env wired: UPS_NUT_ADMIN_USER=$NUTUSER (creds in $ENVF)"

# 3. Reload upsd so the new user takes effect.
if systemctl reload nut-server 2>/dev/null; then echo ">> nut-server reloaded"
elif upsd -c reload 2>/dev/null;            then echo ">> upsd -c reload ok"
else echo ">> WARNING: could not reload upsd — do it by hand" >&2; fi

if [ "${1:-}" = "--fire" ]; then
  sleep 1
  echo ">> live-fire (beeper.mute + quick battery test) on every UPS:"
  for U in $(upsc -l 2>/dev/null | grep -v '^Init'); do
    echo "   --- $U ---"
    printf "   beeper.mute: %s\n"              "$(upscmd -u "$NUTUSER" -p "$PW" "$U" beeper.mute 2>&1 | tail -1)"
    printf "   test.battery.start.quick: %s\n" "$(upscmd -u "$NUTUSER" -p "$PW" "$U" test.battery.start.quick 2>&1 | tail -1)"
  done
fi
