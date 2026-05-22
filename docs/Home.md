# ups-orchestrator

A small daemon that watches your UPSes through [NUT](https://networkupstools.org/)
and turns power events into per-UPS Discord embeds. When a UPS gets low it can
also shut down the machines it powers over a serial console, SSH, or locally,
and it leaves the host's own protective shutdown to NUT's `upsmon`.

It has no runtime dependencies (just the standard library and the `upsc` CLI
that ships with NUT) and runs well on a Raspberry Pi.

## Where to go next

- **[Configuration](Configuration)**: the `config.json` reference.
- **[Shutdown Targets](Shutdown-Targets)**: serial vs SSH vs local, thresholds, ordering.
- **[Deployment](Deployment)**: system install, the NUT wiring, the watch service.
- **[Architecture](Architecture)**: how the event path and poll loop fit together.

## Commands

```
ups-orchestrator status [--watch]   # per-UPS table; --watch live-refreshes it
ups-orchestrator watch              # the poll loop (run as a systemd --user service)
ups-orchestrator <event> [ups]      # one NUT event (called by upssched)
```
