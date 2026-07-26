#!/usr/bin/env bash
# Remove every orchestrator-managed shutdown target from the live config.
# This does not touch webhook secrets, NUT passwords, or UPS serials.
set -euo pipefail

RUN_USER="${RUN_USER:-klein}"
CONFIG="${UPS_ORCH_CONFIG:-/etc/ups-orchestrator/config.json}"
HERE="$(cd "$(dirname "$0")" && pwd)"

# IF-02: the rewrite used to be a python3 heredoc ending in a bare
# `tmp.replace(path)` — verbatim the pattern state.replace_preserving_metadata
# exists to eliminate. Run as root against a 0640 root:nut config that holds a
# Discord webhook URL, that silently left the file world-readable with the
# installer's ACL gone, with no warning and nothing visibly broken. The rewrite now
# lives in disable-shutdown-targets.py and goes through the project's single
# writer — which means it must run under an interpreter that can import the
# package, and the system python3 cannot: the package lives in the install venv.
# Resolve that venv from the symlink install.sh puts on the PATH.
PYBIN="${UPS_ORCH_PYTHON:-}"
if [ -z "$PYBIN" ]; then
  ORCH="$(command -v ups-orchestrator 2>/dev/null || true)"
  [ -n "$ORCH" ] && PYBIN="$(dirname "$(readlink -f "$ORCH")")/python3"
fi
if [ -z "$PYBIN" ] || [ ! -x "$PYBIN" ]; then
  echo "cannot locate the ups-orchestrator venv interpreter (looked for" >&2
  echo "'ups-orchestrator' on PATH; override with UPS_ORCH_PYTHON=/path/to/python3)." >&2
  echo "Refusing to rewrite $CONFIG rather than write it without the shared," >&2
  echo "metadata-preserving writer — a bare rename strips its mode, owner and ACL." >&2
  exit 1
fi

"$PYBIN" "$HERE/disable-shutdown-targets.py" "$CONFIG"

systemctl --user -M "$RUN_USER@" restart ups-orchestrator-watch.service 2>/dev/null || true
echo "Orchestrator shutdown targets disabled in $CONFIG"
