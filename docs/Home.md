# ups-orchestrator

A small daemon that watches your UPSes through [NUT](https://networkupstools.org/),
turns power events into per-UPS Discord embeds, and — if you turn it on — makes
sure every machine on a dying UPS gets shut down cleanly. No runtime
dependencies beyond the standard library and NUT's `upsc` CLI; runs well on a
Raspberry Pi.

## The one thing to understand first

**Every UPS data cable plugs into one machine.** All three UPS USB cables land
on a single host — the **NUT primary** (a Raspberry Pi 5 here). That machine is
the only one on the network that can read a battery percentage or a runtime
estimate, because it is the only one physically wired to a UPS.

**So every other machine has to be *told* to shut down.** `shutdown_method` is
nothing more than the choice of **how it is told**:

| `shutdown_method` | Who issues the poweroff | What the far end needs | Survives a dead LAN |
|---|---|---|---|
| **`native`** | the machine itself, via its own `upsmon` reading the primary's `upsd` over TCP 3493 | `nut-client` and a working network | no |
| **`ssh`** | the primary, running `ssh <alias> '<cmd>'` | `sshd`, a key, passwordless `sudo shutdown` | no |
| **`serial`** | the primary, writing down a console cable | a console cable and an auto-login getty | **yes** |
| **`none`** | nobody | — | — |

A machine holds exactly one of these. They are alternatives, not layers.

**`native` does not need a USB cable on the remote machine** — the most
commonly misunderstood point here. A native machine *subscribes over the LAN* to
the primary's view of a UPS and powers itself off; the primary pushes nothing at
it. All the `LISTEN` / `upsd.users` / nftables setup that `monitor add --method
native` performs exists for one purpose: opening TCP 3493 to that one host
safely.

**Why the choice matters:** the router, the modem and all three network switches
here are on **one UPS**. If it drops, the LAN drops — and `native` and `ssh` die
with it, because both of them *are* the network. Serial is point-to-point
copper and keeps working. Serial is the robust choice; ssh is the convenient
one.

**The serial baud trap:** `stty -F <dev> <rate>` exits 0 for 9600, 19200,
115200 and 0 alike, so a wrong-but-valid baud is undetectable — it writes
garbage down the line, reports success, and the machine never shuts down. Read
the real rate off the far end (`systemctl show serial-getty@<tty> -p ExecStart
--value`) and declare exactly that. Every baud in this documentation is an
example, never a value to copy.

## Where to go next

- **[Configuration](Configuration.md)**: the `config.json` reference, the
  per-machine `shutdown_method` model, the `monitor` CLI, the per-UPS `devices`
  inventory.
- **[Shutdown Mechanisms](Shutdown-Mechanisms.md)**: native NUT vs. serial vs.
  SSH push in depth — ordering, what fires each, trade-offs.
- **[Shutdown Targets](Shutdown-Targets.md)** *(legacy, back-compat only)*: the
  per-UPS array that predates the per-machine model.
- **[Deployment](Deployment.md)**: system install, NUT wiring, enrolling a
  machine, serial far-end setup, the watch service, health checks and repair.
- **[Architecture](Architecture.md)**: how the event path and poll loop fit together.

## Commands

```
ups-orchestrator status [--watch]        # per-UPS table; --watch live-refreshes it
ups-orchestrator monitor list            # enrolled machines and their shutdown authority
ups-orchestrator monitor verify <name>   # "will this machine actually shut down?"
ups-orchestrator remote-shutdown --dry-run   # every target + gate verdict; touches nothing
ups-orchestrator shutdown rehearse <name>    # harmless probe over the real transport
ups-orchestrator maintenance begin|status|end  # declare a planned power cut
ups-orchestrator report [--print]        # daily/load report, print or send to Discord
ups-orchestrator audit                   # boot/UPS/state/shutdown evidence report
ups-orchestrator watch                   # the poll loop (run as a systemd --user service)
ups-orchestrator <event> [ups]           # one NUT event (called by upssched)
```

Operational entry points: `make nut-status` (read-only),
`sudo make nut-repair-listen`, `sudo make install-config CONFIG=...` — see
[Deployment](Deployment.md).
