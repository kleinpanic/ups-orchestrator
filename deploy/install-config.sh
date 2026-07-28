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
[ -x "$PY" ] || { echo "no venv at $PY — run 'make venv' first" >&2; exit 1; }
[ -e "$SRC" ] || { echo "no such config: $SRC" >&2; exit 1; }

# T-03-03: validate-then-install across two dereferences of an attacker-supplied
# path is a TOCTOU. `install` and `[ -f ]` both follow symlinks, so swapping $SRC
# between the two steps landed any root-readable file in /etc as the live config.
# Refuse a symlink outright, then stage the bytes into a root-owned file and
# validate and install THAT — so what was checked is what lands, always.
[ -L "$SRC" ] && { echo "refusing: $SRC is a symlink" >&2; exit 1; }
[ -f "$SRC" ] || { echo "refusing: $SRC is not a regular file" >&2; exit 1; }

STAGE="$(mktemp -p "$(dirname "$DEST")" .config.stage.XXXXXX)"
trap 'rm -f "$STAGE"' EXIT
chmod 0600 "$STAGE"
cat -- "$SRC" >"$STAGE"

echo "--- validating $SRC ---"
# `-P` (T-03-01) keeps CWD off sys.path so root cannot be made to import an
# attacker's module from whatever directory the caller happened to be in.
UPS_ORCH_CONFIG="$STAGE" "$PY" -P - "$STAGE" <<'PYEOF'
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

# T-03-02: `install` unlinks the destination and creates a NEW inode, which drops
# every ACL entry — the same class as T-02-23, which state.py fixed on the Python
# side only. Capture the destination's real ACL and restore it, rather than
# re-deriving a single entry from ambient $SUDO_USER and silently losing the rest.
ACL_SAVE=""
if [ -f "$DEST" ]; then
  ACL_SAVE="$(getfacl -p -- "$DEST" 2>/dev/null || true)"
  BACKUP="$DEST.bak-$(date +%s)"
  cp -p "$DEST" "$BACKUP"
  echo "backed up live config to $BACKUP"
fi

install -o root -g nut -m 0640 "$STAGE" "$DEST"

if [ -n "$ACL_SAVE" ]; then
  printf '%s\n' "$ACL_SAVE" | setfacl --restore=- \
    || { echo "ACL restore failed; falling back to u:$RUN_USER:r" >&2
         setfacl -m "u:$RUN_USER:r" "$DEST"; }
else
  setfacl -m "u:$RUN_USER:r" "$DEST"
fi

echo "installed $SRC -> $DEST"
ls -l "$DEST"
getfacl -p "$DEST" 2>/dev/null | grep -E "^user:" || true
