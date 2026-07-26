# Configuration

Config is a single JSON file. It holds no secrets; the Discord webhook comes
from an environment variable, so the file itself is safe to keep around.

The orchestrator looks for the config in this order: `$UPS_ORCH_CONFIG`, then
`/etc/ups-orchestrator/config.json`, then `config.json` next to the repo. State
(per-UPS bookkeeping) resolves the same way via `$UPS_ORCH_STATE`, then
`/var/lib/ups-orchestrator/state.json`.

Local forensic logs are controlled by:

- `$UPS_ORCH_SAMPLES`: high-frequency UPS samples.
- `$UPS_ORCH_EVENT_LOG`: UPS event/transition/shutdown-gate records.
- `$UPS_ORCH_NOTIFICATION_LOG`: Discord delivery attempts and results.

## Top-level keys

| Key | Default | Meaning |
|-----|---------|---------|
| `discord_webhook_env` | `UPS_DISCORD_WEBHOOK` | Env var the webhook URL is read from |
| `discord_webhook_url` | `""` | Fallback URL if the env var is unset (leave empty) |
| `discord_username` | `UPS Orchestrator` | Name shown on the embeds |
| `discord_avatar_url` | `""` | Optional avatar for the webhook |
| `poll_seconds` | `30` | How often `watch` checks UPS state and the shutdown policy |
| `countdown_every_seconds` | `60` | On-battery countdown post cadence (`0` turns it off) |
| `load_step` | on | Output-load collapse detection (a downstream device dying) |
| `shutdown` | disabled | Central opt-in policy for orchestrator-managed shutdowns |
| `nut_server` | localhost-only | Primary-side `upsd` exposure (see below) |
| `monitored_machines` | `[]` | Machines enrolled via `monitor add --method ...` (native/serial/ssh/none, see below) |
| `upses` | — | Map of NUT device name → per-UPS settings |

## Shutdown policy

`shutdown.enabled` is the master switch. It defaults to `false`, so no target
command runs unless you explicitly enable the policy and the relevant group.

- `require_power_outage`: require the UPS to be on battery before targets run.
- `min_on_battery_seconds`: minimum outage duration before target commands can run.
- `notify`: send Discord attempt/result notifications for every target command.
- `external`: central thresholds for `serial` and `remote` targets.
- `internal`: central thresholds for `local` targets.

When battery and runtime readings are both available, both must be at or below
their group thresholds. That prevents a high battery-percent threshold from
shutting machines down while the UPS still reports healthy runtime.

!!! warning "This gate applies regardless of `shutdown_method` — and both default off"
    `shutdown.enabled` plus the relevant group flag (`external` for a
    `serial`/`ssh` machine or a legacy `remote`/`serial` target, `internal` for
    a `local` target) gate **every** automatic shutdown the orchestrator can
    issue — completely independent of what a given machine's `shutdown_method`
    is set to. A machine correctly configured with `shutdown_method: "serial"`
    will still never fire while `shutdown.enabled` (or its group) is `false`.
    **Both default to `false`.** Phase 2 ships with the policy off in
    production; do not flip `shutdown.enabled` unless you mean it.

    Two commands exist to inspect this without flipping anything:

    - `ups-orchestrator remote-shutdown [ups] --dry-run` **bypasses nothing.**
      It resolves every target this UPS would push to or fire locally and
      prints, for each, the transport, the resolved command, and the gate's
      current verdict — so a disabled policy shows up as a full listing
      annotated `would fire: no — shutdown policy disabled`, not a blank
      screen and not a lie.
    - `ups-orchestrator shutdown rehearse <machine>` deliberately does **not**
      consult this policy at all: its command is hard-coded to a non-shutdown
      `logger` invocation, so gating it on `shutdown.enabled` would make it
      unusable in exactly the state (`false`) it exists to be used in.

## NUT server (`nut_server`)

Primary-side `upsd` exposure. It holds **no secrets** — the secondary NUT
password never lives here.

| Key | Default | Meaning |
|-----|---------|---------|
| `listen` | `["127.0.0.1", "::1"]` | Addresses `upsd` binds. Localhost-only by default |
| `port` | `3493` | `upsd` TCP port |
| `secondary_user` | `upsmon_secondary` | The `upsd.users` account secondaries authenticate as |

`listen` is localhost-only on a fresh config so `upsd` is never silently exposed
to the network. `monitor add` appends the primary's LAN address at enrollment
time (and applies the matching `upsd.conf` LISTEN + a full `nut-server`
restart — see [Deployment](Deployment.md)). A LISTEN change needs a **full
restart**, not a reload.

## Monitored machines (`monitored_machines`)

A machine the orchestrator knows how to power down. Every entry carries exactly
one **effective shutdown method** — the single per-machine authority — set by
`monitor add --method {none|native|serial|ssh}`. The array ships **empty**;
entries are added by `monitor add`, not by hand.

| Field | Default | Meaning |
|-------|---------|---------|
| `name` | — | Machine name (also the `ssh` alias if none given) |
| `shutdown_method` | `none` | **`none` \| `native` \| `serial` \| `ssh`** — the single effective shutdown authority for this machine. Equal weight with every other field below. |
| `ssh` | `""` | `ssh_config` Host alias. Required for `shutdown_method: native` (bootstraps the NUT secondary over this alias) and `ssh` (the push destination); ignored for `serial`/`none`. |
| `ups` | `""` | NUT UPS name the machine is powered by (e.g. `cyberpower`). `monitor add --ups` is **required** for every method except `none` — a push record with no `ups` could never be projected onto any UPS's low-battery event, so the CLI refuses it up front (rc 2) rather than accepting a dead-letter entry. `--method none` is the only method where `--ups` is optional at the CLI: omitted, the record carries no shutdown authority to project in the first place; given anyway, it is stored and makes `monitor verify` run the BL-02 advisory probe described below. |
| `powervalue` | `1` | `1` = powered by this UPS (counts toward `MINSUPPLIES`); never `0` for a real feed. Only meaningful for `native` |
| `os` | `auto` | `auto` \| `arch` \| `ubuntu` \| `debian` — picks the install/config path. Only consulted for `native` |
| `shutdown_cmd` | `/sbin/shutdown -h now` | Command run on trigger. **Per-transport, not universal — see below.** |
| `ip` | `""` | Resolved source IP added to the firewall `saddr` set. Written only by the `native` enrollment path |
| `serial_device` | `""` | `shutdown_method: serial` only — the console device, e.g. `/dev/serial/by-id/usb-...` (prefer the by-id path over a bare `/dev/ttyUSB0`, which can renumber across reboots) |
| `serial_baud` | *(none — disarms if absent)* | `shutdown_method: serial` only. **Never assumed.** An absent or unparseable `serial_baud` disarms the machine at load rather than guessing a rate. See the baud call-out below. |
| `backup` | `{enabled: false, kind: "remote"}` | **LEGACY.** Still parsed for back-compat; drop it from new entries — see below |

!!! danger "Baud is yours to declare — and a wrong value is invisible to the orchestrator"
    The live rpi5 console line here runs at **9600**. Declare the *real* baud of
    the machine you are enrolling; do not copy a value from another console.
    `stty -F <device> <baud>` only configures the **local** tty — it returns
    success for 9600, 19200, 115200, and effectively any recognised rate on the
    same physical line, and the byte write completes regardless of whether the
    far end is listening at that speed. **A wrong `serial_baud` sends garbage
    down the wire and the orchestrator reports nothing wrong: rc 0, no
    exception, no shutdown.** What *is* detected and reported is a rejected
    *local* line configuration (a malformed baud, or a value `stty` itself
    refuses) — never a far-end speed mismatch. There is no substitute for the
    operator confirming the real console speed (e.g. from the far end's own
    getty/console configuration) before declaring it here.

### Three ways a machine ends up with a method

1. **A newly created record** (one you write with an explicit `shutdown_method`)
   uses exactly that value; an unrecognised or blank value coerces to `none`
   (logged) rather than silently activating a transport.
2. **A legacy record with no explicit `shutdown_method`** derives one from its
   old shape, evaluated in this order: a non-empty `ups` → `native` (checked
   *first*, so a Phase-1 native secondary with `ups` set and a stale
   `backup{enabled:false}` still derives `native`, never a push); otherwise an
   enabled legacy `backup` → `serial` or `ssh` per its `kind`; otherwise `none`.
3. **Explicit `"shutdown_method": "none"`** always means off, even for a legacy
   shape that would otherwise have derived something — an explicit value always
   wins over derivation.

A derived push method (case 2's `serial`/`ssh` branch) is a dead letter unless
you also give the record an explicit `ups`: a push method is derived *only*
when `ups` is empty, and the machine is only ever projected onto a shutdown
attempt for a UPS whose name matches its own `ups` field — so a derived push
with no `ups` can never fire, is reported as such at load, and must be given
an explicit `shutdown_method` and a `ups` to actually work.

**Deferred:** a native-plus-deadman regime — where a `backup` block fires as a
last-resort trigger strictly below a native secondary's own LB point — was
scoped for this phase and **dropped**; it may return in a future phase.

### Worked example

```json
"monitored_machines": [
  {
    "name": "mt",
    "shutdown_method": "serial",
    "serial_device": "/dev/serial/by-id/usb-FTDI_FT232R_USB_UART-if00-port0",
    "serial_baud": 9600,
    "ups": "cyberpower"
  },
  {
    "name": "spark",
    "shutdown_method": "native",
    "ssh": "spark",
    "ups": "cyberpower3",
    "powervalue": 1,
    "os": "ubuntu",
    "ip": "192.168.1.141"
  }
]
```

`backup` is deliberately absent from both entries — see LEGACY below.

### Per-transport `shutdown_cmd`

`shutdown_cmd` is not one field with one meaning: the same string runs under
**two different privilege contexts** depending on `shutdown_method`.

- **`native`** — `upsmon` on the secondary runs `SHUTDOWNCMD` **as root**, so
  the default `/sbin/shutdown -h now` is correct unmodified.
- **`serial` / `ssh`** — the command runs as whichever user the ssh connection
  or the far-end auto-login getty executes as. If that is not root, a bare
  `/sbin/shutdown -h now` needs an escalation prefix (e.g. `sudo /sbin/shutdown
  -h now`) — **and the failure mode differs by transport.** Over ssh, a
  permission failure is a non-zero rc you'll see. **Over serial it is silent:**
  the bytes are delivered, the write returns rc 0, and the box simply stays up.
  Declare an escalated `shutdown_cmd` for a push record unless the far end logs
  in as root, and see [Deployment → mt far-end getty setup](Deployment.md) for
  the NOPASSWD configuration that makes this safe on the far end.

### Enrollment and lifecycle CLI

```bash
# Enroll — --method selects the authority. native is the default (back-compat
# with pre-Phase-2 invocations that omit --method); --ssh is required only for
# native and ssh. --ups is required for every method EXCEPT none (rc 2 if
# missing) — a push with no ups can never be projected onto a low-battery
# event, so the CLI refuses the dead-letter record up front.
sudo ups-orchestrator monitor add mt --method serial \
     --serial-device /dev/serial/by-id/usb-FTDI_FT232R_USB_UART-if00-port0 \
     --serial-baud 9600 --ups cyberpower

sudo ups-orchestrator monitor add spark --method native --ssh spark --ups cyberpower3 --os ubuntu

sudo ups-orchestrator monitor add somebox --method ssh --ssh somebox --ups cyberpower

# --method none is the ONE method where --ups is optional — omit it entirely
# for "no shutdown authority at all", or pass it if this box is still worth
# probing for a stray live secondary (see the monitor verify table below).
sudo ups-orchestrator monitor add somebox --method none

# Method-aware lifecycle
ups-orchestrator monitor list      # declared method, and the effective method when it differs;
                                    # plus every Config.degraded notice, ERROR/ADVISORY labelled
ups-orchestrator monitor verify mt # "will this machine actually shut down?" — see below
sudo ups-orchestrator monitor remove mt   # native: real remote NUT teardown + nft/saddr update;
                                    # serial/ssh/none: config entry only

# Preview without touching anything
ups-orchestrator remote-shutdown cyberpower --dry-run
# Prints every resolved target (configured shutdown_targets[] + monitored_machines
# projections) with its transport, resolved command, and CURRENT gate verdict —
# e.g. "would fire: no — shutdown policy disabled" or
# "would fire: no — UPS is not close to empty (...)". Touches shutdowns_sent,
# the notifier, and the event log NOT AT ALL.

# Push a harmless, hard-coded, non-shutdown probe over the real transport
ups-orchestrator shutdown rehearse mt
# Sends `logger -t ups-orchestrator PHASE2_REHEARSAL` over mt's configured
# transport — NEVER the real shutdown_cmd, and never a local target. Ignores
# shutdown.enabled entirely (see the warning above) because the command itself
# cannot halt anything. Persists nothing.
```

**`monitor verify`'s answer, by declared method:**

| Declared method | What verify does | Result |
|---|---|---|
| `native` | Always runs the NUT secondary probe (`verify_secondary`) — this is the only evidence available on this box about an authority that lives on another one | rc 0 if the secondary answers, rc 1 if not; if a load notice is present it is shown alongside, naming `monitor remove <name>` as the real disarm |
| `none` **with** a non-empty `ups` | Also runs the secondary probe, as an advisory — this is exactly BL-02's signature (an operator may have hand-edited a former native record to `none` without actually tearing it down) | If a secondary answers: reports it, **rc 1**; if not: **rc 0**. Falsely reporting "no active authority" here would be the whole problem |
| `none`, no `ups` | No probe | `no active shutdown authority`, **rc 0** |
| `serial`/`ssh` declaring a non-empty `ip` | This shape is **always disarmed at load** (IW-05 — `ip` is written only by the native enrollment path, so a push record carrying one is almost certainly a former native secondary that was hand-edited rather than torn down): prints `DISARMED (declared serial/ssh): …` **and still runs the secondary probe** | **rc 1** regardless of what the probe finds — the probe result is shown for diagnosis, but the disarm alone already forces rc 1 |
| `serial`/`ssh`, **disarmed for any other reason** (missing device/alias, unresolvable `ups`, dual-regime conflict, …) | No probe — there is no `ip` to suspect a stray secondary over | `DISARMED (declared <method>): <reason>` plus the remedy, **rc 1** — deliberately never rendered as a plain "no active authority", which would be indistinguishable from a genuine `none` |
| `serial`, **armed** | Checks the recorded device's presence (a stat, injected in tests) | rc 0 if present and a character device with a usable declared baud, rc 1 otherwise |
| `ssh`, **armed** | Probes the alias's reachability (`ssh <alias> true`) | rc 0 if reachable, rc 1 otherwise |
| Config `ups` value is a NUT-metacharacter string, or the `ssh` alias is option-shaped/invalid | Refuses to run the probe (never shells out with an unvalidated value) | **rc 2** |
| Unknown machine name | — | **rc 2** |

`monitor verify`/`monitor add`/`monitor remove` share **rc 2** for a config or
argument problem the command refuses to act on: an unknown machine, an
invalid `--ups`/`--ssh`/`--primary-ip` literal, or (on `monitor add`) a
missing method-required flag such as `--ssh` on `native`/`ssh` or `--ups` on
anything but `none`. Those specific checks are hand-written validation inside
`_monitor_add`/`_monitor_verify` — a plain `return 2` from `main()`, not a
raised `SystemExit`. An invalid `--method`/`--os`/`--powervalue` **choice**
still exits 2 the ordinary argparse way (`parser.error` → `SystemExit(2)`),
which any caller sees as the same process exit code but is a different code
path than the hand-written checks.

A `monitor add` that would switch an existing **declared-`native`** record to
`serial`/`ssh`/`none` is **refused** — `monitor remove <name>` first, which is
the only command that runs the real remote NUT teardown. Leaving the switch
unrefused would silently orphan the old secondary's `upsmon` while also
activating a push, i.e. two live shutdown authorities on one machine.

!!! danger "`--force` and `--force-remote-config` authorise two DIFFERENT things — never conflate them"
    `monitor add` has two independent force flags. Passing one never implies
    the other, and confusing them lets an operator who only meant to clear a
    **local** warning also clobber a **third machine's** NUT config:

    - **`--force`** overrides only the **local** dual-regime refusal — the
      guard that fires when a machine's active `shutdown_method` and an
      enabled legacy `shutdown_target` on the same UPS would both govern it
      (double-shutdown risk). It authorises nothing on any remote host.
    - **`--force-remote-config`** authorises a `native` enrollment to
      overwrite an **unmarked** remote `/etc/nut/upsmon.conf` (one carrying a
      `MONITOR` line outside the orchestrator's own managed block — i.e. an
      operator's hand-written config) or to demote a remote `/etc/nut/nut.conf`
      away from `MODE=standalone`/`MODE=netserver` (a real NUT server on that
      box). Without it, `monitor add` refuses to write and reports which
      condition tripped the refusal, changing nothing on the remote box.

    Example — enrolling over a remote that already runs its own NUT server,
    with a pre-existing local double-shutdown conflict:

    ```bash
    sudo ups-orchestrator monitor add mt --method native --ssh mt --ups cyberpower \
         --force --force-remote-config
    ```

    Passing only `--force` here clears the local conflict but still refuses
    to touch `mt`'s existing NUT config; passing only `--force-remote-config`
    authorises the remote overwrite but still refuses at the local
    dual-regime guard. Both are required together only when both conditions
    are actually present — neither flag does anything the other's condition
    doesn't trigger.

### Legacy: `shutdown_targets[]` and `backup` (back-compat only)

`shutdown_targets[]` (per-UPS) and `MonitoredMachine.backup` (per-machine) are
**LEGACY** — still parsed so old config files keep loading, but neither is how
a new entry should be written. **`backup` has never driven a shutdown**:
nothing at runtime reads it — it is parsed and displayed only — so the
serial/SSH push a `backup` block appears to promise has always been
imaginary. New entries should drop it entirely (see the worked example above)
and declare `shutdown_method` instead. See [Shutdown Targets](Shutdown-Targets.md)
for the full legacy per-UPS `shutdown_targets[]` reference.

### The secondary NUT password

The secondary's NUT credential lives in `/etc/ups-orchestrator.env` as
`UPS_NUT_SECONDARY_PASSWORD`, **never** in `config.json`. It must match the
`[upsmon_secondary]` entry in `/etc/nut/upsd.users` on the primary (see
`deploy/nut/upsd.users.snippet`). `config.json` carries no secret.

### Mutual exclusion: degrade-and-disarm, not a load failure

A machine cannot be governed by **two** shutdown regimes at once: an active
`shutdown_method` (`native`/`serial`/`ssh`/`none` all count) *and* an enabled
legacy `shutdown_target` on the same UPS. `monitor add` **refuses without
`--force`** when it detects this locally at enrollment time. But a conflict
that reaches disk another way — a hand-edited file, or a config authored
before this rule existed — does **not** fail to load. `Config.load` disarms
whichever side of the conflict is disarmable and keeps running, logging an
`ERROR`-severity notice and surfacing it in `monitor list`, `monitor verify`,
the `status` view, and the web UI. The outcome differs by which authority
conflicts:

- **A `serial`/`ssh` machine** in conflict: its effective method is forced to
  `none` (disarmed) — it will not be pushed.
- **A `none` machine** in conflict: not applicable — `none` carries no
  authority to conflict over, but a conflicting *legacy target* naming it is
  still disabled.
- **The conflicting legacy `shutdown_target`** is always disabled and will not
  fire, regardless of which authority it collided with.
- **A `native` machine** in conflict is the one case config **cannot** disarm:
  its authority is the *remote* box's own `upsmon` reacting to this primary's
  FSD, configured entirely in that box's `/etc` — nothing a config parser does
  here reaches it. The conflicting legacy target is disabled, and the native
  secondary **remains armed** as the sole surviving authority. **Editing
  config never disarms a native secondary.** The only thing that does is
  `monitor remove <name>`, which runs the real remote NUT teardown.

In every case the daemon keeps running and keeps polling every configured UPS.
`Config.degraded` is the machine-readable surface for every such notice; it is
empty on a healthy config.

**The load only refuses outright (raises, refusing to start) for structural
monitoring-topology corruption, never for a shutdown-authority problem:** an
unreadable config file, malformed JSON, a non-object JSON root, an absent or
empty `upses` section, a non-object entry inside `upses`, a `upses` mapping
that becomes empty after filtering out non-object entries, or two UPS names
that canonicalise to the same key. Every one of those would leave `watch`
polling zero or ambiguous UPSes while looking healthy, which is worse than
refusing to start. Every shutdown-authority misconfiguration — a bad baud, a
dual-regime conflict, an unresolvable `ups`, a stale `ip` on a push record —
degrades instead.

### File permissions after a persist

`monitor add`/`monitor remove` rewrite `config.json` through an atomic
temp-file-then-replace. That replace now **preserves the destination file's
mode, owner/group, and POSIX ACL** across the rewrite — the installer's
intended on-disk state is `0640 root:nut` plus a read ACL for the run user
(`deploy/install.sh`), and a persist that does not preserve it can leave the
`--user` watch service unable to read its own config after a restart. If you
ever find `/etc/ups-orchestrator/config.json` at `0600 root:root` with no ACL
entry for the run user, that is **damage, not the intended shipped state** —
re-run `deploy/install.sh`'s permission step or restore the ACL by hand.

## Load-step drop detection

`load_step` flags an abrupt output-load collapse — the only in-band signature
NUT gives for a downstream device losing power while the UPS itself stays `OL`.
On by default; set `enabled` to `false` to silence it.

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Master switch for the check |
| `drop_percent` | `15` | Load-point drop below the recent peak that trips the event |
| `window_polls` | `4` | Polls whose peak the current load is compared against |
| `cooldown_seconds` | `600` | Minimum gap between notifications (events are always logged) |

The drop is measured against the highest load in the last `window_polls` polls
rather than just the previous poll, so a collapse that straddles a poll boundary
(the UPS reporting an intermediate value mid-decay) still trips instead of
splitting into two sub-threshold steps. A trip logs a `load_step_drop` event
with the estimated watts delta and sends one notification per `cooldown_seconds`;
the alert embeds a 10-minute draw-history sparkline built from the recorder
samples. It is a hint, not a verdict — a heavy job finishing looks identical —
so pair it with a reachability check on the device.

## Per-UPS

Each key under `upses` is a NUT device name (whatever you called it in
`ups.conf`). A UPS has a `label` (used in the embeds) and a list of
`shutdown_targets`; see [Shutdown Targets](Shutdown-Targets.md).

```json
{
  "discord_webhook_env": "UPS_DISCORD_WEBHOOK",
  "poll_seconds": 30,
  "countdown_every_seconds": 60,
  "load_step": { "enabled": true, "drop_percent": 15, "cooldown_seconds": 600, "window_polls": 4 },
  "shutdown": {
    "enabled": false,
    "require_power_outage": true,
    "min_on_battery_seconds": 120,
    "notify": true,
    "external": { "enabled": false, "battery_below": 15, "runtime_below": 300 },
    "internal": { "enabled": false, "battery_below": 10, "runtime_below": 120 }
  },
  "upses": {
    "rack": { "label": "Rack UPS", "shutdown_targets": [] },
    "desk": { "label": "Desk UPS", "shutdown_targets": [] }
  }
}
```

## The webhook

Set it in the environment, never in the file:

```bash
export UPS_DISCORD_WEBHOOK="https://discord.com/api/webhooks/…"
```

On a deployed box this lives in `/etc/ups-orchestrator.env`, which both the NUT
event path and the watch service read.

Use `ups-orchestrator notify-test` after editing the env file. It sends a test
embed and prints `ok`, `configured`, attempt count, and HTTP status without
printing the webhook URL.
