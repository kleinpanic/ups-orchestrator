# ups-orchestrator

A small daemon that watches your UPSes through [NUT](https://networkupstools.org/)
and turns power events into per-UPS Discord embeds. Each machine you enroll
(`monitor add --method ...`) gets exactly one shutdown authority: **native**
wraps NUT's own primary/secondary model, and **serial**/**ssh** are the
orchestrator's own opt-in push transports, gated by a central policy and
disabled by default — while leaving the host's own protective shutdown to
NUT's `upsmon`.

It has no runtime dependencies (just the standard library and the `upsc` CLI
that ships with NUT) and runs well on a Raspberry Pi.

## Where to go next

- **[Configuration](Configuration.md)**: the `config.json` reference, including
  the per-machine `shutdown_method` model.
- **[Shutdown Mechanisms](Shutdown-Mechanisms.md)**: native NUT vs. serial vs.
  SSH push, ordering, trade-offs.
- **[Shutdown Targets](Shutdown-Targets.md)** *(legacy, back-compat only)*: the
  per-UPS array that predates the per-machine model.
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
