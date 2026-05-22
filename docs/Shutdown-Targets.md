# Shutdown Targets

A shutdown target is a machine the orchestrator powers down when its UPS runs
low. Each UPS can have any number, and they're all disabled until you say
otherwise. The host running the daemon is protected separately by NUT's own
`upsmon SHUTDOWNCMD`; targets are for the *other* boxes a UPS feeds.

## Kinds

- **`serial`**: send the command over a serial console (`device` + `baud`) into
  a passwordless or auto-login getty. This needs no network, so it keeps working
  during an outage when the router/switch is also dark. It's the best primary
  path for a server you can reach over a console cable.
- **`remote`**: run the command over SSH. `host` can be a real hostname or an
  `ssh_config` Host alias; leave `user` empty to connect as just `ssh <alias>`,
  which keeps the real hostname/port/key in `~/.ssh/config`.
- **`local`**: the host the daemon runs on. These always fire *after* every
  enabled serial/remote target on the same UPS, so the watcher dies last.

## Triggers

A target fires when **either** threshold is met:

- `battery_below`: charge percentage at or below this value, or
- `runtime_below`: estimated runtime (seconds) at or below this value.

Leave both out and the target only fires on an explicit `remote_shutdown` event.

## Example

```json
{
  "name": "bigserver",
  "kind": "serial",
  "enabled": true,
  "device": "/dev/ttyUSB0",
  "baud": 115200,
  "cmd": "sudo /sbin/shutdown -h now",
  "battery_below": 50
}
```

## What each kind needs on the far end

- **serial**: an auto-login getty on the target's serial port. Scope the
  auto-login to that serial tty only; don't loosen SSH or the physical console.
- **remote**: key-based SSH (BatchMode) and a passwordless `shutdown` for the
  SSH user.
- **local**: a `sudoers.d` rule for passwordless `shutdown`/`poweroff`, which
  `deploy/install.sh` writes for the run user.
