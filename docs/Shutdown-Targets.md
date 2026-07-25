# Shutdown Targets

A shutdown target is a machine the orchestrator knows how to power down. Each
UPS can have any number, and target entries are only transports: they say *how*
to reach a device. The top-level `shutdown` policy decides *whether* any target
command is allowed to run. That policy is disabled by default.

The host running the daemon is protected separately by NUT's own
`upsmon SHUTDOWNCMD`; orchestrator targets are optional extra actions for the
devices a UPS feeds.

!!! note "SSH is the fallback, not the primary path"
    Native NUT (upsmon secondary, enrolled via `monitor add`) is the primary
    graceful-shutdown mechanism; the `remote` (SSH) / `serial` targets here are a
    configurable, **default-off** backup. See
    [SSH vs. native NUT](Shutdown-Mechanisms.md) for the full analysis, the
    failure modes, and when each path applies.

### Where the backup sits relative to native LB

The backup is a **last-resort deadman**, and where you place its threshold
depends on whether the machine already has a native secondary:

- **Machine WITH a native secondary** (e.g. mt/spark, enrolled via `monitor add`):
  the backup must sit **strictly below** that UPS's LB point. It fires only if
  native shutdown *didn't* while the primary was still alive — a deadman for
  "FSD/`OB LB` didn't do its job." Placed above LB it would race the native path
  and double-shut-down. (This is the same dual-regime conflict `monitor add`
  refuses without `--force`.)
- **Machine with NO native secondary** (e.g. an appliance that can't run
  nut-client): keep an early/high threshold — the backup *is* its graceful path,
  not a deadman, so there is nothing below LB to defer to.

The backup does **not** cover the primary-dies-first (b2/c-OL) hole: it runs on
the primary and dies with it. See
[Deployment → Known limitation](Deployment.md).

### Chosen defaults for the open questions

These were live-environment unknowns resolved to defaults, not blockers:

- **DEADTIME 30** on secondaries (the ≥3×POLLFREQ floor for POLLFREQ 5).
- **Per-host nft scope** — each secondary's source IP added to the dedicated
  `table inet ups_orchestrator` set, not a broad allow.
- **Serial-cable presence unknown** → backup `kind` defaults to `remote`; switch
  to `serial` where a console cable exists (network-independent, survives a dark
  switch).
- **`nut` group present** → snippet/secret files at `0640 root:nut`, with a
  `0600 root:root` fallback where the group is unavailable.

## Kinds

- **`serial`**: send the command over a serial console (`device` + `baud`) into
  a passwordless or auto-login getty. This needs no network, so it keeps working
  during an outage when the router/switch is also dark. It's the best primary
  path for a server you can reach over a console cable.
- **`remote`**: run the command over SSH. `host` can be a real hostname or an
  `ssh_config` Host alias; leave `user` empty to connect as just `ssh <alias>`,
  which keeps the real hostname/port/key in `~/.ssh/config`.
- **`local`**: the host the daemon runs on. Local targets run *after* every
  enabled serial/remote target on the same UPS, so the watcher dies last.

## Triggers

Auto-shutdown is controlled from one top-level surface:

- `shutdown.enabled` must be `true`.
- The relevant group must be enabled:
  - `shutdown.external.enabled` for `serial` and `remote`.
  - `shutdown.internal.enabled` for `local`.
- The UPS must be on battery when `require_power_outage` is true.
- The UPS must have been on battery for at least `min_on_battery_seconds`.
- The UPS must be close to empty by the group's central `battery_below` and
  `runtime_below` thresholds.

If battery percent and runtime are both available, both must be at or below the
group threshold. This prevents a shutdown at, for example, 50% battery while the
UPS still reports 30 minutes of runtime. If one reading is unavailable, the
available threshold is used.

An explicit `remote_shutdown` event still respects this policy; it does not
bypass disabled groups or the close-to-empty gate.

## Example

```json
{
  "name": "bigserver",
  "kind": "serial",
  "enabled": true,
  "device": "/dev/ttyUSB0",
  "baud": 115200,
  "cmd": "sudo /sbin/shutdown -h now"
}
```

## What each kind needs on the far end

- **serial**: an auto-login getty on the target's serial port. Scope the
  auto-login to that serial tty only; don't loosen SSH or the physical console.
- **remote**: key-based SSH (BatchMode) and a passwordless `shutdown` for the
  SSH user.
- **local**: a `sudoers.d` rule for passwordless `shutdown`/`poweroff`, which
  `deploy/install.sh` writes for the run user.
