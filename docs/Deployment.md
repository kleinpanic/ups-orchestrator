# Deployment

The orchestrator gets installed to a system location because NUT's `nut` user
runs the event path and can't read a `0700` home directory. The repo can live
in your home for development; the deployed copy lives under `/opt`, `/etc`, and
`/var/lib`.

## 1. System install (root)

```bash
sudo deploy/install.sh
sudo "$EDITOR" /etc/ups-orchestrator.env   # set your real webhook
```

This creates a venv at `/opt/ups-orchestrator/venv` (symlinked to
`/usr/local/bin/ups-orchestrator`), installs the config/env/state under `/etc`
and `/var/lib`, drops the `upssched-cmd.sh` dispatcher in `/usr/local/bin`, sets
ACLs so the run user can read the config and write state, and adds a `sudoers.d`
rule for local shutdown targets. Orchestrator-managed shutdowns remain disabled
unless the top-level `shutdown` policy is explicitly enabled.

## 2. Wire up NUT

Apply the snippets in `deploy/nut/` (review them first; set your UPS names and
USB ids):

- `ups.conf`: one section per UPS.
- `upssched.conf`: points `CMDSCRIPT` at `/usr/local/bin/upssched-cmd.sh` and
  maps the events.
- `upsmon.conf`: a `MONITOR` line per UPS plus the `NOTIFYFLAG`/`NOTIFYCMD`
  wiring.

### The MONITOR contract

`MONITOR <ups>@<host> <powervalue> <user> <pass> <primary|secondary>` decides
who shuts down and when, so get these right:

- **`@<host>`** must name the host running `upsd` — the one physically cabled to
  the UPS. Use `@localhost` only there; a host watching over the network names
  the `upsd` host instead. A wrong host means upsmon never reaches the UPS and
  every event silently no-ops.
- **`powervalue`** is `1` for a UPS that actually powers this host (counts toward
  `MINSUPPLIES`) and `0` for a notify-only UPS, so an unrelated UPS's battery
  can't trigger this host's shutdown.
- **`primary`** runs the shutdown sequence (exactly one per UPS); **`secondary`**
  only observes and powers itself off. On eulerpi5 the lines are `primary`
  because it owns the shutdown; a USB-cabled debug/observer host (the rpi4) would
  list the same UPSes as `secondary`.

Every `NOTIFYFLAG … EXEC` must map to an event the orchestrator handles
(`onbatt`, `online`, `lowbatt`, `commbad`, `commok`) and vice versa — a flag with
no handler is dead config; a handler with no flag never fires. The orchestrator
only observes and notifies here; upsmon's `SHUTDOWNCMD` does the real shutdown.

```bash
sudo systemctl restart nut-driver-enumerator nut-server nut-monitor
upsc -l   # your UPSes should be listed
```

For multiple USB UPSes with the same vendor/product id, pin each section with a
`serial = ...` line. `nut-scanner -U` is the quickest way to list those serials.

## 3. User services (no sudo)

```bash
loginctl enable-linger "$USER"
deploy/install-user-service.sh
```

This installs:

- `ups-orchestrator-watch.service`: continuous poll loop for the opt-in shutdown
  policy and on-battery countdowns.
- `ups-orchestrator-recorder.service`: one-second UPS telemetry samples for
  power-loss forensics. It retains twenty 50 MB historical segments plus the
  active file (roughly two weeks at the live three-UPS record size) and records
  self-test, output-shutdown timer, and alarm fields alongside load/voltage.
- `ups-orchestrator-boot-audit.service`: one-shot post-boot alert when the host
  recovered from abrupt power loss.
- `ups-orchestrator-report.timer`: daily Discord report of battery, estimated
  time to 0%, load, and voltage for every configured UPS. On Mondays it also
  posts the **power dashboard** image (per-UPS cards, a draw-over-time chart, and
  daily-kWh usage bars for the week) — no separate timer. This needs `matplotlib`
  in the install venv; add it once with
  `sudo /opt/ups-orchestrator/venv/bin/pip install matplotlib` (or
  `pip install ups-orchestrator[dashboard]`). Without it the report still sends;
  the image is skipped with a logged warning.

You can test the report path immediately:

```bash
ups-orchestrator report --print   # terminal preview
ups-orchestrator report           # send Discord webhook
ups-orchestrator notify-test      # send test embed and print delivery result
ups-orchestrator audit            # boot/UPS/state/shutdown evidence report
ups-orchestrator logs events      # tail durable local UPS event JSONL
ups-orchestrator logs notifications
```

## Where things land

| Path | What |
|------|------|
| `/usr/local/bin/ups-orchestrator` | the orchestrator |
| `/usr/local/bin/upssched-cmd.sh` | NUT dispatcher |
| `/etc/ups-orchestrator/config.json` | per-UPS config (no secret) |
| `/etc/ups-orchestrator.env` | webhook + paths |
| `/var/lib/ups-orchestrator/state.json` | per-UPS state |
| `/var/lib/ups-orchestrator/samples.jsonl` | high-frequency UPS samples |
| `/var/lib/ups-orchestrator/events.jsonl` | UPS event/decision log |
| `/var/lib/ups-orchestrator/notifications.jsonl` | Discord delivery outcomes |
| `~/.config/systemd/user/ups-orchestrator-watch.service` | the poll loop |
| `~/.config/systemd/user/ups-orchestrator-recorder.service` | telemetry recorder |
| `~/.config/systemd/user/ups-orchestrator-report.timer` | daily UPS load report |

Your real device ids, IPs, and the webhook stay on the machine under `/etc`;
none of that is in the repo.
