# ⚡ ups-orchestrator

NUT-driven UPS power-event monitor for a homelab. It turns [Network UPS Tools](https://networkupstools.org/)
power events into **beautiful, per-UPS Discord embeds** — on-battery alerts, a
runtime-remaining countdown, power-restored summaries, and low-battery warnings —
while leaving the *actual* protective shutdown to NUT's own `upsmon`.

Built for a Raspberry Pi 5 (`eulerpi5`) monitoring **two** CyberPower UPSes over USB:

| NUT name | Model | USB ID |
|----------|-------|--------|
| `cyberpower` | CyberPower PR1500LCDRT2U | `0764:0601` |
| `cyberpower2` | CyberPower CP1500 AVR | `0764:0501` |

## Design

**Hybrid, primarily NUT event-driven:**

- **NUT `upssched` → orchestrator → Discord.** `upsmon` fires `ONBATT`/`ONLINE`/`LOWBATT`/`COMMBAD`/`COMMOK`,
  `upssched` passes them through `deploy/upssched-cmd.sh` to `ups-orchestrator <event> $UPSNAME`,
  which posts a labelled embed.
- **NUT protects, the orchestrator announces.** The genuine low-battery shutdown is
  done by `upsmon`'s `SHUTDOWNCMD` (already privileged and battle-tested). The
  orchestrator only *reports* it. (You can opt into orchestrator-initiated
  shutdown per UPS with `shutdown_pi_on_lowbatt`, off by default.)
- **A `systemd` timer** runs `ups-orchestrator tick` every minute for the
  on-battery runtime countdown — the lightweight "polling" half of the hybrid.
- **Deferred R630 SSH shutdown** is kept in code behind `r630_shutdown.enabled`
  (off by default) — the UPSes only hold the PowerEdge ~5 min, so it's not the
  primary strategy.

```
utility power ──▶ UPS ──USB──▶ Pi5 (NUT host)
                               │
                  upsd ──▶ upsmon ──┬─▶ SHUTDOWNCMD        (NUT protects)
                                    └─▶ upssched ─▶ upssched-cmd.sh
                                                     └─▶ ups-orchestrator ─▶ 🟦 Discord
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

The notifier is behind a `Notifier` protocol, so a future **Discord bot** is a
drop-in replacement — implement `Notifier.send`, no event-logic changes.

## Configuration

Copy the template and fill it in (the webhook is **never** committed — it comes
from an environment variable):

```bash
cp config.example.json config.json
export UPS_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"   # systemd: set in the unit / EnvironmentFile
```

`config.json` (no secrets):

```jsonc
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",  // env var to read the URL from
  "discord_username": "UPS Orchestrator",
  "discord_avatar_url": "",
  "poll_on_battery_seconds": 60,
  "upses": {
    "cyberpower":  { "label": "Rack UPS — PR1500LCDRT2U", "shutdown_pi_on_lowbatt": false },
    "cyberpower2": { "label": "Desk UPS — CP1500 AVR",     "shutdown_pi_on_lowbatt": false }
  }
}
```

Config path resolves to `$UPS_ORCH_CONFIG`, else `<repo>/config.json`.
State (per-UPS bookkeeping) lives at `$UPS_ORCH_STATE`, else `<repo>/state.json`.

## Install / Deploy

The repo lives under a `0700` home that NUT's `nut` user can't enter, so the
orchestrator is installed to a **system venv** (`/opt/ups-orchestrator/venv`,
symlinked to `/usr/local/bin/ups-orchestrator`). Config/secret/state live under
`/etc` + `/var/lib`; the `--user` tick timer reaches them via ACLs.

```bash
# 1) system install (root): venv, /etc config+env, /var/lib state, dispatcher, ACLs
sudo deploy/install.sh
sudo "$EDITOR" /etc/ups-orchestrator.env        # put your real webhook here

# 2) apply the NUT snippets (review first), then restart NUT
#    /etc/nut/ups.conf      <- deploy/nut/ups.conf.snippet      (adds cyberpower2)
#    /etc/nut/upssched.conf <- deploy/nut/upssched.conf.snippet (fixes CMDSCRIPT)
#    /etc/nut/upsmon.conf   <- deploy/nut/upsmon.conf.snippet   (2nd MONITOR)
sudo systemctl restart nut-driver-enumerator nut-server nut-monitor
upsc -l                                          # expect: cyberpower AND cyberpower2

# 3) on-battery countdown timer (NO sudo)
loginctl enable-linger "$USER"                   # run while logged out
deploy/install-user-timer.sh
```

Layout after install:

| Path | What |
|------|------|
| `/usr/local/bin/ups-orchestrator` | the orchestrator (→ `/opt/ups-orchestrator/venv`) |
| `/usr/local/bin/upssched-cmd.sh` | NUT dispatcher (sources the env, calls the orchestrator) |
| `/etc/ups-orchestrator/config.json` | per-UPS config (no secret), `root:nut 0640` + user ACL |
| `/etc/ups-orchestrator.env` | webhook + paths, `root:nut 0640` + user ACL |
| `/var/lib/ups-orchestrator/state.json` | per-UPS state, `nut:nut` + user ACL |
| `~/.config/systemd/user/ups-orchestrator-tick.*` | the `--user` countdown timer |

## Develop

```bash
.venv/bin/ruff check .
.venv/bin/mypy
.venv/bin/pytest
```

CI runs all three on every push/PR (`.github/workflows/ci.yml`).

## License

MIT — see [LICENSE](LICENSE).
