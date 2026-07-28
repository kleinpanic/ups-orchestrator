# Deployment

The orchestrator gets installed to a system location because NUT's `nut` user
runs the event path and can't read a `0700` home directory. The repo can live
in your home for development; the deployed copy lives under `/opt`, `/etc`, and
`/var/lib`.

## 1. System install (root)

```bash
sudo deploy/install.sh
sudo "$EDITOR" /etc/ups-orchestrator.env   # set your real webhook
```

This creates a venv at `/opt/ups-orchestrator/venv` (symlinked to
`/usr/local/bin/ups-orchestrator`), installs the config/env/state under `/etc`
and `/var/lib`, drops the `upssched-cmd.sh` dispatcher in `/usr/local/bin`, sets
ACLs so the run user can read the config and write state, and adds a `sudoers.d`
rule for local shutdown targets. Orchestrator-managed shutdowns remain disabled
unless the top-level `shutdown` policy is explicitly enabled.

## 2. Wire up NUT

Apply the snippets in `deploy/nut/` (review them first; set your UPS names and
USB ids):

- `ups.conf`: one section per UPS.
- `upssched.conf`: points `CMDSCRIPT` at `/usr/local/bin/upssched-cmd.sh` and
  maps the events. `ONBATT` is **debounced** via a 15 s `START-TIMER` so brief
  utility dips don't page — only a sustained outage forwards `onbatt`, and the
  restore alert only follows a real (non-debounced) outage. `LOWBATT` is never
  debounced.
- `upsmon.conf`: a `MONITOR` line per UPS plus the `NOTIFYFLAG`/`NOTIFYCMD`
  wiring.

### The MONITOR contract

`MONITOR <ups>@<host> <powervalue> <user> <pass> <primary|secondary>` decides
who shuts down and when, so get these right:

- **`@<host>`** must name the host running `upsd` — the one physically cabled to
  the UPS. Use `@localhost` only there; a host watching over the network names
  the `upsd` host instead. A wrong host means upsmon never reaches the UPS and
  every event silently no-ops.
- **`powervalue`** is `1` for a UPS that actually powers this host (counts toward
  `MINSUPPLIES`) and `0` for a notify-only UPS, so an unrelated UPS's battery
  can't trigger this host's shutdown.
- **`primary`** runs the shutdown sequence (exactly one per UPS); **`secondary`**
  only observes and powers itself off. On eulerpi5 the lines are `primary`
  because it owns the shutdown; a USB-cabled debug/observer host (the rpi4) would
  list the same UPSes as `secondary`.

Every `NOTIFYFLAG … EXEC` must map to an event the orchestrator handles
(`onbatt`, `online`, `lowbatt`, `commbad`, `commok`) and vice versa — a flag with
no handler is dead config; a handler with no flag never fires. The orchestrator
only observes and notifies here; upsmon's `SHUTDOWNCMD` does the real shutdown.

```bash
sudo systemctl restart nut-driver-enumerator nut-server nut-monitor
upsc -l   # your UPSes should be listed
```

For multiple USB UPSes with the same vendor/product id, pin each section with a
`serial = ...` line. `nut-scanner -U` is the quickest way to list those serials.

## 2b. Enroll a machine (`monitor add --method ...`)

`monitor add --method {none|native|serial|ssh}` (default `native`, matching
pre-Phase-2 invocations that omit `--method`) is the one enrollment command for
every shutdown authority. `serial`/`ssh`/`none` are **record-only**: they
write a `monitored_machines` entry and touch nothing else — no
`upsd.users`/`LISTEN`, no firewall, no remote NUT install. Only `native` runs
the privileged NUT-secondary bootstrap described below, and it runs on the
**primary** (eulerpi5) as root:

```bash
# native — network-reachable, nut-capable box: bootstraps a real NUT secondary
sudo ups-orchestrator monitor add mt --method native --ssh mt --ups cyberpower --os arch --powervalue 1

# serial — network-independent push over a console cable; no --ssh needed
sudo ups-orchestrator monitor add mt --method serial \
     --serial-device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-if00-port0 \
     --serial-baud 9600 --ups cyberpower

# ssh — push transport, no NUT enrollment
sudo ups-orchestrator monitor add somebox --method ssh --ssh somebox --ups cyberpower
```

You cannot switch a `native` record to `serial`/`ssh`/`none` in place —
`monitor add` refuses that transition and tells you to `monitor remove <name>`
first, because that is the only command that runs the real remote NUT
teardown. Skipping it would leave the old secondary's `upsmon` armed while also
activating a push — two live shutdown authorities on one machine.

The rest of this section describes the `native` bootstrap. What it does, in order:

1. **Bootstrap the primary.** Append the LAN `LISTEN` to `/etc/nut/upsd.conf`,
   add the `[upsmon_secondary]` account to `/etc/nut/upsd.users`, then run a
   **full** `systemctl restart nut-server`. A `reload` is a **no-op** for
   `LISTEN` — `upsd` only binds its sockets at start, so a reload leaves it on
   localhost and the secondary never connects. This is why the flow restarts
   rather than reloads.
2. **Firewall.** Splice a marked, reversible
   `tcp dport 3493 ip saddr { … } accept` rule **into the operator's own input
   base chain** (the one carrying `hook input` / `policy drop`, in
   `/etc/nftables.d/main.nft`), reload the top-level `/etc/nftables.conf`, then
   **restart the crowdsec bouncer** (see the warning below). The rule *must* live
   in that base chain: nftables evaluates every base chain on the `input` hook,
   and an `accept` in a self-contained `policy accept` table at negative priority
   is terminal only for its own chain — the packet still reaches the `policy drop`
   base chain and is dropped, so the port never opens (the symptom is a `upsc`
   *timeout*, not a refusal). The marked block makes the edit idempotent and
   `monitor remove` reverses it cleanly. `/etc/nftables.d/main.nft` must already
   exist and contain the input base chain — enrollment errors clearly if it does
   not, rather than writing a rule that lands nowhere.
3. **Remote install/config.** Over the `ssh` alias: install the NUT client for
   the box's distro (`nut` on Arch, `nut-client` on Ubuntu/Debian), write a
   secondary `upsmon.conf` (`powervalue 1`, `MINSUPPLIES 1`, `DEADTIME 30`, and
   **no** `POWERDOWNFLAG`/`killpower` — a secondary has no UPS to power off),
   and enable `nut-monitor`.
4. **Verify.** Confirm the secondary reads its UPS off the primary's `upsd`.

The secondary's NUT password comes from `UPS_NUT_SECONDARY_PASSWORD` in
`/etc/ups-orchestrator.env` and must match the `[upsmon_secondary]` entry on the
primary. It is never written to `config.json`.

**Address resolution.** Two addresses matter and both are auto-detected, but a
WAN/NAT SSH path can fool the defaults — override explicitly when in doubt:

- *The secondary's source IP* (the nftables `saddr`, and what `upsd` sees). It is
  resolved by running `ip -o route get <primary>` **on the secondary** and taking
  the `src` — the real address the box sources packets from toward the primary.
  `$SSH_CONNECTION` is only a fallback, because over a WAN/NAT hop its client
  field is the **gateway**, not the machine's LAN IP. Pass `--ip <addr>` to
  override.
- *The primary's LAN IP* (the `MONITOR` line and the `LISTEN` the secondary dials).
  It is taken from the first non-loopback `nut_server.listen` entry, else
  auto-detected via a local `ip route get` toward the secondary. If neither
  yields a LAN address, enrollment **errors** (rather than silently binding
  `upsd` to localhost and failing at verify) — pass `--primary-ip <addr>` or add
  a LAN address to `nut_server.listen`.

!!! danger "Mandatory: restart crowdsec after every `nft -f`"
    Debian's `/etc/nftables.conf` opens with `flush ruleset`, which wipes
    crowdsec's `crowdsec`/`crowdsec6` tables. The bouncer does **not** recreate
    them on a plain reload — only a **restart** does. So every nftables reload
    must be followed by:

    ```bash
    sudo nft -f /etc/nftables.conf
    sudo systemctl restart crowdsec-firewall-bouncer
    ```

    `monitor add` does this for you. To make the guard permanent across *future*
    reloads, install the shipped `PartOf=nftables.service` drop-in
    (`deploy/nftables/crowdsec-partof.conf`):

    ```bash
    sudo systemctl edit crowdsec-firewall-bouncer   # paste the [Unit] stanza
    sudo systemctl daemon-reload
    ```

    Warning sign that the guard is missing: `nft list tables` shows no `crowdsec`
    table after an enrollment or reload.

Also apply the `nut-monitor` network-online drop-in
(`deploy/nut/nut-monitor-network-online.conf`) on each secondary so `upsmon`
starts after the network is actually up, not merely configured — otherwise it
logs a spurious comm-loss on boot until DHCP settles.

## 2c. Serial push far-end setup (`--method serial`)

**Operator-only actions on the far-end machine** (e.g. `mt`) — the orchestrator
never touches another host's `/etc`; this is reference guidance for whoever
administers that box, and Phase 2 ships **code only**: nothing here has been
carried out against a live host as part of this phase, and `shutdown.enabled`
stays `false` in production.

### Primary-side prerequisite: device access (`dialout`)

Before any of the far-end setup below matters, the **primary** must be able to
open the serial device at all. `deploy/install.sh` adds the run user to
`dialout`, which is the group Debian puts `/dev/ttyUSB*` and `/dev/ttyS*` in:

```bash
id -nG "$USER" | tr ' ' '\n' | grep -qx dialout || echo "NOT in dialout"
# group membership only takes effect on a new login session:
sudo usermod -aG dialout "$USER" && echo "log out and back in"
```

Without it every serial push and every `ups-orchestrator shutdown rehearse`
fails with `PermissionError` on the device open — the watch loop runs as your
user under `systemd --user`, not as root. Group changes do **not** apply to an
already-running session or an already-started user service, so after a fresh
install: re-login, then `systemctl --user restart ups-orchestrator-watch`.

**The `nut` user is deliberately not in `dialout`, and does not need to be.**
The upssched dispatcher runs as `nut`, but the shipped
`deploy/nut/upssched.conf.snippet` only wires `ONBATT`, `ONLINE`, `LOWBATT`,
`COMMBAD` and `COMMOK` — none of which reach the push gate (see
[Shutdown-Mechanisms](Shutdown-Mechanisms.md)). **The serial push is not
reachable from the NUT event path at all**; it fires from the poll loop, which
runs as your user. If you add an `AT ... EXECUTE remote_shutdown` rule to
`upssched.conf`, you must add `nut` to `dialout` too — otherwise that rule
fails with `PermissionError` at the one moment it exists to work.

### Far-end prerequisites

A `serial` push writes a shutdown command into a passwordless/auto-login getty
on the target's console tty. Three prerequisites, **scoped to that one serial
tty only** — none of this should loosen SSH or the physical console login:

1. **Auto-login getty on the serial console, at the SAME baud the primary
   uses.** On a systemd host, override the console-getty unit for the specific
   serial tty (name unverified for any given box — commonly `ttyS0`/`ttyS1`;
   the operator confirms it against that machine's actual wiring):

   ```bash
   sudo systemctl edit serial-getty@ttyS0.service
   ```

   ```ini
   [Service]
   ExecStart=
   ExecStart=-/sbin/agetty --autologin youruser -L %I 9600 $TERM   # 9600 = EXAMPLE
   ```

   (The exact flags depend on your distro's `agetty` version — treat this as
   a starting point to verify against `man agetty` on the target, not a
   drop-in guarantee.) The baud given here (`9600` is only an EXAMPLE — this deployment's Dell
   console actually runs at 115200) **must match** what
   the primary declares as `serial_baud` for this machine — see the baud
   call-out in [Configuration](Configuration.md). A mismatch here is exactly
   the silent failure mode described there: the write still "succeeds" (rc 0)
   and nothing happens.

2. **NOPASSWD for shutdown/poweroff**, if the auto-login user above is not
   root — the pushed command (`shutdown_cmd`, see
   [Configuration → per-transport shutdown_cmd](Configuration.md)) must be able
   to run without a password prompt, or it will sit on the console waiting for
   input that a push over a 3-wire cable can never provide:

   ```
   youruser ALL=(root) NOPASSWD: /sbin/shutdown, /sbin/poweroff
   ```

3. **BIOS/SOL must not steal the tty from the OS getty post-boot.** On a Dell
   PowerEdge (or similar server BMC), Serial-Over-LAN can redirect the same
   physical UART to the BMC console after boot, in which case the pushed bytes
   reach the BMC, not the running Linux getty, and the "shutdown" never
   happens. Confirm SOL is disabled for the tty you're wiring to, or that it
   hands the port back to the OS before this path is relied on.

### Known limitation: primary-dies-first (b2 / c-OL)

The native secondary model has one irreducible hole. Secondaries monitor `upsd`
**on the primary** over TCP. If the primary (eulerpi5) or the network switch
between them dies on an **otherwise healthy grid**, the secondaries are left
last-known-`OL` and *blind*: `upsmon` treats prolonged comm-loss as a **warning
(NOCOMM), not a shutdown**, by design. They stay up but unprotected against a
*subsequent* outage until the feed returns.

**Option A (shipped mitigation).** Each secondary runs `powervalue 1` +
`MINSUPPLIES 1` + `DEADTIME 30`. This covers every path where the grid is *really*
down: a genuine on-battery outage still triggers shutdown, and if the primary
dies mid-outage (everything already `OB`), each secondary's local
`DEADTIME`-on-`OB` timer declares the UPS dead and shuts down (dead-UPS
inference, Path B2). What Option A does **not** cover is the *healthy-grid*
crash: primary or switch dies while everything is `OL`, so there is no `OB`
state to infer from — NOCOMM warnings only, no shutdown.

**Do not claim the SSH/serial backup closes this hole.** The default-off backup
runs *on eulerpi5* — it dies with the primary (mode b) and cannot cross a dark
switch (mode c). Stating otherwise would be a documentation correctness bug.

**Option B (the designated future fix, not implemented here):** a second,
independent `upsd` (e.g. on the rpi4, in a separate power + network domain) that
the secondaries also monitor. That gives a surviving monitor when the primary is
gone. It is a topology change, deferred to a future phase.

**Wiring recommendation (mitigation D):** put the network switch **and** eulerpi5
on the longest-runtime UPS. It does not close the b2/c-OL hole, but it shrinks the
window in which the coordinator or its network path goes dark before the
secondaries.

## 3. User services (no sudo)

```bash
loginctl enable-linger "$USER"
deploy/install-user-service.sh
```

This installs:

- `ups-orchestrator-watch.service`: continuous poll loop for the opt-in shutdown
  policy and on-battery countdowns.
- `ups-orchestrator-recorder.service`: one-second UPS telemetry samples for
  power-loss forensics. It retains twenty 50 MB historical segments plus the
  active file (roughly two weeks at the live three-UPS record size) and records
  self-test, output-shutdown timer, and alarm fields alongside load/voltage.
- `ups-orchestrator-boot-audit.service`: one-shot post-boot alert when the host
  recovered from abrupt power loss.
- `ups-orchestrator-report.timer`: daily Discord report of battery, estimated
  time to 0%, load, and voltage for every configured UPS. On Mondays it also
  posts the **power dashboard** image (per-UPS cards, a draw-over-time chart, and
  daily-kWh usage bars for the week) — no separate timer. This needs `matplotlib`
  in the install venv; add it once with
  `sudo /opt/ups-orchestrator/venv/bin/pip install matplotlib` (or
  `pip install ups-orchestrator[dashboard]`). Without it the report still sends;
  the image is skipped with a logged warning.
- `ups-orchestrator-selftest.timer` / `.service`: weekly NUT battery self-test
  with a Discord alert on failure. **Installed but not enabled** — the test
  discharges the pack, and it needs a NUT admin account (`instcmds` in
  `/etc/nut/upsd.users`) exported as `UPS_NUT_ADMIN_USER` /
  `UPS_NUT_ADMIN_PASSWORD` in `/etc/ups-orchestrator.env`. Arming it is your
  call: `systemctl --user enable --now ups-orchestrator-selftest.timer`.

You can test the report path immediately:

```bash
ups-orchestrator report --print   # terminal preview
ups-orchestrator report           # send Discord webhook
ups-orchestrator notify-test      # send test embed and print delivery result
ups-orchestrator audit            # boot/UPS/state/shutdown evidence report
ups-orchestrator logs events      # tail durable local UPS event JSONL
ups-orchestrator logs notifications
```

## Maintenance windows (planned power cuts)

```bash
ups-orchestrator maintenance begin --hours 2 --reason "recabling the rack"
ups-orchestrator maintenance status
ups-orchestrator maintenance end
```

Declare one **before** you pull a plug on purpose.

The reason is that a deliberate cut and a real outage are indistinguishable from
this host. Both leave every UPS reporting `OL` right up to the instant the host
dies — there is no in-band signal that says "a human did this". The boot audit
therefore falls back to filesystem-recovery evidence in the boot journal, which
fires on plenty of ordinary boots. Powering everything down on purpose to redo
cabling produced three critical Discord alerts in one evening. Only the operator
knows which it was, so the operator gets to say so in advance.

While a window is open, `ups-orchestrator boot-audit` suppresses its critical
power-loss alert and records why. It is the **last** of three suppression gates,
which matters for what a window is actually for:

1. the boot was already audited (the per-boot marker),
2. the **previous** boot logged systemd's own shutdown sequence — a clean
   `poweroff`/`reboot`, and no amount of fsck noise makes that an outage,
3. an operator-declared maintenance window is open.

Gate 2 already covers an orderly `sudo poweroff`. A window is what you need for
the case gate 2 cannot see: the cut that leaves the journal stopping mid-line,
because you really did pull the plug.

| Flag | Default | Meaning |
|------|---------|---------|
| `--hours` | `4` | Window length for `begin`. Must be greater than zero (rc 2 otherwise) |
| `--reason` | `""` | Free text, echoed into the suppression record so a silent boot audit still says who silenced it and why |

`begin` overwrites any open window rather than extending it, so re-running it is
how you lengthen one. `end` prints `no window was open` and still exits 0 if
there was nothing to clear — ending a window you are unsure about is always
safe. `status` prints `no window open — power-loss alerts are ARMED` when
nothing is set.

!!! warning "The expiry is the safety feature — do not wish it away"
    A window is time-bounded on purpose. A boolean "maintenance mode" flag that
    an operator forgets to clear silences outage alerting **forever**, and that
    failure is invisible precisely because nothing gets delivered: a silenced
    monitor and a quiet month look identical from the outside. An expiry makes
    the armed state the default one — the system re-arms itself whether or not
    anyone remembers. Prefer a short window you re-open over a long one you
    intend to close.

The marker is `/var/lib/ups-orchestrator/maintenance.json` (override with
`$UPS_ORCH_MAINTENANCE_STATE`), holding just the expiry and the reason. Anything
unreadable — missing, truncated, not JSON, no `until` — is treated as **no
window**, so a corrupt marker fails toward alerting rather than toward silence.
Deleting the file by hand is equivalent to `maintenance end`.

## Health checks and repair

Three targets exist for the times something looks wrong. Run the read-only one
first.

```bash
make nut-status                                  # read-only, NO sudo
sudo make nut-repair-listen                      # restore upsd's loopback LISTEN
sudo make install-config CONFIG=/path/to.json    # validate, then install into /etc
```

### `make nut-status`

Read-only and unprivileged — it changes nothing, so it is always safe to run
first and it will not mask the fault by restarting something. It prints, in one
screen:

- `is-active` for `nut-server`, `nut-monitor` and `nut-driver.target`, plus the
  `--user` units `ups-orchestrator-recorder` and `ups-orchestrator-watch`;
- the **active** (uncommented) `LISTEN` lines in `/etc/nut/upsd.conf`;
- whether a bare `upsc -l` is answered — printing
  `REFUSED (run: sudo make nut-repair-listen)` when it is not;
- the most recent recorder sample, as a timestamp and each UPS's status, so you
  can tell "no data" apart from "bad data".

### `sudo make nut-repair-listen`

Guarantees `/etc/nut/upsd.conf` keeps a **loopback** `LISTEN`, restarts
`nut-server`, and verifies a bare `upsc -l` works before it exits non-zero.

It exists because of a genuinely nasty default. `upsd` listens on localhost only
when `upsd.conf` has **no `LISTEN` line at all**, and Debian ships the file with
every `LISTEN` commented out. Writing the first explicit `LISTEN` — as LAN
enrollment must — silently *replaces* that implicit default instead of adding to
it. That broke this host twice:

- every bare `upsc` was refused for two days, taking the local tooling with it;
- then a boot where `eth0` had no DHCP lease yet left `upsd` with no bindable
  address, so it exited, and systemd's restart limit killed `nut-server`
  outright.

Idempotent: if loopback is already listed it says so, skips the restart, and
removes the backup it took. When it does rewrite, it backs the file up to
`upsd.conf.bak-<epoch>` first and edits **in place** so the inode keeps its
mode, owner and ACL. It reuses whatever LAN address is already configured, and
the rewrite goes through the same tested function the daemon uses
(`nutclient.upsert_upsd_listen`), so there is one implementation of the rule and
no second one to drift. Needs the venv (`make venv`).

### `sudo make install-config CONFIG=...`

Never hand-copy a config into `/etc`. This target:

1. **Validates that the file actually LOADS** before touching the live one, and
   prints every load-time degrade notice it raises;
2. prints the topology the file implies — for each UPS, what it *shuts down* and
   what it *also powers* (the `devices` inventory above), so a recabling typo is
   visible as the wrong blast radius rather than discovered during an outage;
3. backs the live config up to `config.json.bak-<epoch>` if one exists;
4. installs it `0640 root:nut` with a `u:<user>:r` ACL, then prints the
   resulting mode and ACL.

The validation is in front of the install, not after it, because a config that
fails to parse is a monitoring-topology outage that looks healthy: `watch` would
poll zero UPSes and report no problem. The user granted the read ACL is
`$SUDO_USER`.

## Where things land

| Path | What |
|------|------|
| `/usr/local/bin/ups-orchestrator` | the orchestrator |
| `/usr/local/bin/upssched-cmd.sh` | NUT dispatcher |
| `/etc/ups-orchestrator/config.json` | per-UPS config (no secret) |
| `/etc/ups-orchestrator.env` | webhook + paths |
| `/var/lib/ups-orchestrator/state.json` | per-UPS state |
| `/var/lib/ups-orchestrator/samples.jsonl` | high-frequency UPS samples |
| `/var/lib/ups-orchestrator/events.jsonl` | UPS event/decision log |
| `/var/lib/ups-orchestrator/notifications.jsonl` | Discord delivery outcomes |
| `/var/lib/ups-orchestrator/maintenance.json` | open maintenance window (absent = alerts armed) |
| `/var/lib/ups-orchestrator/boot-audit.json` | per-boot marker, so one boot alerts once |
| `~/.config/systemd/user/ups-orchestrator-watch.service` | the poll loop |
| `~/.config/systemd/user/ups-orchestrator-recorder.service` | telemetry recorder |
| `~/.config/systemd/user/ups-orchestrator-report.timer` | daily UPS load report |
| `~/.config/systemd/user/ups-orchestrator-selftest.timer` | weekly battery self-test (installed, **not enabled**) |

Your real device ids, IPs, and the webhook stay on the machine under `/etc`;
none of that is in the repo.
