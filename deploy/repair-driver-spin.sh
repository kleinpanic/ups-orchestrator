#!/usr/bin/env bash
# Stop a usbhid-ups driver burning a core on broken Interrupt In reports.
#
# Symptom this exists for, measured on this host: `usbhid-ups -a cyberpower3`
# held ~81% of a core CONTINUOUSLY for two days (810 jiffies per 10s wall) while
# the two sibling drivers on the same box used literally 0%. The driver was not
# hung -- `upsc cyberpower3` answered in 15ms throughout -- so nothing alerted and
# nothing looked broken. It just quietly ate a fifth of a 4-core Pi, which is the
# machine whose whole job is to stay up through an outage.
#
# Cause: some CyberPower units emit Interrupt In reports that usbhid-ups cannot
# map. It reads one, discards it, and immediately reads again. NUT's own manual
# names the remedy: "pollonly ... not recommended, but needed if these reports are
# broken on your UPS". Polling still happens on the normal pollfreq/pollinterval
# cadence, so monitoring fidelity is unchanged -- only the interrupt path is
# skipped.
#
# Note it is per-UPS: cyberpower and cyberpower3 are the SAME model and vendor id
# (0764:0601) and only one of them misbehaves, so this must never be applied
# blanket-wise. Pass the NUT name of the one that is actually spinning.
#
# Idempotent: re-running when `pollonly` is already present changes nothing and
# skips the restart.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONF="${UPS_CONF:-/etc/nut/ups.conf}"
UPS="${1:-}"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo make nut-repair-driver-spin UPS=<name>" >&2
  exit 1
fi
if [ -z "$UPS" ]; then
  echo "usage: sudo make nut-repair-driver-spin UPS=<nut-name>" >&2
  echo "find the offender with: make nut-driver-cpu" >&2
  exit 2
fi
# Same reasoning as repair-upsd-listen.sh T-03-08: this value is written verbatim
# into a root-owned NUT config, so anything that is not a bare section name is
# refused before it can reach the file.
case "$UPS" in
  *[!a-zA-Z0-9_-]*|"") echo "refusing UPS name $UPS: expected [A-Za-z0-9_-]+ only" >&2; exit 2 ;;
esac
[ -f "$CONF" ] || { echo "$CONF does not exist" >&2; exit 1; }
grep -qE "^\s*\[$UPS\]\s*$" "$CONF" || { echo "no [$UPS] section in $CONF" >&2; exit 2; }

# EXACT string comparison, never `$0 ~ ups`: awk would read "[cyberpower3]" as a
# regex CHARACTER CLASS, which matches the line "[cyberpower]" too. A dry run caught
# that adding pollonly to the healthy sibling and leaving the spinning one alone --
# these two are the same model and differ only by serial, so it looked plausible.
if awk -v ups="[$UPS]" '
      function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
      trim($0) ~ /^\[/ { inblk = (trim($0) == ups) }
      inblk && trim($0) == "pollonly" { found = 1 }
      END { exit !found }
    ' "$CONF"; then
  echo "[$UPS] already has pollonly; nothing to do"
  exit 0
fi

BACKUP="$CONF.bak-$(date +%s)"
cp -a -- "$CONF" "$BACKUP"
echo "backed up $CONF -> $BACKUP"

# Insert inside the target section only, immediately after its header.
TMP="$(mktemp -p "$(dirname "$CONF")" .ups.conf.XXXXXX)"
trap 'rm -f -- "$TMP"' EXIT
awk -v ups="[$UPS]" '
  function trim(s) { sub(/^[ \t]+/, "", s); sub(/[ \t]+$/, "", s); return s }
  { print }
  !done && trim($0) == ups { print "  pollonly"; done = 1 }
' "$CONF" > "$TMP"
# Preserve the original mode/owner rather than inheriting mktemp's 0600 root:root:
# NUT config is root:nut 0640 and the drivers read it as nut.
chown --reference="$CONF" -- "$TMP"
chmod --reference="$CONF" -- "$TMP"
mv -- "$TMP"  "$CONF"
trap - EXIT
echo "added pollonly to [$UPS]"

echo "restarting nut-driver@$UPS ..."
systemctl restart "nut-driver@$UPS.service"
sleep 3
systemctl is-active --quiet "nut-driver@$UPS.service" \
  || { echo "driver did NOT come back; restore with: cp -a $BACKUP $CONF" >&2; exit 1; }

# Prove the UPS is still readable before declaring success -- a driver that is
# "active" but not answering is the failure mode that matters here.
if upsc "$UPS" ups.status >/dev/null 2>&1; then
  echo "driver restarted and $UPS still answers: $(upsc "$UPS" ups.status 2>/dev/null)"
else
  echo "driver is active but $UPS does not answer; restore with: cp -a $BACKUP $CONF" >&2
  exit 1
fi
echo "measure the result with: make nut-driver-cpu"
