# Configuration

Config is a single JSON file. It holds no secrets; the Discord webhook comes
from an environment variable, so the file itself is safe to keep around.

The orchestrator looks for the config in this order: `$UPS_ORCH_CONFIG`, then
`/etc/ups-orchestrator/config.json`, then `config.json` next to the repo. State
(per-UPS bookkeeping) resolves the same way via `$UPS_ORCH_STATE`, then
`/var/lib/ups-orchestrator/state.json`.

## Top-level keys

| Key | Default | Meaning |
|-----|---------|---------|
| `discord_webhook_env` | `UPS_DISCORD_WEBHOOK` | Env var the webhook URL is read from |
| `discord_webhook_url` | `""` | Fallback URL if the env var is unset (leave empty) |
| `discord_username` | `UPS Orchestrator` | Name shown on the embeds |
| `discord_avatar_url` | `""` | Optional avatar for the webhook |
| `poll_seconds` | `30` | How often `watch` checks battery thresholds |
| `countdown_every_seconds` | `60` | On-battery countdown post cadence (`0` turns it off) |
| `shutdown_scope` | `remote` | Global default: `remote` (only remote/serial targets) or `all` (also the local host, last). Each UPS can override it. |
| `upses` | — | Map of NUT device name → per-UPS settings |

## Per-UPS

Each key under `upses` is a NUT device name (whatever you called it in
`ups.conf`). A UPS has a `label` (used in the embeds) and a list of
`shutdown_targets`; see [Shutdown Targets](Shutdown-Targets).

```json
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",
  "poll_seconds": 30,
  "countdown_every_seconds": 60,
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
