"""Event handlers — the orchestrator's brain.

Two decoupled concerns share these handlers:

* **NUT-event webhooks** — ``onbatt``/``online``/``lowbatt``/``commbad``/``commok``
  fire from NUT's ``upssched`` and post per-UPS Discord embeds. NUT's own
  ``upsmon`` ``SHUTDOWNCMD`` remains the backstop that powers off this host.
* **Polling-driven shutdown** — the ``tick`` handler (run repeatedly by the
  ``watch`` loop at a configurable interval) can run configured
  ``shutdown_targets`` only when the top-level shutdown policy explicitly opts
  in, the UPS is on battery long enough, and the UPS is close to empty.
  ``local`` targets are always sequenced **after** every enabled ``remote``
  target on the same UPS, so the watcher host dies last. The on-battery
  countdown post has its own cadence and never gates shutdown decisions.

Side effects (snapshot reads, shutdowns, clock) are injected via :class:`Deps`
so the handlers unit-test without a real UPS or network.
"""

from __future__ import annotations

import json
import logging
import shlex
import subprocess
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from ups_orchestrator.config import (
    LoadStepPolicy,
    MonitoredMachine,
    ShutdownGroupPolicy,
    ShutdownTarget,
    UpsConfig,
    normalize_ups_name,
)
from ups_orchestrator.notify import Level, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import UpsState

LOG = logging.getLogger("ups_orchestrator.events")

EventLogger = Callable[[str, UpsConfig, UpsSnapshot | None, str, dict[str, object] | None], None]


# --- default side effects (overridable in tests) -----------------------------


def ssh_dest(target: ShutdownTarget) -> str:
    """SSH destination: ``user@host`` if a user is set, else just ``host``.

    Leaving ``user`` empty lets ``host`` be an ``ssh_config`` Host alias (e.g.
    ``mt``), so connection details (real hostname, port, key) live in
    ``~/.ssh/config`` rather than the orchestrator config.
    """
    return f"{target.user}@{target.host}" if target.user else target.host


# A transport runner's contract is *return a failure tuple, never raise*. The caller
# appends to ``state.shutdowns_sent`` AFTER the runner returns and holds the local
# targets until every remote has been sent, so a runner that escapes leaves the target
# unmarked and the local hosts unreached — the watcher Pi's own poweroff starves on the
# battery it shares with the machine that hung (T-02-24). Hence the broad catch: it is
# the contract, not laziness.
def _default_ssh_shutdown(target: ShutdownTarget) -> tuple[int, str, str]:
    dest = ssh_dest(target)
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", dest, target.cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        return 1, "", f"ssh transport to {dest} failed: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _default_local_shutdown(cmd: str) -> tuple[int, str, str]:
    try:
        # shlex.split raises ValueError on an unbalanced quote and subprocess.run
        # raises IndexError on an empty argv, both before any process exists.
        proc = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=20, check=False
        )
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        return 1, "", f"local transport ({cmd!r}) failed: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _default_serial_shutdown(target: ShutdownTarget) -> tuple[int, str, str]:
    """Send ``cmd`` to a serial console (assumes a passwordless/auto-login getty).

    Network-independent: works during an outage when SSH can't reach the box.

    Success means two things and no more: the LOCAL tty was configured at the declared
    baud, and the bytes were written. The far end is never read back, so a far-end speed
    MISMATCH is **not** detectable here — ``stty -F <dev> <rate>`` returns 0 for 9600,
    19200, 115200 and 0 alike, and the payload write completes at any of them. What the
    captured return code catches is a MALFORMED rate or a line that could not be
    configured locally. Bidirectional readback is deferred as OQ-02.
    """
    try:
        stty = subprocess.run(
            ["stty", "-F", target.device, str(target.baud), "raw", "-echo"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if stty.returncode != 0:
            # check=False keeps this the single decision point; raising
            # CalledProcessError instead would just escape into the handler below.
            return (
                1,
                "",
                f"could not configure the local serial line {target.device} at "
                f"{target.baud} baud (stty rc={stty.returncode}: "
                f"{stty.stderr.strip() or '(no stderr)'}); the shutdown command was "
                f"NOT sent. This says nothing about the far end's line speed.",
            )
        with open(target.device, "wb", buffering=0) as port:
            port.write(b"\r")  # nudge the shell to a fresh prompt
            time.sleep(0.5)
            payload = (target.cmd + "\n").encode()
            written = port.write(payload)
        if written != len(payload):
            # Unbuffered write returned short — the far end likely isn't reading
            # (device unplugged / no getty). Report failure, not false success.
            return 1, "", f"short serial write: {written}/{len(payload)} bytes to {target.device}"
        return 0, "", ""
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        # OSError alone let subprocess.TimeoutExpired escape straight past this.
        return 1, "", f"serial transport to {target.device} failed: {exc}"


def _noop_event_log(
    _event: str,
    _ups: UpsConfig,
    _snap: UpsSnapshot | None,
    _message: str,
    _data: dict[str, object] | None,
) -> None:
    return None


@dataclass
class Deps:
    """Injectable side effects + the one poll knob the handlers need."""

    notifier: Notifier
    read_snapshot: Callable[[str], UpsSnapshot] = read_snapshot
    ssh_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_ssh_shutdown
    local_shutdown: Callable[[str], tuple[int, str, str]] = _default_local_shutdown
    serial_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_serial_shutdown
    now: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))
    countdown_every: int = 60  # seconds between on-battery countdown posts; 0 = off
    # A transfer must persist this many seconds before the poll loop pages ON
    # BATTERY, so grid blips and battery self-tests (both brief) don't alarm.
    onbatt_notify_grace: int = 20
    event_log: EventLogger = _noop_event_log
    load_step: LoadStepPolicy = field(default_factory=LoadStepPolicy)
    sample_path: Path | None = None  # recorder JSONL, for draw-history sparklines
    # Enrolled machines, projected onto ephemeral shutdown targets by
    # ``_machine_targets``. Empty by default so a handler built without config
    # (tests, one-off dispatch) pushes to nothing.
    monitored_machines: tuple[MonitoredMachine, ...] = ()


# --- formatting helpers -------------------------------------------------------


def fmt_duration(seconds: int | None) -> str:
    """Render a span of seconds like ``2h 5m 3s`` (omitting zero leading units)."""
    if seconds is None or seconds < 0:
        return "unknown"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h}h" if h else "", f"{m}m" if (m or h) else "", f"{s}s"]
    return " ".join(p for p in parts if p)


_STATUS_LABELS = {
    "OL": "Online",
    "OB": "On Battery",
    "LB": "Low Battery",
    "CHRG": "Charging",
    "DISCHRG": "Discharging",
    "RB": "Replace Battery",
    "BYPASS": "Bypass",
    "OFF": "Off",
}


def _pretty_status(status: str | None) -> str:
    if not status:
        return "Unknown"
    labelled = [_STATUS_LABELS.get(f, f) for f in status.split()]
    return f"{' · '.join(labelled)}  (`{status}`)"


def charge_bar(pct: int, width: int = 10) -> str:
    """Render a battery charge percentage as a unicode gauge, e.g. ``▰▰▰▰▰▰▰▱▱▱``."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def _snapshot_fields(snap: UpsSnapshot) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [("Status", _pretty_status(snap.status))]
    if snap.charge is not None:
        fields.append(("Battery", f"{charge_bar(snap.charge)} **{snap.charge}%**"))
    if snap.runtime_seconds is not None:
        fields.append(("Expected time before 0%", f"~{fmt_duration(snap.runtime_seconds)}"))
    if snap.load is not None:
        if snap.estimated_load_watts is not None and snap.realpower_nominal is not None:
            fields.append(
                (
                    "Load",
                    (
                        f"{snap.load}% {snap.load_level} "
                        f"(~{snap.estimated_load_watts}/{snap.realpower_nominal} W, "
                        f"{snap.load_margin_percent}% margin)"
                    ),
                )
            )
        else:
            fields.append(("Load", f"{snap.load}% {snap.load_level}"))
    if snap.input_voltage is not None:
        fields.append(("Input voltage", f"{snap.input_voltage:.1f} V"))
    if snap.output_voltage is not None:
        fields.append(("Output voltage", f"{snap.output_voltage:.1f} V"))
    if snap.load_is_high:
        fields.append(("Load warning", "Output load is high; rebalance or move devices."))
    return fields


def _log_event(
    deps: Deps,
    event: str,
    ups: UpsConfig,
    snap: UpsSnapshot | None,
    message: str,
    data: dict[str, object] | None = None,
) -> None:
    try:
        deps.event_log(event, ups, snap, message, data)
    except Exception:  # noqa: BLE001 - logging must never break UPS handling
        LOG.exception("event log failed for %s/%s", ups.name, event)


def _record_status_transition(
    ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot
) -> None:
    if snap.status == state.last_status:
        return
    _log_event(
        deps,
        "status_transition",
        ups,
        snap,
        "UPS status changed",
        {"previous_status": state.last_status, "new_status": snap.status},
    )
    state.last_status = snap.status


# --- NUT-event handlers (Discord notifications) -------------------------------


def handle_onbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    now = deps.now()
    state.onbatt_since = now
    state.shutdowns_sent = []
    state.last_tick_notified = now  # delay first countdown by one cadence
    state.onbatt_notified = True  # this path pages now, so the poll loop won't re-page
    state.last_status = snap.status
    _log_event(deps, "onbatt", ups, snap, "Utility power lost; UPS is on battery.")
    deps.notifier.send(
        Notification(
            title=f"🔋 {ups.label} — ON BATTERY",
            body=(
                "Utility power is out and this UPS is carrying the load. "
                "Shutdown automation remains policy-gated."
            ),
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )


def handle_online(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    outage = None if state.onbatt_since is None else max(0, deps.now() - state.onbatt_since)
    paged = state.onbatt_notified  # did this outage ever page ON BATTERY?
    state.onbatt_since = None
    state.shutdowns_sent = []
    state.last_tick_notified = None
    state.onbatt_notified = False
    state.last_status = snap.status
    fields = _snapshot_fields(snap)
    if outage is not None:
        fields.insert(0, ("Outage duration", fmt_duration(outage)))
    _log_event(
        deps,
        "online",
        ups,
        snap,
        "Utility power restored.",
        {"outage_seconds": outage, "paged": paged},
    )
    # Only announce restoration if we announced the outage — a sub-grace blip or a
    # self-test transfer stays silent on both ends.
    if paged:
        deps.notifier.send(
            Notification(
                title=f"✅ {ups.label} — POWER RESTORED",
                body="Back on utility power. Shutdown state has been reset.",
                level=Level.SUCCESS,
                fields=fields,
            )
        )


def handle_lowbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    _log_event(deps, "lowbatt", ups, snap, "NUT reported low battery.")
    deps.notifier.send(
        Notification(
            title=f"⚠️ {ups.label} — LOW BATTERY",
            body="Battery critical — NUT will shut this host down (backstop).",
            level=Level.CRITICAL,
            fields=_snapshot_fields(snap),
        )
    )


def handle_commbad(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    _log_event(deps, "commbad", ups, None, "Lost contact with UPS.")
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION LOST",
            body="Lost contact with the UPS (USB/driver issue or UPS powered off).",
            level=Level.WARNING,
        )
    )


def handle_commok(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    state.last_status = snap.status
    _log_event(deps, "commok", ups, snap, "Re-established contact with UPS.")
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION RESTORED",
            body="Re-established contact with the UPS.",
            level=Level.SUCCESS,
            fields=_snapshot_fields(snap),
        )
    )


# --- polling-driven shutdown --------------------------------------------------


def _target_group(ups: UpsConfig, target: ShutdownTarget) -> ShutdownGroupPolicy:
    return ups.shutdown_policy.internal if target.is_local else ups.shutdown_policy.external


def _target_location(target: ShutdownTarget) -> str:
    if target.is_local:
        return "this host (local)"
    if target.is_serial:
        return f"serial {target.device}"
    return f"{ssh_dest(target)} (ssh)"


def _outage_age(state: UpsState, deps: Deps) -> int | None:
    if state.onbatt_since is None:
        return None
    return max(0, deps.now() - state.onbatt_since)


def _threshold_status(
    label: str, value: int | None, threshold: int | None, render: Callable[[int], str]
) -> tuple[bool | None, str]:
    if threshold is None:
        return None, f"{label} threshold disabled"
    if value is None:
        return None, f"{label} unknown"
    due = value <= threshold
    return due, f"{label} {render(value)} <= {render(threshold)}"


def _close_to_empty(group: ShutdownGroupPolicy, snap: UpsSnapshot) -> tuple[bool, str]:
    battery_due, battery_reason = _threshold_status(
        "battery", snap.charge, group.battery_below, lambda pct: f"{pct}%"
    )
    runtime_due, runtime_reason = _threshold_status(
        "runtime", snap.runtime_seconds, group.runtime_below, fmt_duration
    )
    known = [result for result in (battery_due, runtime_due) if result is not None]
    reasons = [battery_reason, runtime_reason]
    if not known:
        return False, "; ".join(reasons)
    if battery_due is not None and runtime_due is not None:
        return battery_due and runtime_due, "; ".join(reasons)
    return bool(known[0]), "; ".join(reasons)


def _target_should_fire(
    ups: UpsConfig, state: UpsState, deps: Deps, target: ShutdownTarget, snap: UpsSnapshot
) -> tuple[bool, str]:
    """Return whether the target may fire and why."""
    policy = ups.shutdown_policy
    if not policy.enabled:
        return False, "shutdown policy disabled"

    group = _target_group(ups, target)
    group_name = "internal" if target.is_local else "external"
    if not group.enabled:
        return False, f"{group_name} shutdown group disabled"

    if policy.require_power_outage:
        if not snap.on_battery:
            return False, "UPS is not on battery"
        age = _outage_age(state, deps)
        if age is None:
            return False, "on-battery start was not recorded yet"
        if age < policy.min_on_battery_seconds:
            return (
                False,
                "on-battery time "
                f"{fmt_duration(age)} < {fmt_duration(policy.min_on_battery_seconds)}",
            )

    close, reason = _close_to_empty(group, snap)
    if not close:
        return False, f"UPS is not close to empty ({reason})"
    return True, f"{group_name} shutdown allowed ({reason})"


def _notify_shutdown_attempt(
    ups: UpsConfig,
    deps: Deps,
    target: ShutdownTarget,
    snap: UpsSnapshot,
    where: str,
    reason: str,
) -> None:
    if not ups.shutdown_policy.notify:
        return
    fields = _snapshot_fields(snap)
    fields.insert(0, ("Target", f"{target.name} via {where}"))
    fields.insert(1, ("Trigger", reason))
    deps.notifier.send(
        Notification(
            title=f"🛑 {ups.label} — shutdown attempt for {target.name}",
            body="The orchestrator is issuing a configured shutdown command.",
            level=Level.CRITICAL,
            fields=fields,
        )
    )


def _notify_shutdown_result(
    ups: UpsConfig, deps: Deps, target: ShutdownTarget, rc: int, err: str, where: str
) -> None:
    if not ups.shutdown_policy.notify:
        return
    if rc == 0:
        deps.notifier.send(
            Notification(
                title=f"🛑 {ups.label} — shutdown sent to {target.name}",
                body=f"Graceful shutdown issued to {where}.",
                level=Level.CRITICAL,
            )
        )
    else:
        deps.notifier.send(
            Notification(
                title=f"❗ {ups.label} — shutdown FAILED for {target.name}",
                body=f"rc={rc}; stderr={err or '(none)'}",
                level=Level.CRITICAL,
            )
        )


def _fire_target(
    ups: UpsConfig,
    state: UpsState,
    deps: Deps,
    target: ShutdownTarget,
    snap: UpsSnapshot,
    reason: str,
) -> None:
    where = _target_location(target)
    _log_event(
        deps,
        "shutdown_attempt",
        ups,
        snap,
        "Issuing configured shutdown target command.",
        {"target": target.name, "where": where, "reason": reason},
    )
    _notify_shutdown_attempt(ups, deps, target, snap, where, reason)
    # Backstop for the runner contract, and NOT redundant with the defaults' own
    # handlers: ``Deps`` carries injected runners (tests, any future transport) that
    # those handlers do not cover. Only the call site can guarantee the invariant that
    # matters — ``shutdowns_sent`` is always appended, so the local targets below are
    # always reached even when every remote blows up on a dead switch (T-02-24).
    try:
        if target.is_local:
            rc, _out, err = deps.local_shutdown(target.cmd)
        elif target.is_serial:
            rc, _out, err = deps.serial_shutdown(target)
        else:
            rc, _out, err = deps.ssh_shutdown(target)
    except Exception as exc:  # noqa: BLE001 - an escaping runner must not strand the rest
        rc, err = 1, f"shutdown transport for {target.name} ({where}) raised: {exc}"
    state.shutdowns_sent.append(target.name)
    _log_event(
        deps,
        "shutdown_result",
        ups,
        snap,
        "Configured shutdown target command completed.",
        {"target": target.name, "where": where, "returncode": rc, "stderr": err},
    )
    _notify_shutdown_result(ups, deps, target, rc, err, where)


def _machine_targets(
    ups: UpsConfig, machines: Sequence[MonitoredMachine]
) -> Iterator[ShutdownTarget]:
    """Project this UPS's push-managed machines onto ephemeral shutdown targets.

    A machine's ``shutdown_method`` selects the *transport*; the existing
    ``ShutdownPolicy`` gate still decides *whether and when*. The projected targets
    are handed to the unchanged firing path, so no new shutdown or transport logic
    exists anywhere.

    ``native`` machines are deliberately never projected: their ``upsmon`` secondary
    powers itself off on the primary's FSD, so a push as well would shut the box
    down twice (P2-01/P2-06). ``none`` machines opted out of shutdown entirely.

    ``serial_baud`` is carried verbatim. ``_default_serial_shutdown`` runs
    ``stty -F <device> <baud>``, so a substituted baud writes garbage down the line
    and still returns rc=0 — a silent no-shutdown (P2-08).

    The projected target is named after the machine, which is the
    ``state.shutdowns_sent`` dedupe key. A name already claimed on this UPS would
    make the later target a no-op with no trace, so a collision is logged as an
    error and dropped instead. ``Config.load`` already rejects a machine that
    collides with an *enabled* legacy target, so in a loaded config this can only
    trip on two machines sharing a name.
    """
    ups_name = normalize_ups_name(ups.name)
    seen = {t.name.strip().lower() for t in ups.shutdown_targets if t.enabled}
    for m in machines:
        if normalize_ups_name(m.ups) != ups_name:
            continue
        method = m.shutdown_method.strip().lower()
        if method == "ssh":
            target = ShutdownTarget(
                name=m.name, kind="remote", enabled=True, host=m.ssh, cmd=m.shutdown_cmd
            )
        elif method == "serial":
            target = ShutdownTarget(
                name=m.name,
                kind="serial",
                enabled=True,
                device=m.serial_device,
                baud=m.serial_baud,
                cmd=m.shutdown_cmd,
            )
        else:
            continue
        key = m.name.strip().lower()
        if key in seen:
            LOG.error(
                "Machine %r on UPS %s has a duplicate shutdown target name; skipping its "
                "push — rename it or that machine will never be shut down",
                m.name,
                ups.name,
            )
            continue
        seen.add(key)
        yield target


def _run_shutdown_targets(ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot) -> None:
    """Fire due targets — remotes first, locals only once all remotes are sent."""
    projected = _machine_targets(ups, deps.monitored_machines)
    enabled = [t for t in (*ups.shutdown_targets, *projected) if t.enabled]
    # Serial is network-independent; ssh dies with the switch. Fire serial first so a
    # collapsing network can't strand a shutdown. The sort is stable, so declared
    # order still holds within each transport.
    remotes = sorted((t for t in enabled if not t.is_local), key=lambda t: not t.is_serial)
    locals_ = [t for t in enabled if t.is_local]

    for t in remotes:
        should_fire, reason = _target_should_fire(ups, state, deps, t, snap)
        if t.name not in state.shutdowns_sent and should_fire:
            _fire_target(ups, state, deps, t, snap, reason)
        elif t.name not in state.shutdowns_sent:
            _log_event(
                deps,
                "shutdown_target_blocked",
                ups,
                snap,
                "Configured remote/serial target did not meet shutdown gate.",
                {"target": t.name, "reason": reason},
            )

    # Local hosts die last: hold until every enabled remote has been triggered.
    active_remotes = [t for t in remotes if _target_group(ups, t).enabled]
    if any(t.name not in state.shutdowns_sent for t in active_remotes):
        return
    for t in locals_:
        should_fire, reason = _target_should_fire(ups, state, deps, t, snap)
        if t.name not in state.shutdowns_sent and should_fire:
            _fire_target(ups, state, deps, t, snap, reason)
        elif t.name not in state.shutdowns_sent:
            _log_event(
                deps,
                "shutdown_target_blocked",
                ups,
                snap,
                "Configured local target did not meet shutdown gate.",
                {"target": t.name, "reason": reason},
            )


def _draw_sparkline(sample_path: Path, ups_name: str, *, minutes: int = 10) -> str:
    """Render recent outlet watts from the recorder log as a Unicode sparkline."""
    try:
        with sample_path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return ""
    watts: list[tuple[float, int]] = []
    for line in tail:
        try:
            d = json.loads(line)
            t = d["unix_time"]
            w = d["upses"][ups_name]["estimated_load_watts"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(t, (int, float)) and isinstance(w, (int, float)):
            watts.append((float(t), int(w)))
    if len(watts) < 2:
        return ""
    cutoff = watts[-1][0] - minutes * 60
    pts = [w for t, w in watts if t >= cutoff]
    if len(pts) < 2:
        return ""
    cols = 24
    step = max(1, len(pts) // cols)
    buckets = [max(pts[i : i + step]) for i in range(0, len(pts), step)][:cols]
    lo, hi = min(buckets), max(buckets)
    blocks = "▁▂▃▄▅▆▇█"
    if hi == lo:
        bar = blocks[0] * len(buckets)
    else:
        bar = "".join(blocks[(w - lo) * 7 // (hi - lo)] for w in buckets)
    return f"`{bar}` {lo}–{hi} W, last {minutes} min"


def _check_load_step(ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot) -> None:
    """Flag an output-load collapse (a downstream device dying).

    The drop is measured against the highest load in the last ``window_polls``
    polls — not just the previous poll — so a collapse that straddles a poll
    (the UPS reporting an intermediate value mid-decay) still trips. Tracking
    always runs so enabling the policy later starts with a baseline. The event
    is always logged when the threshold trips; the notification is rate-limited
    by the policy cooldown.
    """
    load = snap.load
    if load is None:
        return
    policy = ups.load_step if ups.load_step is not None else deps.load_step
    window = list(state.recent_loads)
    state.recent_loads = (window + [load])[-max(1, policy.window_polls) :]
    if not policy.enabled or not window:
        return
    peak = max(window)
    drop = peak - load
    if drop < policy.drop_percent:
        return
    # Restart the window at the collapsed level so the stale peak can't
    # re-trigger on every poll until it ages out.
    state.recent_loads = [load]
    watts = (drop * snap.realpower_nominal) // 100 if snap.realpower_nominal else None
    _log_event(
        deps,
        "load_step_drop",
        ups,
        snap,
        f"Output load fell {drop} points from its recent peak ({peak}% -> {load}%).",
        {
            "peak_load": peak,
            "new_load": load,
            "drop_points": drop,
            "estimated_watts_delta": watts,
            "window": window,
        },
    )
    now = deps.now()
    if (
        state.last_load_step_notified is not None
        and now - state.last_load_step_notified < policy.cooldown_seconds
    ):
        return
    state.last_load_step_notified = now
    watts_note = f" (≈{watts} W)" if watts is not None else ""
    sparkline = _draw_sparkline(deps.sample_path, ups.name) if deps.sample_path is not None else ""
    body = (
        f"Output load fell to {load}% from a recent high of {peak}%{watts_note}. "
        "A device on this UPS may have lost power — or just finished heavy "
        "work. Worth a reachability check."
    )
    if sparkline:
        body += f"\n\n{sparkline}"
    deps.notifier.send(
        Notification(
            title=f"📉 {ups.label} — load dropped {drop} points",
            body=body,
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )


def handle_tick(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """One poll iteration (driven by the ``watch`` loop).

    Tracks load steps every call; otherwise a no-op unless the UPS is on
    battery. Evaluates shutdown targets every call; posts a runtime countdown
    only every ``countdown_every`` seconds.
    """
    snap = deps.read_snapshot(ups.name)
    _record_status_transition(ups, state, deps, snap)
    _check_load_step(ups, state, deps, snap)
    if not snap.on_battery:
        if state.onbatt_since is not None:
            _log_event(
                deps,
                "online_detected_by_poll",
                ups,
                snap,
                "Poll loop detected utility power restored before/without online callback.",
            )
            handle_online(ups, state, deps)
        return

    now = deps.now()
    if state.onbatt_since is None:
        state.onbatt_since = now
        state.shutdowns_sent = []
        state.last_tick_notified = now
        _log_event(
            deps,
            "onbatt_detected_by_poll",
            ups,
            snap,
            "Poll loop detected on-battery state before/without onbatt callback.",
        )

    # Defer the page until the transfer has persisted past the grace window, so a
    # brief blip or a battery self-test (which also transfers to battery) that
    # clears within the window never alarms. Once sent, don't re-page this outage.
    if not state.onbatt_notified and (now - state.onbatt_since) >= deps.onbatt_notify_grace:
        state.onbatt_notified = True
        deps.notifier.send(
            Notification(
                title=f"🔋 {ups.label} — ON BATTERY",
                body=(
                    "Poll loop confirms the UPS on battery beyond the notify grace. "
                    "This covers cases where the NUT event callback is missed."
                ),
                level=Level.WARNING,
                fields=_snapshot_fields(snap),
            )
        )

    _run_shutdown_targets(ups, state, deps, snap)

    if (
        state.onbatt_notified
        and deps.countdown_every > 0
        and (
            state.last_tick_notified is None
            or (now - state.last_tick_notified) >= deps.countdown_every
        )
    ):
        state.last_tick_notified = now
        _log_event(
            deps,
            "onbatt_countdown",
            ups,
            snap,
            "UPS remains on battery; sending countdown notification.",
        )
        deps.notifier.send(
            Notification(
                title=f"⏳ {ups.label} — still on battery",
                body=f"Estimated runtime remaining: ~{fmt_duration(snap.runtime_seconds)}.",
                level=Level.WARNING,
                fields=_snapshot_fields(snap),
            )
        )


def handle_remote_shutdown(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """Explicit trigger: evaluate configured targets now (remotes first, local last)."""
    snap = deps.read_snapshot(ups.name)
    _log_event(deps, "remote_shutdown", ups, snap, "Explicit remote shutdown trigger received.")
    if not ups.shutdown_policy.enabled:
        _log_event(
            deps,
            "remote_shutdown_skipped",
            ups,
            snap,
            "Triggered, but orchestrator-managed shutdowns are disabled.",
        )
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — shutdown skipped",
                body="Triggered, but orchestrator-managed shutdowns are disabled.",
                level=Level.INFO,
            )
        )
        return
    if ups.shutdown_policy.require_power_outage and not snap.on_battery:
        _log_event(
            deps,
            "remote_shutdown_skipped",
            ups,
            snap,
            "Triggered, but UPS is not on battery.",
        )
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — shutdown skipped",
                body="Triggered, but the UPS is no longer on battery.",
                level=Level.INFO,
            )
        )
        return
    _run_shutdown_targets(ups, state, deps, snap)


_HANDLERS: dict[str, Callable[[UpsConfig, UpsState, Deps], None]] = {
    "onbatt": handle_onbatt,
    "online": handle_online,
    "lowbatt": handle_lowbatt,
    "commbad": handle_commbad,
    "commok": handle_commok,
    "tick": handle_tick,
    "remote_shutdown": handle_remote_shutdown,
}


def dispatch(event: str, ups: UpsConfig, state: UpsState, deps: Deps) -> bool:
    """Run the handler for ``event``. Returns False if the event is unknown."""
    handler = _HANDLERS.get(event.lower())
    if handler is None:
        LOG.warning("Unknown event %r for UPS %s", event, ups.name)
        return False
    handler(ups, state, deps)
    return True
