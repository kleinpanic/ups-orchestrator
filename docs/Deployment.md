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

## 2b. Enroll NUT secondaries (`monitor add`)

Network-reachable, nut-capable boxes shut *themselves* down as NUT secondaries
instead of being pushed a shutdown over SSH (the credential-minimal, coordinated
design — see [SSH vs. native NUT](Shutdown-Mechanisms.md)). `monitor add` runs on
the **primary** (eulerpi5) as root and drives the whole enrollment:

```bash
sudo ups-orchestrator monitor add mt --ssh mt --ups cyberpower --os arch --powervalue 1
```

What it does, in order:

1. **Bootstrap the primary.** Append the LAN `LISTEN` to `/etc/nut/upsd.conf`,
   add the `[upsmon_secondary]` account to `/etc/nut/upsd.users`, then run a
   **full** `systemctl restart nut-server`. A `reload` is a **no-op** for
   `LISTEN` — `upsd` only binds its sockets at start, so a reload leaves it on
   localhost and the secondary never connects. This is why the flow restarts
   rather than reloads.
2. **Firewall.** Add the secondary's source IP to the dedicated
   `table inet ups_orchestrator` nftables set (never the operator's own
   `filter`/`input` chain), write `/etc/nftables.conf`, and `nft -f` it — then
   **restart the crowdsec bouncer** (see the warning below).
3. **Remote install/config.** Over the `ssh` alias: install the NUT client for
   the box's distro (`nut` on Arch, `nut-client` on Ubuntu/Debian), write a
   secondary `upsmon.conf` (`powervalue 1`, `MINSUPPLIES 1`, `DEADTIME 30`, and
   **no** `POWERDOWNFLAG`/`killpower` — a secondary has no UPS to power off),
   and enable `nut-monitor`.
4. **Verify.** Confirm the secondary reads its UPS off the primary's `upsd`.

The secondary's NUT password comes from `UPS_NUT_SECONDARY_PASSWORD` in
`/etc/ups-orchestrator.env` and must match the `[upsmon_secondary]` entry on the
primary. It is never written to `config.json`.

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

You can test the report path immediately:

```bash
ups-orchestrator report --print   # terminal preview
ups-orchestrator report           # send Discord webhook
ups-orchestrator notify-test      # send test embed and print delivery result
ups-orchestrator audit            # boot/UPS/state/shutdown evidence report
ups-orchestrator logs events      # tail durable local UPS event JSONL
ups-orchestrator logs notifications
```

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
| `~/.config/systemd/user/ups-orchestrator-watch.service` | the poll loop |
| `~/.config/systemd/user/ups-orchestrator-recorder.service` | telemetry recorder |
| `~/.config/systemd/user/ups-orchestrator-report.timer` | daily UPS load report |

Your real device ids, IPs, and the webhook stay on the machine under `/etc`;
none of that is in the repo.
