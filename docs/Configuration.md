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
| `shutdown` | disabled | Central opt-in policy for orchestrator-managed shutdowns |
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

## Per-UPS

Each key under `upses` is a NUT device name (whatever you called it in
`ups.conf`). A UPS has a `label` (used in the embeds) and a list of
`shutdown_targets`; see [Shutdown Targets](Shutdown-Targets).

```json
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",
  "poll_seconds": 30,
  "countdown_every_seconds": 60,
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
