#!/usr/bin/env bash
# Remove every orchestrator-managed shutdown target from the live config.
# This does not touch webhook secrets, NUT passwords, or UPS serials.
set -euo pipefail

RUN_USER="${RUN_USER:-klein}"
CONFIG="${UPS_ORCH_CONFIG:-/etc/ups-orchestrator/config.json}"

python3 - <<'PY' "$CONFIG"
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text())
data["shutdown"] = {
    "enabled": False,
    "require_power_outage": True,
    "min_on_battery_seconds": 120,
    "notify": True,
    "external": {"enabled": False, "battery_below": 15, "runtime_below": 300},
    "internal": {"enabled": False, "battery_below": 10, "runtime_below": 120},
}
for ups in data.get("upses", {}).values():
    if isinstance(ups, dict):
        ups["shutdown_targets"] = []
        ups.pop("shutdown_scope", None)
tmp = path.with_suffix(path.suffix + ".tmp")
tmp.write_text(json.dumps(data, indent=2) + "\n")
tmp.replace(path)
PY

systemctl --user -M "$RUN_USER@" restart ups-orchestrator-watch.service 2>/dev/null || true
echo "Orchestrator shutdown targets disabled in $CONFIG"
