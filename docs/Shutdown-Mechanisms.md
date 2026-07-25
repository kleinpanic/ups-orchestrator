# Shutdown mechanisms: SSH push vs. native NUT

The orchestrator can power down UPS-fed machines three ways — `serial`, `remote`
(SSH), and `local` (see [Shutdown targets](Shutdown-Targets.md)). The SSH path is
the convenient one, and it is also the one with the most failure modes. This page
describes that pathology honestly and asks whether NUT's own distributed-shutdown
model is a better fit.

## What the SSH path actually does

`_default_ssh_shutdown` runs, per target:

```text
ssh -o BatchMode=yes -o ConnectTimeout=10 <user@host> "sudo /sbin/shutdown -h now"
```

The orchestrator **pushes** a shutdown command *into* each box over SSH when its
per-group policy (`battery_below` / `runtime_below`) trips.

```mermaid
flowchart LR
    subgraph orch["orchestrator host"]
      P[policy gate] -->|ssh sudo shutdown| K1
      P -->|ssh sudo shutdown| K2
    end
    K1["box A (sshd + sudo)"]
    K2["box B (sshd + sudo)"]
    NET{{"network switch / router<br/>⚠ often on the same failing power"}}
    P -.->|every command traverses| NET
    NET -.-> K1
    NET -.-> K2
```

## The pathology

1. **Network dependency in the exact failure window.** A grid outage is *when* you
   need the shutdown, and it is also when the switch/router/AP between the
   orchestrator and the target is most likely dark (unprotected, or on another
   draining UPS). SSH then times out (`ConnectTimeout=10`) and the box never shuts
   down gracefully — it hard-drops when its UPS empties. Serial exists precisely
   because it sidesteps this; SSH does not.
2. **It reimplements NUT's distributed shutdown — with more moving parts.** NUT
   already solves "shut down every machine on a dying UPS" with the primary/secondary
   model (below). The SSH path rebuilds a slice of that as an outbound command
   channel that needs `sshd`, a login, a shell, and a passwordless `sudo shutdown`
   on every target.
3. **Credential/trust inversion.** SSH shutdown means the orchestrator holds keys
   that can run `sudo shutdown` on *every* box — a lateral-movement liability that
   points the wrong way (one host able to halt the fleet). NUT's model inverts this:
   each box pulls state from the primary and shuts *itself* down.
4. **No coordination with NUT's own state.** The SSH decision runs on the
   orchestrator's thresholds, independent of NUT's `LOWBATT`/FSD. Two shutdown
   authorities on one UPS with no shared sequencing; the "local dies last" ordering
   is a hand-rolled fragment of what NUT sequences natively.
5. **Fire-and-forget correctness gaps.** A box mid-shutdown may drop the SSH
   connection, so a *successful* shutdown can return non-zero and page a false
   failure.

## The native NUT model (primary / secondary + FSD)

In NUT, one host is the **primary** (runs `upsd`, owns the UPS). Every other
UPS-fed host runs `upsmon` as a **secondary**, monitoring that `upsd` over TCP.
No SSH, no keys pushed around, no reverse command channel — the secondary shuts
*itself* down. There are **two** ways it decides to, and which one applies depends
on the primary's `powervalue` for that UPS:

- **Primary-driven FSD** — if the primary declares `powervalue ≥ 1` for the UPS
  (i.e. it is itself fed by it), then when the primary hits low-battery it raises
  the **FSD** (forced-shutdown) flag; every secondary polls it off `upsd`
  (`ups.status` → `OB LB FSD`) within one `POLLFREQALERT` and runs `SHUTDOWNCMD`.
- **Secondary self-trigger** — if the primary is `powervalue 0` for that UPS
  (monitors it but isn't powered by it — *this is exactly eulerpi5's case for the
  Rack and Third UPSes*), the primary never raises FSD for it. The secondary must
  therefore reach the decision itself: with `powervalue 1` + `MINSUPPLIES 1` it
  shuts down the moment it reads its own UPS as `OB LB` (or after `DEADTIME` if the
  UPS was last seen `OB` and then goes silent).

Either way the secondary runs plain `shutdown -h now` — a secondary must **never**
run `killpower`/`POWERDOWNFLAG` (it has no UPS to power off; doing so is a known
foot-gun).

```mermaid
flowchart RL
    UPSD["upsd on primary<br/>(sets FSD when critical)"]
    S1["secondary upsmon<br/>box A → local shutdown"]
    S2["secondary upsmon<br/>box B → local shutdown"]
    S1 -->|MONITOR pulls status| UPSD
    S2 -->|MONITOR pulls status| UPSD
    UPSD -.->|FSD flag| S1
    UPSD -.->|FSD flag| S2
```

This is the credential-minimal, coordinated, pull-based design NUT is built for.
It removes pathologies 2–5 outright — but it introduces **one new gap of its own**:

!!! warning "The primary-dies-first hole"
    Secondaries monitor `upsd` **on the primary**. If the primary's *own* UPS dies
    first, the primary powers off, `upsd` vanishes, and the secondaries lose their
    monitor for *their* UPS. During a real outage (everything already `OB`) each
    secondary's local `DEADTIME`-on-`OB` timer still fires, so they shut down safely.
    But if the primary (or the network switch) dies on an otherwise **healthy grid**,
    the secondaries are left up-but-blind until the feed returns. `upsmon` treats
    prolonged comm-loss as a *warning*, not a shutdown — by design. The durable fix
    is a **second, independent `upsd`** the secondaries also monitor; until then this
    is a documented limitation, and the default-off serial deadman below is the
    stop-gap.

## The honest trade-off

| | SSH push (current) | Native NUT (secondary + FSD) | Serial |
|---|---|---|---|
| Survives a dead network | ❌ | ❌ (upsd is over TCP) | ✅ |
| Credential surface | keys + sudo on every box | read MONITOR creds only | none (getty) |
| Coordinated with NUT | no | yes (native) | no |
| Per-**box** threshold on one UPS | ✅ | ❌ (FSD is per-UPS) | ✅ |
| Works on non-NUT appliances | ✅ | ❌ (needs nut-client) | ✅ (any console) |
| Agent required on target | sshd + sudo | nut-client | auto-login getty |

Native NUT does **not** fix the network pathology — `upsd` is reached over TCP, so
a secondary is as blind as SSH when the switch is dark. Only **serial** is
network-independent. What native NUT fixes is the *credential inversion*, the
*coordination gap*, and the *reimplementation* — for boxes that can run nut-client.

## Recommended shape

Keep the orchestrator as the **policy brain**, but prefer *wrapping* NUT over
pushing SSH:

- **Network-reachable, nut-capable boxes** → run them as **NUT secondaries** with
  `powervalue 1` + `MINSUPPLIES 1` so each self-triggers on its own `OB LB` (the
  primary here is `powervalue 0` for those UPSes and won't raise FSD for them).
  Credential-minimal, coordinated, no SSH fan-out.
- **Serial** → keep as the **network-independent backstop** for the boxes that
  matter most. This is the only path that survives the outage-network case, so it
  is not replaceable by either SSH or native NUT.
- **SSH / serial backup** → **default-off**, and positioned as a deadman *strictly
  below* the native LB threshold: it fires only if native shutdown *didn't* while
  the primary was still alive. It does **not** cover the primary-dies-first hole
  (it dies with the primary) — that's what the future redundant `upsd` is for.

The per-box thresholds the current policy layer offers are the one thing plain FSD
can't express; if that granularity is actually needed, it belongs at the primary's
decision logic (the orchestrator) driving FSD, not in an SSH fan-out.
