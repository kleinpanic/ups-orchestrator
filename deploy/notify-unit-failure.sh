#!/usr/bin/env bash
# Page Discord when a ups-orchestrator unit has failed.
#
# Invoked by `OnFailure=` — that is, at the one moment the orchestrator itself cannot
# report anything, because the thing that does the reporting is the thing that died.
#
# The failure this exists for: an unloadable /etc/ups-orchestrator/config.json makes
# `_cmd_watch` return 1 before a notifier is ever constructed, AND makes every NUT event
# handler return 1 the same way. With `Restart=always` the unit simply loops. The next
# real outage then produces no Discord traffic and no shutdown of mt or spark, and
# nothing anywhere says the alerting system is down. Silence is indistinguishable from
# "no outage yet", which is the worst property a monitoring system can have.
#
# DELIBERATELY minimal and dependency-free:
#   * reads the webhook straight from the env file, never through Config — the config is
#     the most likely thing to be broken when this runs;
#   * plain curl, no venv, no package import, so a half-installed or mid-upgrade /opt
#     cannot take the alerter down with it;
#   * exits 0 on every path, because a failing OnFailure handler is just another silent
#     failure and systemd will not report it either.
set -uo pipefail

UNIT="${1:-a ups-orchestrator unit}"
ENV_FILE="${UPS_ORCH_ENV:-/etc/ups-orchestrator.env}"

# shellcheck disable=SC1090
[ -r "$ENV_FILE" ] && . "$ENV_FILE"
WEBHOOK="${UPS_DISCORD_WEBHOOK:-}"

# Recent journal lines give the operator the actual reason without a second round trip.
WHY="$(journalctl -u "$UNIT" --user-unit "$UNIT" -n 12 --no-pager 2>/dev/null | tail -n 12)"
[ -n "$WHY" ] || WHY="(no journal lines available)"

# One page per unit per cooldown. `OnFailure=` fires on EVERY failed activation, so a
# crash-loop that trips the start limit fires it several times in seconds — measured at
# three pages for a single loop during the end-to-end test of this very script. An
# alerter that floods is an alerter that gets muted, which is the failure it exists to
# prevent. The journal line below is written every time regardless; only the push is
# suppressed, so nothing is hidden from an operator reading logs.
COOLDOWN_SECONDS="${UPS_ORCH_FAILURE_PAGE_COOLDOWN:-3600}"
STAMP_DIR="${XDG_RUNTIME_DIR:-/tmp}/ups-orchestrator-failure-pages"
STAMP="$STAMP_DIR/$(printf '%s' "$UNIT" | tr -c 'A-Za-z0-9._-' '_')"
mkdir -p "$STAMP_DIR" 2>/dev/null

logger -t ups-orchestrator "unit $UNIT entered failed state"

if [ -f "$STAMP" ]; then
  LAST=$(cat "$STAMP" 2>/dev/null || echo 0)
  NOW=$(date +%s)
  case "$LAST" in (*[!0-9]*|"") LAST=0 ;; esac
  if [ $((NOW - LAST)) -lt "$COOLDOWN_SECONDS" ]; then
    logger -t ups-orchestrator "already paged for $UNIT $((NOW - LAST))s ago; not re-paging"
    exit 0
  fi
fi
date +%s > "$STAMP" 2>/dev/null
logger -t ups-orchestrator "paging Discord for $UNIT"

if [ -z "$WEBHOOK" ]; then
  # Nothing more we can do, but say so where an operator might still find it.
  logger -t ups-orchestrator "no UPS_DISCORD_WEBHOOK in $ENV_FILE; cannot page for $UNIT"
  exit 0
fi

# JSON is built by python3 -- stdlib only, no venv, no package import -- because shell
# escaping of arbitrary journal text into JSON does not work. The first version did the
# quote/newline substitution with sed and tr and produced a body Discord REJECTS for an
# invalid control character, which would have made this alert-of-last-resort silently
# undeliverable: the one failure mode it exists to prevent, in the code meant to prevent
# it. Encoding is a solved problem; do not hand-roll it.
BODY="$(WHY="$WHY" UNIT="$UNIT" python3 -c '
import json, os
why = os.environ["WHY"]
unit = os.environ["UNIT"]
desc = (
    "The orchestrator is not running. **No UPS monitoring, no shutdown of mt or spark, "
    "and no further alerts** until this is fixed \u2014 including during a power outage.\n\n"
    "This message comes from systemd\u2019s OnFailure handler, not from the orchestrator, "
    "because the orchestrator is what died.\n\n```\n" + why[-1500:] + "\n```"
)
print(json.dumps({
    "username": "UPS Orchestrator",
    "embeds": [{"title": f"\U0001F6D1 {unit} has FAILED", "description": desc, "color": 15158332}],
}))
')"
[ -n "$BODY" ] || { logger -t ups-orchestrator "could not build the failure payload for $UNIT"; exit 0; }

curl -sS -m 15 -H "Content-Type: application/json" -X POST -d "$BODY" "$WEBHOOK" >/dev/null 2>&1 \
  || logger -t ups-orchestrator "failed to deliver the unit-failure page for $UNIT"
exit 0
