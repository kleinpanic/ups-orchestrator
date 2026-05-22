#!/bin/sh
# NUT upssched CMDSCRIPT dispatcher (installed to /usr/local/bin so the `nut`
# user can reach it — it must NOT live under a 0700 home directory).
#
# upssched calls this with the timer/EXECUTE name as $1; NUT exposes the active
# UPS as $UPSNAME, which we forward so notifications are labelled per-UPS.
set -eu

# `set -a` so sourced KEY=VALUE assignments are exported into the orchestrator's
# environment (a plain `. file` only sets shell vars, not the exec'd child's env).
if [ -r /etc/ups-orchestrator.env ]; then
  set -a
  . /etc/ups-orchestrator.env
  set +a
fi

ORCH="/usr/local/bin/ups-orchestrator"

case "$1" in
  onbatt|online|lowbatt|commbad|commok|tick|shutdown_r630)
    exec "$ORCH" "$1" "${UPSNAME:-}"
    ;;
  *)
    # Unknown action — ignore quietly.
    ;;
esac
