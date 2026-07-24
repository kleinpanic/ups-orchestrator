# Shutdown Targets

A shutdown target is a machine the orchestrator knows how to power down. Each
UPS can have any number, and target entries are only transports: they say *how*
to reach a device. The top-level `shutdown` policy decides *whether* any target
command is allowed to run. That policy is disabled by default.

The host running the daemon is protected separately by NUT's own
`upsmon SHUTDOWNCMD`; orchestrator targets are optional extra actions for the
devices a UPS feeds.

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
