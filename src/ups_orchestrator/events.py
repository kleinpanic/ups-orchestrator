"""Event handlers — the orchestrator's brain.

Two decoupled concerns share these handlers:

* **NUT-event webhooks** — ``onbatt``/``online``/``lowbatt``/``commbad``/``commok``
  fire from NUT's ``upssched`` and post per-UPS Discord embeds. NUT's own
  ``upsmon`` ``SHUTDOWNCMD`` remains the backstop that powers off this host.
* **Polling-driven shutdown** — the ``tick`` handler (run repeatedly by the
  ``watch`` loop at a configurable interval) reads battery state and shuts down
  configured ``shutdown_targets`` when their charge/runtime threshold is crossed.
  ``local`` targets are always sequenced **after** every enabled ``remote``
  target on the same UPS, so the watcher host dies last. The on-battery
  countdown post has its own cadence and never gates shutdown decisions.

Side effects (snapshot reads, shutdowns, clock) are injected via :class:`Deps`
so the handlers unit-test without a real UPS or network.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from ups_orchestrator.config import ShutdownTarget, UpsConfig
from ups_orchestrator.notify import Level, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import UpsState

LOG = logging.getLogger("ups_orchestrator.events")


# --- default side effects (overridable in tests) -----------------------------


def _default_ssh_shutdown(target: ShutdownTarget) -> tuple[int, str, str]:
    cmd = [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        f"{target.user}@{target.host}",
        target.cmd,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _default_local_shutdown(cmd: str) -> tuple[int, str, str]:
    proc = subprocess.run(shlex.split(cmd), capture_output=True, text=True, timeout=20, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


@dataclass
class Deps:
    """Injectable side effects + the one poll knob the handlers need."""

    notifier: Notifier
    read_snapshot: Callable[[str], UpsSnapshot] = read_snapshot
    ssh_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_ssh_shutdown
    local_shutdown: Callable[[str], tuple[int, str, str]] = _default_local_shutdown
    now: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))
    countdown_every: int = 60  # seconds between on-battery countdown posts; 0 = off


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
        fields.append(("Runtime left", f"~{fmt_duration(snap.runtime_seconds)}"))
    if snap.load is not None:
        fields.append(("Load", f"{snap.load}%"))
    if snap.input_voltage is not None:
        fields.append(("Input", f"{snap.input_voltage:.1f} V"))
    return fields


# --- NUT-event handlers (Discord notifications) -------------------------------


def handle_onbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    now = deps.now()
    state.onbatt_since = now
    state.shutdowns_sent = []
    state.last_tick_notified = now  # delay first countdown by one cadence
    deps.notifier.send(
        Notification(
            title=f"🔋 {ups.label} — ON BATTERY",
            body="Utility power lost; running on battery.",
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )


def handle_online(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    outage = None if state.onbatt_since is None else max(0, deps.now() - state.onbatt_since)
    state.onbatt_since = None
    state.shutdowns_sent = []
    state.last_tick_notified = None
    fields = _snapshot_fields(snap)
    if outage is not None:
        fields.insert(0, ("Outage duration", fmt_duration(outage)))
    deps.notifier.send(
        Notification(
            title=f"✅ {ups.label} — POWER RESTORED",
            body="Back on utility power.",
            level=Level.SUCCESS,
            fields=fields,
        )
    )


def handle_lowbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    deps.notifier.send(
        Notification(
            title=f"⚠️ {ups.label} — LOW BATTERY",
            body="Battery critical — NUT will shut this host down (backstop).",
            level=Level.CRITICAL,
            fields=_snapshot_fields(snap),
        )
    )


def handle_commbad(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION LOST",
            body="Lost contact with the UPS (USB/driver issue or UPS powered off).",
            level=Level.WARNING,
        )
    )


def handle_commok(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION RESTORED",
            body="Re-established contact with the UPS.",
            level=Level.SUCCESS,
            fields=_snapshot_fields(snap),
        )
    )


# --- polling-driven shutdown --------------------------------------------------


def _target_should_fire(target: ShutdownTarget, snap: UpsSnapshot) -> bool:
    """True if either the charge-% or runtime threshold is met."""
    if (
        target.battery_below is not None
        and snap.charge is not None
        and snap.charge <= target.battery_below
    ):
        return True
    return (
        target.runtime_below is not None
        and snap.runtime_seconds is not None
        and snap.runtime_seconds <= target.runtime_below
    )


def _fire_target(ups: UpsConfig, state: UpsState, deps: Deps, target: ShutdownTarget) -> None:
    if target.is_local:
        rc, _out, err = deps.local_shutdown(target.cmd)
        where = "this host"
    else:
        rc, _out, err = deps.ssh_shutdown(target)
        where = f"{target.user}@{target.host}"
    state.shutdowns_sent.append(target.name)
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


def _run_shutdown_targets(
    ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot, *, force: bool = False
) -> None:
    """Fire due targets — remotes first, locals only once all remotes are sent."""
    enabled = [t for t in ups.shutdown_targets if t.enabled]
    remotes = [t for t in enabled if not t.is_local]
    locals_ = [t for t in enabled if t.is_local]

    for t in remotes:
        if t.name not in state.shutdowns_sent and (force or _target_should_fire(t, snap)):
            _fire_target(ups, state, deps, t)

    # Local hosts die last: hold until every enabled remote has been triggered.
    if any(t.name not in state.shutdowns_sent for t in remotes):
        return
    for t in locals_:
        if t.name not in state.shutdowns_sent and (force or _target_should_fire(t, snap)):
            _fire_target(ups, state, deps, t)


def handle_tick(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """One poll iteration (driven by the ``watch`` loop).

    No-op unless the UPS is on battery. Evaluates shutdown targets every call;
    posts a runtime countdown only every ``countdown_every`` seconds.
    """
    snap = deps.read_snapshot(ups.name)
    if not snap.on_battery:
        return

    _run_shutdown_targets(ups, state, deps, snap)

    now = deps.now()
    if deps.countdown_every > 0 and (
        state.last_tick_notified is None or (now - state.last_tick_notified) >= deps.countdown_every
    ):
        state.last_tick_notified = now
        deps.notifier.send(
            Notification(
                title=f"⏳ {ups.label} — still on battery",
                body=f"Estimated runtime remaining: ~{fmt_duration(snap.runtime_seconds)}.",
                level=Level.WARNING,
                fields=_snapshot_fields(snap),
            )
        )


def handle_remote_shutdown(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """Explicit trigger: force all enabled targets now (remotes first, local last)."""
    snap = deps.read_snapshot(ups.name)
    if not snap.on_battery:
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — forced shutdown skipped",
                body="Triggered, but the UPS is no longer on battery.",
                level=Level.INFO,
            )
        )
        return
    _run_shutdown_targets(ups, state, deps, snap, force=True)


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
