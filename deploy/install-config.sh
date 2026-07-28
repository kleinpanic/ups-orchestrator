#!/usr/bin/env bash
# Install a config.json into /etc with the canonical mode, owner and ACL.
#
# Usage: sudo make install-config CONFIG=/path/to/config.json
#
# Validates that the file actually LOADS before replacing the live one. A config
# that fails to parse is a monitoring-topology outage: `watch` would poll zero
# UPSes while looking healthy, so the check belongs in front of the install, not
# after it.
set -euo pipefail

SRC="${1:?usage: install-config.sh <config.json>}"
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PY="$REPO/.venv/bin/python3"
DEST="${UPS_ORCH_ETC_CONFIG:-/etc/ups-orchestrator/config.json}"
RUN_USER="${SUDO_USER:-${RUN_USER:-klein}}"

if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root: sudo make install-config CONFIG=..." >&2
  exit 1
fi
[ -f "$SRC" ] || { echo "no such config: $SRC" >&2; exit 1; }
[ -x "$PY" ] || { echo "no venv at $PY — run 'make venv' first" >&2; exit 1; }

echo "--- validating $SRC ---"
UPS_ORCH_CONFIG="$SRC" "$PY" - "$SRC" <<'PYEOF'
import sys

from ups_orchestrator.config import Config

cfg = Config.load(sys.argv[1])
print(f"loads OK: {len(cfg.upses)} UPS(es), {len(cfg.monitored_machines)} monitored machine(s)")
for notice in cfg.degraded:
    print(f"  {notice.severity.upper()} {notice.subject}: {notice.message}")
for name in cfg.upses:
    managed, devices = cfg.ups_inventory(name)
    print(f"  {name}: shuts down {list(managed) or 'nothing'}; "
          f"also powers {[d.name for d in devices] or 'nothing recorded'}")
PYEOF

if [ -f "$DEST" ]; then
  BACKUP="$DEST.bak-$(date +%s)"
  cp -p "$DEST" "$BACKUP"
  echo "backed up live config to $BACKUP"
fi

install -o root -g nut -m 0640 "$SRC" "$DEST"
setfacl -m "u:$RUN_USER:r" "$DEST"
echo "installed $SRC -> $DEST"
ls -l "$DEST"
getfacl -p "$DEST" 2>/dev/null | grep -E "^user:" || true
