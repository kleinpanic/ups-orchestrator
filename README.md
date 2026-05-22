# ⚡ ups-orchestrator

NUT-driven UPS power-event monitor for a homelab. It turns
[Network UPS Tools](https://networkupstools.org/) power events into **beautiful,
per-UPS Discord embeds** — on-battery alerts, a runtime-remaining countdown,
power-restored summaries, and low-battery warnings — while leaving the *actual*
protective shutdown to NUT's own `upsmon`.

Works with any NUT-supported UPS, and monitors **any number** of them (built and
run against two USB-attached units, but nothing is hard-coded to a model).

## Design

**Hybrid, primarily NUT event-driven:**

- **NUT `upssched` → orchestrator → Discord.** `upsmon` fires
  `ONBATT`/`ONLINE`/`LOWBATT`/`COMMBAD`/`COMMOK`, `upssched` passes them through
  `deploy/upssched-cmd.sh` to `ups-orchestrator <event> $UPSNAME`, which posts a
  labelled embed per UPS.
- **Unified shutdown targets, local last.** Each UPS lists any number of
  `shutdown_targets` — `remote` machines (shut down over SSH) and/or the `local`
  host — each firing when its battery `battery_below` (%) **or** `runtime_below`
  (sec) threshold is crossed. The daemon always sequences `local` targets *after*
  every enabled remote on that UPS, so the watcher host dies last. NUT's
  `upsmon SHUTDOWNCMD` stays as a low-battery backstop. All targets off by default.
- **Configurable poll loop, decoupled from webhooks.** A `systemd --user` service
  (`ups-orchestrator watch`) polls every `poll_seconds` to evaluate shutdown
  thresholds; the on-battery countdown posts on its own `countdown_every_seconds`
  cadence (0 = off). Discord *alerts* stay NUT-event-driven — polling never gates
  them.

```
utility power ──▶ UPS ──USB──▶ host (NUT server)
                               │
                  upsd ──▶ upsmon ──┬─▶ SHUTDOWNCMD            (NUT protects this host)
                                    └─▶ upssched ─▶ upssched-cmd.sh
                                                     └─▶ ups-orchestrator ─▶ 🟦 Discord
                                                                          └─▶ SSH shutdown_targets
```

## Notifications

Embeds are rendered to the Discord spec with zero third-party dependencies
(stdlib `urllib`): a branded author line, severity colour, a unicode battery
gauge (`▰▰▰▰▰▰▰▱▱▱ 72%`), inline status fields, a host/UPS footer, and a native
timestamp. Delivery is non-fatal (a down webhook never wedges NUT) and honours
HTTP 429 `retry_after`.

| Event | Embed |
|-------|-------|
| `onbatt` | 🔋 **ON BATTERY** — status, battery gauge, runtime, load, input V |
| `tick` (on battery) | ⏳ **still on battery** — runtime countdown |
| `online` | ✅ **POWER RESTORED** — outage duration + state |
| `lowbatt` | ⚠️ **LOW BATTERY** — critical, shutdown announced |
| `commbad` / `commok` | 🔌 comms lost / restored |
| (target due) | 🛑 **shutdown sent to `<target>`** |

The notifier sits behind a `Notifier` protocol, so a future **Discord bot** is a
drop-in replacement — implement `Notifier.send`, no event-logic changes.

## Configuration

Copy the template and fill it in (the webhook is **never** committed — it comes
from an environment variable):

```bash
cp config.example.json config.json
export UPS_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
```

`config.json` (no secrets) — keys under `upses` are your NUT device names:

```jsonc
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",  // env var holding the URL
  "discord_username": "UPS Orchestrator",
  "poll_seconds": 30,            // how often the watch loop checks battery thresholds
  "countdown_every_seconds": 60, // on-battery countdown post cadence (0 = off)
  "upses": {
    "ups1": {
      "label": "Rack UPS",
      "shutdown_targets": [
        { "name": "fileserver", "kind": "remote", "enabled": false,
          "host": "fileserver.lan", "user": "youruser",
          "cmd": "sudo /sbin/shutdown -h now",
          "battery_below": 50, "runtime_below": 600 },
        { "name": "this-host", "kind": "local", "enabled": false,
          "cmd": "sudo /sbin/shutdown -h now",
          "battery_below": 15, "runtime_below": 120 }
      ]
    },
    "ups2": { "label": "Desk UPS", "shutdown_targets": [] }
  }
}
```

A target fires when **either** `battery_below` (charge %) or `runtime_below`
(seconds) is crossed; omit both and it only fires on an explicit `remote_shutdown`.
`local` targets need passwordless shutdown (set up by `deploy/install.sh`).

Config path resolves to `$UPS_ORCH_CONFIG`, else `/etc/ups-orchestrator/config.json`,
else `<repo>/config.json`. State resolves similarly via `$UPS_ORCH_STATE` /
`/var/lib/ups-orchestrator/state.json`.

## Status

```bash
ups-orchestrator status            # one-shot per-UPS table (alive/dead, gauge, runtime, load)
ups-orchestrator status --watch    # live-refreshing dashboard (Ctrl-C to exit)
```

## Install / Deploy

NUT's `nut` user can't read a `0700` home, so the orchestrator installs to a
**system venv** (`/opt/ups-orchestrator/venv` → `/usr/local/bin/ups-orchestrator`);
config/secret/state live under `/etc` + `/var/lib`; the `--user` watch service
reaches them via ACLs.

```bash
# 1) system install (root): venv, /etc config+env, /var/lib state, dispatcher, ACLs
sudo deploy/install.sh
sudo "$EDITOR" /etc/ups-orchestrator.env          # put your real webhook here

# 2) apply the NUT snippets (review first — set your UPS names + USB ids), then:
sudo systemctl restart nut-driver-enumerator nut-server nut-monitor
upsc -l                                            # expect your UPSes listed

# 3) poll-loop service (NO sudo)
loginctl enable-linger "$USER"
deploy/install-user-service.sh
```

| Path | What |
|------|------|
| `/usr/local/bin/ups-orchestrator` | the orchestrator (→ `/opt/ups-orchestrator/venv`) |
| `/usr/local/bin/upssched-cmd.sh` | NUT dispatcher (sources env, calls the orchestrator) |
| `/etc/ups-orchestrator/config.json` | per-UPS config (no secret) |
| `/etc/ups-orchestrator.env` | webhook + paths (`root:nut 0640` + user ACL) |
| `/var/lib/ups-orchestrator/state.json` | per-UPS state |
| `~/.config/systemd/user/ups-orchestrator-watch.service` | the `--user` poll loop |
| `/etc/sudoers.d/ups-orchestrator` | passwordless shutdown for `local` targets |

> Live config under `/etc` and `/etc/nut` holds your real device ids / IPs and
> stays on the machine — it is not part of this repo (and `config.json` is
> gitignored).

## Develop

```bash
python3 -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/ruff check . && .venv/bin/mypy && .venv/bin/pytest
```

CI runs ruff + mypy(strict) + pytest on every push/PR across Python 3.11–3.13.

## License

MIT — see [LICENSE](LICENSE).
