# ups-orchestrator

A small daemon that watches your UPSes through [NUT](https://networkupstools.org/)
and turns power events into per-UPS Discord embeds. With an explicit opt-in
policy it can also shut down the machines a UPS powers over a serial console,
SSH, or locally, while leaving the host's own protective shutdown to NUT's
`upsmon`.

It has no runtime dependencies (just the standard library and the `upsc` CLI
that ships with NUT) and runs well on a Raspberry Pi.

## Where to go next

- **[Configuration](Configuration.md)**: the `config.json` reference.
- **[Shutdown Targets](Shutdown-Targets.md)**: serial vs SSH vs local, policy gates, ordering.
- **[Deployment](Deployment.md)**: system install, the NUT wiring, the watch service.
- **[Architecture](Architecture.md)**: how the event path and poll loop fit together.

## Commands

```
ups-orchestrator status [--watch]   # per-UPS table; --watch live-refreshes it
ups-orchestrator report [--print]   # daily/load report, print or send to Discord
ups-orchestrator audit              # boot/UPS/state/shutdown evidence report
ups-orchestrator watch              # the poll loop (run as a systemd --user service)
ups-orchestrator <event> [ups]      # one NUT event (called by upssched)
```
