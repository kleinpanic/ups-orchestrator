# ups-orchestrator

A small, dependency-free daemon that watches your UPSes through
[NUT](https://networkupstools.org/) and turns power events into per-UPS Discord
embeds. Each machine you enroll (`monitor add --method ...`) gets exactly one
shutdown authority: **`native`** wraps NUT's own primary/secondary model (the
secondary's `upsmon` shuts itself down when the primary declares low battery),
and **`serial`**/**`ssh`** are the orchestrator's own opt-in push transports —
gated by a central policy that requires the UPS to be both on battery and
close to empty, and disabled by default. It runs well on a Raspberry Pi and has
no runtime dependencies beyond the standard library and NUT's
`upsc`/`upscmd` CLIs.

## Architecture at a glance

```mermaid
flowchart LR
  UPS[("UPS × N")] --> DRV[NUT driver] --> UPSD[upsd]
  UPSD -->|upsc reads| WATCH[[watch loop]]
  UPSD -->|upsc reads| REC[[recorder]]
  UPSD -->|upssched events| EVENTS[[event handlers]]
  CTL[[control / selftest]] -->|upscmd| UPSD
  WATCH --> EVENTS
  EVENTS -->|embeds| DISCORD["Discord"]
  CTL -->|embeds| DISCORD
  EVENTS -->|gated policy| SHUT["shutdown: native · serial · SSH · local"]
  REC --> STORE[("samples · state · logs")]
```

## Where to go next

- **[Configuration](Configuration.md)** — the `config.json` reference: per-machine
  `shutdown_method` (none/native/serial/ssh), poll cadence, on-battery notify
  grace, per-UPS load-step overrides, the central shutdown policy.
- **[Shutdown mechanisms](Shutdown-Mechanisms.md)** — native NUT vs. serial vs.
  SSH push, ordering, and the trade-offs between them.
- **[Shutdown targets](Shutdown-Targets.md)** *(legacy, back-compat only)* — the
  per-UPS `shutdown_targets[]` array that predates the per-machine model.
- **[Deployment](Deployment.md)** — system install, NUT wiring, the watch service, the
  least-privilege control user.
- **[Architecture](Architecture.md)** — the event path and the poll path in detail.

## Commands

| Command | What it does |
|---|---|
| `status` / `status --watch` | bordered per-UPS TUI panels (battery/load gauges, draw sparkline) |
| `report` | daily Discord load report (weekly power dashboard image) |
| `baseline` | per-UPS draw stats (median/p95/mean) from recorder history |
| `selftest` | run a NUT battery self-test per UPS, alert on failure |
| `control <action>` | safe instant commands (beeper, battery test) across UPSes, with a Discord counterpart |
| `webui` | local stdlib web dashboard (localhost only) |
| `audit` | boot / UPS / log / shutdown evidence summary |

`control` and `selftest` need admin creds in the environment
(`UPS_NUT_ADMIN_USER` / `UPS_NUT_ADMIN_PASSWORD`), never the config. These consumer
CyberPower units expose **no** software display/LCD control.
