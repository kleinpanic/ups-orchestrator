#!/usr/bin/env bash
# Guarantee /etc/nut/upsd.conf keeps a loopback LISTEN, then restart nut-server.
#
# upsd listens on localhost:3493 ONLY when upsd.conf has no LISTEN statement at
# all, and Debian ships the file with every LISTEN commented out. Adding the
# first explicit LISTEN silently replaces that implicit default, which broke this
# host twice: every bare `upsc` was refused for two days, and then a boot where
# eth0 had no DHCP lease yet left upsd with nothing to bind, so it exited and
# systemd's restart limit killed nut-server outright.
#
# Idempotent: re-running when loopback is already active changes nothing and
# skips the restart. The rewrite goes through the same tested pure function the
# daemon uses (nutclient.upsert_upsd_listen), so there is one implementation.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python3"
CONF="${UPSD_CONF:-/etc/nut/upsd.conf}"
PORT="${UPSD_PORT:-3493}"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo make nut-repair-listen" >&2
  exit 1
fi
[ -x "$PY" ] || { echo "no venv at $PY — run 'make venv' first" >&2; exit 1; }
[ -f "$CONF" ] || { echo "$CONF does not exist" >&2; exit 1; }

# Reuse whatever LAN address is already configured. If there is none, pass
# loopback as the LAN address too — upsert_upsd_listen dedupes, so the result is
# a single loopback LISTEN rather than a bogus second line.
LAN_IP="${UPSD_LAN_IP:-$(awk '$1=="LISTEN" && $2!="127.0.0.1" && $2!="::1" {print $2; exit}' "$CONF")}"
LAN_IP="${LAN_IP:-127.0.0.1}"

BACKUP="$CONF.bak-$(date +%s)"
cp -p "$CONF" "$BACKUP"

# Written in place (not via a temp + rename) so the destination inode keeps its
# mode, owner and ACL — the metadata-destroying replace this repo already had to
# fix once in state.py. The backup above covers a crash mid-write.
if "$PY" - "$CONF" "$LAN_IP" "$PORT" <<'PYEOF'
import pathlib
import sys

from ups_orchestrator.nutclient import upsert_upsd_listen

conf, lan_ip, port = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
new_text, changed = upsert_upsd_listen(conf.read_text(), lan_ip, port)
if changed:
    with conf.open("w") as handle:
        handle.write(new_text)
sys.exit(0 if changed else 1)
PYEOF
then
  echo "upsd.conf: loopback LISTEN added (backup: $BACKUP)"
else
  rm -f "$BACKUP"
  echo "upsd.conf: loopback LISTEN already present — nothing to do"
fi

echo "--- active LISTEN lines ---"
grep -E '^\s*LISTEN' "$CONF" || echo "(none)"

systemctl reset-failed nut-server 2>/dev/null || true
systemctl restart nut-server
sleep 2

echo "--- verification ---"
systemctl is-active nut-server >/dev/null 2>&1 \
  && echo "nut-server: active" \
  || { echo "nut-server: FAILED to start — see 'journalctl -u nut-server -n 30'" >&2; exit 1; }

# The whole point of the loopback line: a bare upsc (no host) must work again.
if upsc -l >/dev/null 2>&1; then
  echo "upsc -l (localhost): OK"
  upsc -l
else
  echo "upsc -l (localhost): STILL REFUSED — the repair did not take" >&2
  exit 1
fi
