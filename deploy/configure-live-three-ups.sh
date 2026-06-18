#!/usr/bin/env bash
# Configure this host for the three attached CyberPower UPSes, then redeploy.
#
# Run as root:
#   sudo deploy/configure-live-three-ups.sh
#
# This script does not contain webhook URLs, NUT passwords, or UPS serials.
# It discovers serials with `nut-scanner -U` and preserves the existing
# upsmon credential from /etc/nut/upsmon.conf.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "ERROR: run as root, e.g. sudo $0" >&2
  exit 1
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-klein}"
VENV=/opt/ups-orchestrator/venv

echo ">> backing up live config"
install -d -m 0700 /root/ups-orchestrator-backups
cp -a /etc/nut "/root/ups-orchestrator-backups/nut.$(date +%Y%m%d-%H%M%S)"
[ ! -f /etc/ups-orchestrator/config.json ] || \
  cp -a /etc/ups-orchestrator/config.json \
    "/root/ups-orchestrator-backups/config.$(date +%Y%m%d-%H%M%S).json"

echo ">> discovering attached CyberPower UPSes"
python3 - "$REPO" <<'PY'
from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

repo = Path(sys.argv[1])


def scanner_devices() -> list[dict[str, str]]:
    out = subprocess.check_output(["nut-scanner", "-U"], text=True, stderr=subprocess.STDOUT)
    devices: list[dict[str, str]] = []
    current: dict[str, str] | None = None
    for raw in out.splitlines():
        line = raw.strip()
        if re.fullmatch(r"\[nutdev\d+\]", line):
            if current:
                devices.append(current)
            current = {}
            continue
        if current is None or "=" not in line:
            continue
        key, value = line.split("=", 1)
        current[key.strip()] = value.strip().strip('"')
    if current:
        devices.append(current)
    return devices


def pick(devices: list[dict[str, str]], product: str, productid: str) -> dict[str, str]:
    matches = [
        dev
        for dev in devices
        if dev.get("vendorid") == "0764"
        and dev.get("productid") == productid
        and dev.get("product") == product
        and dev.get("serial")
    ]
    if len(matches) != 1:
        found = ", ".join(f"{d.get('product')}:{d.get('serial')}" for d in devices)
        raise SystemExit(f"Expected exactly one {product} ({productid}); found: {found}")
    return matches[0]


def strip_sections(text: str, section_names: set[str]) -> str:
    out: list[str] = []
    skipping = False
    section_re = re.compile(r"^\[([^]]+)\]\s*$")
    for line in text.splitlines():
        match = section_re.match(line.strip())
        if match:
            skipping = match.group(1) in section_names
        if not skipping:
            out.append(line)
    return "\n".join(out).rstrip() + "\n"


def upsert_ups_conf(devices: dict[str, dict[str, str]]) -> None:
    path = Path("/etc/nut/ups.conf")
    text = strip_sections(path.read_text(), set(devices))
    block = [
        "",
        "# --- Managed by ups-orchestrator: live three UPS config ---",
    ]
    descriptions = {
        "cyberpower": "Rack UPS - CyberPower CST150UC",
        "cyberpower2": "Loaded UPS - CyberPower ABMT1500",
        "cyberpower3": "Third UPS - CyberPower CST135UC2",
    }
    for name, dev in devices.items():
        block.extend(
            [
                f"[{name}]",
                "  driver = usbhid-ups",
                "  port = auto",
                f"  vendorid = {dev['vendorid']}",
                f"  productid = {dev['productid']}",
                f"  serial = {dev['serial']}",
                f"  desc = \"{descriptions[name]}\"",
            ]
        )
        if name == "cyberpower2":
            block.append("  onlinedischarge")
        block.append("")
    block.append("# --- End managed by ups-orchestrator ---")
    path.write_text(text + "\n".join(block) + "\n")


def existing_monitor_creds(text: str) -> tuple[str, str, str]:
    monitor_re = re.compile(r"^MONITOR\s+\S+\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)", re.M)
    preferred = re.search(
        r"^MONITOR\s+cyberpower2@localhost\s+\d+\s+(\S+)\s+(\S+)\s+(\S+)",
        text,
        re.M,
    )
    match = preferred or monitor_re.search(text)
    if not match:
        raise SystemExit("No MONITOR line found in /etc/nut/upsmon.conf; add credentials first")
    return match.group(1), match.group(2), match.group(3)


def upsert_upsmon_conf() -> None:
    path = Path("/etc/nut/upsmon.conf")
    text = path.read_text()
    user, password, role = existing_monitor_creds(text)
    text = re.sub(r"^MONITOR\s+cyberpower[23]?@localhost\s+.*\n?", "", text, flags=re.M)
    block = "\n".join(
        [
            "",
            "# --- Managed by ups-orchestrator: live three UPS monitors ---",
            "# Only cyberpower2 currently feeds this host; the others are notification-only.",
            f"MONITOR cyberpower@localhost 0 {user} {password} {role}",
            f"MONITOR cyberpower2@localhost 1 {user} {password} {role}",
            f"MONITOR cyberpower3@localhost 0 {user} {password} {role}",
            "# --- End managed by ups-orchestrator ---",
            "",
        ]
    )
    path.write_text(text.rstrip() + block)


def upsert_orchestrator_config() -> None:
    path = Path("/etc/ups-orchestrator/config.json")
    if path.exists():
        data = json.loads(path.read_text())
    else:
        data = json.loads((repo / "config.example.json").read_text())
    data.pop("shutdown_scope", None)
    data["shutdown"] = {
        "enabled": False,
        "require_power_outage": True,
        "min_on_battery_seconds": 120,
        "notify": True,
        "external": {
            "enabled": False,
            "battery_below": 15,
            "runtime_below": 300,
        },
        "internal": {
            "enabled": False,
            "battery_below": 10,
            "runtime_below": 120,
        },
    }
    upses = data.setdefault("upses", {})
    upses.setdefault("cyberpower", {})
    upses["cyberpower"]["label"] = "Rack UPS - CyberPower CST150UC"
    upses["cyberpower"]["shutdown_targets"] = []
    upses["cyberpower"].pop("shutdown_scope", None)
    upses.setdefault("cyberpower2", {})
    upses["cyberpower2"]["label"] = "Loaded UPS - CyberPower ABMT1500"
    upses["cyberpower2"]["shutdown_targets"] = []
    upses["cyberpower2"].pop("shutdown_scope", None)
    upses.setdefault("cyberpower3", {})
    upses["cyberpower3"]["label"] = "Third UPS - CyberPower CST135UC2"
    upses["cyberpower3"]["shutdown_targets"] = []
    upses["cyberpower3"].pop("shutdown_scope", None)
    path.write_text(json.dumps(data, indent=2) + "\n")


devices_all = scanner_devices()
devices = {
    "cyberpower": pick(devices_all, "CST150UC", "0601"),
    "cyberpower2": pick(devices_all, "ABMT1500", "0501"),
    "cyberpower3": pick(devices_all, "CST135UC2", "0601"),
}
for name, dev in devices.items():
    print(f"   {name}: {dev['product']} serial={dev['serial']}")

upsert_ups_conf(devices)
upsert_upsmon_conf()
upsert_orchestrator_config()
PY

echo ">> installing current repo to /opt"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$REPO"
ln -sf "$VENV/bin/ups-orchestrator" /usr/local/bin/ups-orchestrator
install -o root -g root -m 0755 "$REPO/deploy/upssched-cmd.sh" /usr/local/bin/upssched-cmd.sh

echo ">> ensuring ACLs for $RUN_USER"
install -d -o root -g nut -m 0750 /etc/ups-orchestrator
install -d -o nut -g nut -m 0775 /var/lib/ups-orchestrator
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator.env 2>/dev/null || true
setfacl -m u:"$RUN_USER":rx /etc/ups-orchestrator 2>/dev/null || true
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator/config.json 2>/dev/null || true
setfacl -R -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator 2>/dev/null || true
setfacl -d -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator 2>/dev/null || true

echo ">> restarting NUT"
systemctl daemon-reload
systemctl restart nut-driver-enumerator nut-server nut-monitor

echo ">> installing user services and daily report timer"
loginctl enable-linger "$RUN_USER" || true
RUN_UID="$(id -u "$RUN_USER")"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
runuser -u "$RUN_USER" -- env HOME="$RUN_HOME" XDG_RUNTIME_DIR="/run/user/$RUN_UID" \
  "$REPO/deploy/install-user-service.sh"

echo ">> NUT inventory"
upsc -l

echo ">> current report preview"
set -a
. /etc/ups-orchestrator.env
set +a
/usr/local/bin/ups-orchestrator report --print

echo ">> sending Discord report webhook"
/usr/local/bin/ups-orchestrator report

echo "Done."
