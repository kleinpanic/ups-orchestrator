# Shutdown Targets (LEGACY, back-compat only)

!!! warning "This per-UPS `shutdown_targets[]` array is LEGACY"
    New enrollments use the per-machine `monitored_machines[].shutdown_method`
    model (`none`/`native`/`serial`/`ssh`, via `monitor add --method ...`) —
    see [Configuration](Configuration.md). `shutdown_targets[]` is still parsed
    so pre-existing config files keep loading, but a new entry should not be
    written here. `kind: "serial"` maps onto `shutdown_method: "serial"`;
    `kind: "remote"` maps onto `shutdown_method: "ssh"`; `kind: "local"` has no
    `monitored_machines` equivalent — the watcher host is never enrolled as a
    machine, so a `local` target stays the only way to describe its own
    shutdown.

A shutdown target is a machine the orchestrator knows how to power down. Each
UPS can have any number, and target entries are only transports: they say *how*
to reach a device. The top-level `shutdown` policy decides *whether* any target
command is allowed to run. That policy is disabled by default.

The host running the daemon is protected separately by NUT's own
`upsmon SHUTDOWNCMD`; orchestrator targets are optional extra actions for the
devices a UPS feeds.

!!! note "Which mechanism is right is decided elsewhere"
    Whether a machine should be told to shut down over `native`, `ssh` or
    `serial` — and why the answer here is usually `serial`, because the router
    and every switch sit on one UPS — is covered in
    [Shutdown mechanisms](Shutdown-Mechanisms.md). Read that first; this page is
    only the shape of the legacy array.

**Deferred:** a native-plus-deadman regime, where a legacy target fired only as
a last resort strictly below a native secondary's own LB point, was scoped for
this phase and **dropped** — it may return in a future phase. (It was also
never actually implemented: `battery_below`/`runtime_below` on a target are
parsed but not consulted at runtime — see Triggers, below.)

### Chosen defaults for the open questions

These were live-environment unknowns resolved to defaults, not blockers:

- **DEADTIME 30** on secondaries (the ≥3×POLLFREQ floor for POLLFREQ 5).
- **Per-host nft scope** — each secondary's source IP added to the dedicated
  `table inet ups_orchestrator` set, not a broad allow.
- **Serial-cable presence unknown** → legacy target `kind` defaults to
  `remote`; switch to `serial` where a console cable exists
  (network-independent, survives a dark switch).
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

!!! danger "Match the far end's ACTUAL baud — a wrong value is silent"
    `stty -F <device> <rate>` returns **exit 0** for 9600, 19200, 115200 and 0
    alike, so a wrong-but-valid `baud` here is **undetectable**: it sends
    garbage down the wire, the orchestrator reports success (rc 0), and the
    machine never shuts down. Only a rejected *local* line configuration is
    caught — never a far-end speed mismatch.

    Read the real rate off the machine you are wiring to:

    ```bash
    systemctl show serial-getty@ttyS0 -p ExecStart --value   # the agetty line
    ```

    The rate is the bare number in the `agetty` arguments. **The `115200` in
    the example below is an example, not a value to copy.**

```json
{
  "name": "bigserver",
  "kind": "serial",
  "enabled": true,
  "device": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART-if00-port0",
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
