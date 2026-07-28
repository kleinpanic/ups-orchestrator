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

# T-03-08: UPSD_LAN_IP is written verbatim into a NUT config as root. A newline
# would inject an arbitrary directive, so reject anything that is not a bare
# address before it can reach the file.
# Delegates to the stdlib rather than a character-class guess: the earlier
# `case` accepted `...`, `::::` and `1.2.3.4.5.6` while its error message claimed
# they were not bare IPs, and it rejected a legitimate IPv6 zone id.
_valid_addr() {
  "$PY" -P -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1].split("%")[0])' "$1" 2>/dev/null
}

# Reuse whatever LAN address is already configured. If there is none, pass
# loopback as the LAN address too — upsert_upsd_listen dedupes, so the result is
# a single loopback LISTEN rather than a bogus second line.
LAN_IP="${UPSD_LAN_IP:-$(awk '$1=="LISTEN" && $2!="127.0.0.1" && $2!="::1" {print $2; exit}' "$CONF")}"
LAN_IP="${LAN_IP:-127.0.0.1}"
_valid_addr "$LAN_IP" || { echo "refusing: LAN address $LAN_IP is not a bare IP" >&2; exit 1; }
case "$PORT" in ''|*[!0-9]*) echo "refusing: port $PORT is not numeric" >&2; exit 1 ;; esac

BACKUP="$CONF.bak-$(date +%s)"
cp -p "$CONF" "$BACKUP"

# Written in place (not via a temp + rename) so the destination inode keeps its
# mode, owner and ACL — the metadata-destroying replace this repo already had to
# fix once in state.py (T-02-23). The backup above covers a crash mid-write, and
# T-03-05 is why the exit codes below are distinct: an exception and a no-op both
# exited non-zero before, so a TRUNCATED file took the "nothing to do" branch,
# deleted its own backup, and then restarted nut-server against the wreckage.
#
# `-P` (T-03-01) keeps CWD off sys.path. Without it root imports from whatever
# directory the caller happened to be in — including ones uid `nut` owns.
set +e
"$PY" -P - "$CONF" "$LAN_IP" "$PORT" <<'PYEOF'
import pathlib
import sys

from ups_orchestrator.nutclient import upsert_upsd_listen

conf, lan_ip, port = pathlib.Path(sys.argv[1]), sys.argv[2], int(sys.argv[3])
new_text, changed = upsert_upsd_listen(conf.read_text(), lan_ip, port)
if not changed:
    sys.exit(10)
with conf.open("w") as handle:
    handle.write(new_text)
sys.exit(0)
PYEOF
rc=$?
set -e

CHANGED=0
case "$rc" in
  0)  CHANGED=1; echo "upsd.conf: loopback LISTEN added (backup: $BACKUP)" ;;
  10) rm -f "$BACKUP"; echo "upsd.conf: loopback LISTEN already present — nothing to do" ;;
  *)  cp -p "$BACKUP" "$CONF"
      echo "rewrite FAILED (rc=$rc) — $CONF restored from $BACKUP, nut-server NOT restarted" >&2
      exit 1 ;;
esac

echo "--- active LISTEN lines ---"
grep -E '^\s*LISTEN' "$CONF" || echo "(none)"

# Restart only when the file actually changed, or when nut-server is not
# currently up. The header promised this and the first version restarted
# unconditionally — dropping every upsmon connection on a host whose restart
# limit had already killed nut-server once.
if [ "$CHANGED" -eq 1 ] || ! systemctl is-active nut-server >/dev/null 2>&1; then
  systemctl reset-failed nut-server 2>/dev/null || true
  systemctl restart nut-server
  sleep 2
else
  echo "nut-server: already active and config unchanged — not restarting"
fi

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
