#!/usr/bin/env bash
# System install for ups-orchestrator. Run as root (e.g. `sudo deploy/install.sh`).
#
# Installs the orchestrator to a system venv reachable by the NUT `nut` user
# (the repo lives under a 0700 home, which `nut` cannot enter), wires config/
# state/secret under /etc + /var/lib, grants the run-user (for the --user watch
# service) access via ACLs, and allows it passwordless shutdown for local
# shutdown_targets. NUT config files are reviewed/merged by hand.
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(logname 2>/dev/null || echo "${USER:-root}")}"
VENV=/opt/ups-orchestrator/venv

echo ">> backing up /etc/nut"
cp -a /etc/nut "/root/nut.bak-$(date +%Y%m%d-%H%M%S)"

echo ">> system venv install"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q --force-reinstall --no-deps "$REPO"
# /usr/local/bin is on every user's PATH incl. the `nut` user that runs the
# upssched dispatcher during an outage — it cannot enter the 0700 repo home, so
# the binary must live on a system path, not ~/.local/bin.
ln -sf "$VENV/bin/ups-orchestrator" /usr/local/bin/ups-orchestrator

echo ">> granting '$RUN_USER' perms so future redeploys need no sudo"
# Own the venv as the install user (group nut so the `nut` user keeps rx), and
# add the user to nut. After this, `make deploy-code` redeploys without root.
chown -R "$RUN_USER":nut /opt/ups-orchestrator
chmod -R g+rX /opt/ups-orchestrator
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx nut || { usermod -aG nut "$RUN_USER"; echo "   added $RUN_USER to nut (re-login for it to take effect)"; }
# IF-03: the serial shutdown transport opens /dev/ttyUSB*//dev/ttyS* "wb" from the
# same RUN_USER-owned `systemd --user` watch unit, and Debian puts those devices in
# group `dialout`. Every other privilege the daemon needs is granted above or below
# (nut group, /etc + /var/lib ACLs, poweroff sudoers); this one was not, so on a
# fresh install every serial push and every `ups-orchestrator shutdown rehearse`
# died with PermissionError on the device open. It works on the box this was
# developed against only because that user already happened to be in dialout.
#
# The `nut` user is deliberately NOT added. The upssched dispatcher runs as `nut`,
# but the shipped upssched.conf.snippet has no rule that reaches the push gate
# (ONBATT/LOWBATT/COMMBAD/COMMOK only — see docs/Shutdown-Mechanisms.md), so the
# serial push is unreachable from that path. An operator who adds an
# `AT ... EXECUTE remote_shutdown` line must grant `nut` dialout as well.
id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx dialout || { usermod -aG dialout "$RUN_USER"; echo "   added $RUN_USER to dialout (serial shutdown transport device access; re-login for it to take effect)"; }
# Convenience symlink on the user's own PATH (does not replace the system one).
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
[ -n "$RUN_HOME" ] && install -d -o "$RUN_USER" -g "$RUN_USER" "$RUN_HOME/.local/bin" \
  && ln -sf "$VENV/bin/ups-orchestrator" "$RUN_HOME/.local/bin/ups-orchestrator" \
  && chown -h "$RUN_USER":"$RUN_USER" "$RUN_HOME/.local/bin/ups-orchestrator"

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

echo ">> ACLs so '$RUN_USER' (the --user watch service) can read config/env + write state"
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator.env
setfacl -m u:"$RUN_USER":rx /etc/ups-orchestrator
setfacl -m u:"$RUN_USER":r  /etc/ups-orchestrator/config.json
setfacl -R -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator
setfacl -d -m u:"$RUN_USER":rwx /var/lib/ups-orchestrator

echo ">> sudoers: allow '$RUN_USER' passwordless shutdown (for local shutdown_targets)"
cat > /etc/sudoers.d/ups-orchestrator <<SUDOERS
# Lets the --user watch service power off THIS host for a local shutdown_target.
$RUN_USER ALL=(root) NOPASSWD: /sbin/shutdown, /usr/sbin/shutdown, /sbin/poweroff, /usr/bin/systemctl poweroff
SUDOERS
chmod 0440 /etc/sudoers.d/ups-orchestrator
visudo -cf /etc/sudoers.d/ups-orchestrator >/dev/null && echo "   sudoers OK" || { echo "   sudoers INVALID — removing"; rm -f /etc/sudoers.d/ups-orchestrator; }

echo ">> NUT control user (beeper + battery test; least privilege)"
"$REPO/deploy/nut-control-user.sh" || echo "   control-user setup skipped (NUT not ready?)"

echo ">> NUT: appending UPS sections to ups.conf (idempotent; edit the snippet's"
echo "   vendorid/productid first — see deploy/nut/ups.conf.snippet)"
if grep -Eq '^\[(cyberpower|ups[0-9]+)\]' /etc/nut/ups.conf; then
  echo "   existing UPS sections found; leaving /etc/nut/ups.conf unchanged"
else
  cat "$REPO/deploy/nut/ups.conf.snippet" >> /etc/nut/ups.conf
fi

cat <<EOF

Almost done. Review & apply the remaining NUT snippets by hand, then restart:
  - /etc/nut/upssched.conf : use deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT)
  - /etc/nut/upsmon.conf   : add a MONITOR line per UPS (deploy/nut/upsmon.conf.snippet)

  systemctl restart nut-driver-enumerator nut-server nut-monitor
  upsc -l            # expect all your UPSes listed

Then enable the poll loop (NO sudo):
  deploy/install-user-service.sh
EOF
