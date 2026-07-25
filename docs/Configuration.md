# Configuration

Config is a single JSON file. It holds no secrets; the Discord webhook comes
from an environment variable, so the file itself is safe to keep around.

The orchestrator looks for the config in this order: `$UPS_ORCH_CONFIG`, then
`/etc/ups-orchestrator/config.json`, then `config.json` next to the repo. State
(per-UPS bookkeeping) resolves the same way via `$UPS_ORCH_STATE`, then
`/var/lib/ups-orchestrator/state.json`.

Local forensic logs are controlled by:

- `$UPS_ORCH_SAMPLES`: high-frequency UPS samples.
- `$UPS_ORCH_EVENT_LOG`: UPS event/transition/shutdown-gate records.
- `$UPS_ORCH_NOTIFICATION_LOG`: Discord delivery attempts and results.

## Top-level keys

| Key | Default | Meaning |
|-----|---------|---------|
| `discord_webhook_env` | `UPS_DISCORD_WEBHOOK` | Env var the webhook URL is read from |
| `discord_webhook_url` | `""` | Fallback URL if the env var is unset (leave empty) |
| `discord_username` | `UPS Orchestrator` | Name shown on the embeds |
| `discord_avatar_url` | `""` | Optional avatar for the webhook |
| `poll_seconds` | `30` | How often `watch` checks UPS state and the shutdown policy |
| `countdown_every_seconds` | `60` | On-battery countdown post cadence (`0` turns it off) |
| `load_step` | on | Output-load collapse detection (a downstream device dying) |
| `shutdown` | disabled | Central opt-in policy for orchestrator-managed shutdowns |
| `nut_server` | localhost-only | Primary-side `upsd` exposure (see below) |
| `monitored_machines` | `[]` | NUT secondaries enrolled via `monitor add` (see below) |
| `upses` | — | Map of NUT device name → per-UPS settings |

## Shutdown policy

`shutdown.enabled` is the master switch. It defaults to `false`, so no target
command runs unless you explicitly enable the policy and the relevant group.

- `require_power_outage`: require the UPS to be on battery before targets run.
- `min_on_battery_seconds`: minimum outage duration before target commands can run.
- `notify`: send Discord attempt/result notifications for every target command.
- `external`: central thresholds for `serial` and `remote` targets.
- `internal`: central thresholds for `local` targets.

When battery and runtime readings are both available, both must be at or below
their group thresholds. That prevents a high battery-percent threshold from
shutting machines down while the UPS still reports healthy runtime.

## NUT server (`nut_server`)

Primary-side `upsd` exposure. It holds **no secrets** — the secondary NUT
password never lives here.

| Key | Default | Meaning |
|-----|---------|---------|
| `listen` | `["127.0.0.1", "::1"]` | Addresses `upsd` binds. Localhost-only by default |
| `port` | `3493` | `upsd` TCP port |
| `secondary_user` | `upsmon_secondary` | The `upsd.users` account secondaries authenticate as |

`listen` is localhost-only on a fresh config so `upsd` is never silently exposed
to the network. `monitor add` appends the primary's LAN address at enrollment
time (and applies the matching `upsd.conf` LISTEN + a full `nut-server`
restart — see [Deployment](Deployment.md)). A LISTEN change needs a **full
restart**, not a reload.

## Monitored machines (`monitored_machines`)

A NUT secondary — a UPS-fed box that runs `upsmon` as `secondary`, monitors the
primary's `upsd` over TCP, and shuts *itself* down. This is the native,
credential-minimal graceful-shutdown path (see
[SSH vs. native NUT](Shutdown-Mechanisms.md)). The array ships **empty**; entries
are added by `monitor add`, not by hand.

| Field | Default | Meaning |
|-------|---------|---------|
| `name` | — | Machine name (also the `ssh` alias if none given) |
| `ssh` | `""` | `ssh_config` Host alias used to bootstrap the secondary |
| `ups` | `""` | NUT UPS name the machine is powered by (e.g. `cyberpower`) |
| `powervalue` | `1` | `1` = powered by this UPS (counts toward `MINSUPPLIES`); never `0` for a real feed |
| `os` | `auto` | `auto` \| `arch` \| `ubuntu` \| `debian` — picks the install/config path |
| `shutdown_cmd` | `/sbin/shutdown -h now` | Command the secondary runs on trigger |
| `ip` | `""` | Resolved source IP added to the firewall `saddr` set |
| `backup` | `{enabled: false, kind: "remote"}` | Optional SSH/serial **backup** below the native path |

Worked example (kept out of `config.example.json` so the shipped file stays
empty — a copied-into-production example would push a half-formed entry into the
firewall set):

```json
"monitored_machines": [
  { "name": "mt",    "ssh": "mt",    "ups": "cyberpower",  "powervalue": 1, "os": "arch",   "ip": "192.168.1.140", "backup": { "enabled": false, "kind": "serial" } },
  { "name": "spark", "ssh": "spark", "ups": "cyberpower3", "powervalue": 1, "os": "ubuntu", "ip": "192.168.1.141", "backup": { "enabled": false, "kind": "remote" } }
]
```

### The secondary NUT password

The secondary's NUT credential lives in `/etc/ups-orchestrator.env` as
`UPS_NUT_SECONDARY_PASSWORD`, **never** in `config.json`. It must match the
`[upsmon_secondary]` entry in `/etc/nut/upsd.users` on the primary (see
`deploy/nut/upsd.users.snippet`). `config.json` carries no secret.

### Dual-regime guard (`monitor add --force`)

A machine can be governed by **two** shutdown regimes at once: a native NUT
secondary (fires below LB) *and* an enabled `shutdown_target` on the same UPS
(fires on the shared external-group thresholds). That is a double-shutdown risk,
so `monitor add` **refuses without `--force`** when it detects a machine that is
both an enrolled secondary and an enabled `shutdown_target` on its UPS.
`Config.load` logs the same conflict as a warning.

!!! note "Per-machine below-LB thresholds are deferred"
    A true per-machine *below-LB threshold* regime (each backup firing at its own
    point beneath its UPS's LB) is a **deferred** follow-up. Per-target
    `battery_below` / `runtime_below` are parsed for compatibility but **ignored
    at runtime** — do not rely on them as an implemented per-machine regime.

## Load-step drop detection

`load_step` flags an abrupt output-load collapse — the only in-band signature
NUT gives for a downstream device losing power while the UPS itself stays `OL`.
On by default; set `enabled` to `false` to silence it.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Master switch for the check |
| `drop_percent` | `15` | Load-point drop below the recent peak that trips the event |
| `window_polls` | `4` | Polls whose peak the current load is compared against |
| `cooldown_seconds` | `600` | Minimum gap between notifications (events are always logged) |

The drop is measured against the highest load in the last `window_polls` polls
rather than just the previous poll, so a collapse that straddles a poll boundary
(the UPS reporting an intermediate value mid-decay) still trips instead of
splitting into two sub-threshold steps. A trip logs a `load_step_drop` event
with the estimated watts delta and sends one notification per `cooldown_seconds`;
the alert embeds a 10-minute draw-history sparkline built from the recorder
samples. It is a hint, not a verdict — a heavy job finishing looks identical —
so pair it with a reachability check on the device.

## Per-UPS

Each key under `upses` is a NUT device name (whatever you called it in
`ups.conf`). A UPS has a `label` (used in the embeds) and a list of
`shutdown_targets`; see [Shutdown Targets](Shutdown-Targets.md).

```json
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",
  "poll_seconds": 30,
  "countdown_every_seconds": 60,
  "load_step": { "enabled": true, "drop_percent": 15, "cooldown_seconds": 600, "window_polls": 4 },
  "shutdown": {
    "enabled": false,
    "require_power_outage": true,
    "min_on_battery_seconds": 120,
    "notify": true,
    "external": { "enabled": false, "battery_below": 15, "runtime_below": 300 },
    "internal": { "enabled": false, "battery_below": 10, "runtime_below": 120 }
  },
  "upses": {
    "rack": { "label": "Rack UPS", "shutdown_targets": [] },
    "desk": { "label": "Desk UPS", "shutdown_targets": [] }
  }
}
```

## The webhook

Set it in the environment, never in the file:

```bash
export UPS_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
```

On a deployed box this lives in `/etc/ups-orchestrator.env`, which both the NUT
event path and the watch service read.

Use `ups-orchestrator notify-test` after editing the env file. It sends a test
embed and prints `ok`, `configured`, attempt count, and HTTP status without
printing the webhook URL.
