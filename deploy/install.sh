#!/usr/bin/env bash
# System install for ups-orchestrator. Run as root (e.g. `sudo deploy/install.sh`).
#
# Installs the orchestrator to a system venv reachable by the NUT `nut` user
# (the repo lives under a 0700 home, which `nut` cannot enter), wires config/
# state/secret under /etc + /var/lib, and grants the run-user (for the --user
# tick timer) access via ACLs. NUT config files are reviewed/merged by hand.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-klein}"
VENV=/opt/ups-orchestrator/venv

echo ">> backing up /etc/nut"
cp -a /etc/nut "/root/nut.bak-$(date +%Y%m%d-%H%M%S)"

echo ">> system venv install"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$REPO"
ln -sf "$VENV/bin/ups-orchestrator" /usr/local/bin/ups-orchestrator

echo ">> config / state / dispatcher"
install -d -o root -g nut -m 0750 /etc/ups-orchestrator
[ -f /etc/ups-orchestrator/config.json ] \
  || install -o root -g nut -m 0640 "$REPO/config.example.json" /etc/ups-orchestrator/config.json
install -d -o nut -g nut -m 0775 /var/lib/ups-orchestrator
install -o root -g root -m 0755 "$REPO/deploy/upssched-cmd.sh" /usr/local/bin/upssched-cmd.sh

if [ ! -f /etc/ups-orchestrator.env ]; then
  install -o root -g nut -m 0640 "$REPO/deploy/ups-orchestrator.env.example" /etc/ups-orchestrator.env
  echo "   >>> EDIT /etc/ups-orchestrator.env with your real webhook <<<"
fi

echo ">> ACLs so '$RUN_USER' (the --user tick timer) can read config/env + write state"
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator.env
setfacl -m u:"$RUN_USER":rx /etc/ups-orchestrator
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator/config.json
setfacl -R -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator
setfacl -d -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator

echo ">> NUT: registering second UPS in ups.conf (idempotent)"
grep -q '^\[cyberpower2\]' /etc/nut/ups.conf || cat "$REPO/deploy/nut/ups.conf.snippet" >> /etc/nut/ups.conf

cat <<EOF

Almost done. Review & apply the remaining NUT snippets by hand, then restart:
  - /etc/nut/upssched.conf : use deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT)
  - /etc/nut/upsmon.conf   : add MONITOR for cyberpower2 (deploy/nut/upsmon.conf.snippet)

  systemctl restart nut-driver-enumerator nut-server nut-monitor
  upsc -l            # expect: cyberpower AND cyberpower2

Then enable the on-battery countdown timer (NO sudo):
  deploy/install-user-timer.sh
EOF
