"""Event handlers — the orchestrator's brain.

Each NUT power event maps to a handler that (a) reads a UPS snapshot, (b) sends a
per-UPS labelled Discord notification, and (c) updates persisted state. Side
effects (snapshot reads, shutdowns, clock) are injected via :class:`Deps` so the
handlers are pure enough to unit-test without a real UPS or network.

Hybrid model: the **actual** protective shutdown of *this* host is left to NUT's
``upsmon`` ``SHUTDOWNCMD`` by default (``shutdown_pi_on_lowbatt`` is off). The
orchestrator only announces it. Optionally, a UPS may define one or more
``shutdown_targets`` — remote machines it powers — which are gracefully shut
down over SSH after a per-target grace period on battery (all disabled by
default).
"""

from __future__ import annotations

import logging
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


def _default_shutdown_pi() -> None:
    subprocess.run(["/usr/bin/systemctl", "poweroff"], timeout=10, check=False)


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


@dataclass
class Deps:
    """Injectable side effects. Defaults wire to the real system."""

    notifier: Notifier
    read_snapshot: Callable[[str], UpsSnapshot] = read_snapshot
    shutdown_pi: Callable[[], None] = _default_shutdown_pi
    ssh_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_ssh_shutdown
    now: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))


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
    flags = status.split()
    labelled = [f"{_STATUS_LABELS.get(f, f)}" for f in flags]
    return f"{' · '.join(labelled)}  (`{status}`)"


def charge_bar(pct: int, width: int = 10) -> str:
    """Render a battery charge percentage as a unicode gauge, e.g. ``▰▰▰▰▰▰▰▱▱▱``."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def _snapshot_fields(snap: UpsSnapshot) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = []
    fields.append(("Status", _pretty_status(snap.status)))
    if snap.charge is not None:
        fields.append(("Battery", f"{charge_bar(snap.charge)} **{snap.charge}%**"))
    if snap.runtime_seconds is not None:
        fields.append(("Runtime left", f"~{fmt_duration(snap.runtime_seconds)}"))
    if snap.load is not None:
        fields.append(("Load", f"{snap.load}%"))
    if snap.input_voltage is not None:
        fields.append(("Input", f"{snap.input_voltage:.1f} V"))
    return fields


# --- handlers -----------------------------------------------------------------


def handle_onbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    now = deps.now()
    state.onbatt_since = now
    state.shutdowns_sent = []
    state.last_tick_notified = now
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
    will_self_shutdown = ups.shutdown_pi_on_lowbatt
    body = (
        "Battery critical — orchestrator is powering off this host now."
        if will_self_shutdown
        else "Battery critical — NUT will shut this host down."
    )
    deps.notifier.send(
        Notification(
            title=f"⚠️ {ups.label} — LOW BATTERY",
            body=body,
            level=Level.CRITICAL,
            fields=_snapshot_fields(snap),
        )
    )
    if will_self_shutdown:
        deps.shutdown_pi()


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


def handle_tick(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """Periodic on-battery check (driven by a systemd timer).

    While on battery, posts a runtime-remaining countdown and triggers any
    configured remote ``shutdown_targets`` whose grace period has elapsed.
    """
    snap = deps.read_snapshot(ups.name)
    if not snap.on_battery:
        return

    now = deps.now()
    state.last_tick_notified = now
    deps.notifier.send(
        Notification(
            title=f"⏳ {ups.label} — still on battery",
            body=f"Estimated runtime remaining: ~{fmt_duration(snap.runtime_seconds)}.",
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )
    _run_shutdown_targets(ups, state, deps, now)


def handle_remote_shutdown(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """Explicit trigger: force all enabled shutdown_targets now (e.g. NUT timer)."""
    snap = deps.read_snapshot(ups.name)
    if not snap.on_battery:
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — remote shutdown skipped",
                body="Triggered, but the UPS is no longer on battery.",
                level=Level.INFO,
            )
        )
        return
    _run_shutdown_targets(ups, state, deps, deps.now(), force=True)


def _run_shutdown_targets(
    ups: UpsConfig, state: UpsState, deps: Deps, now: int, *, force: bool = False
) -> None:
    """Shut down each enabled target whose grace period has elapsed (once)."""
    elapsed = None if state.onbatt_since is None else now - state.onbatt_since
    for target in ups.shutdown_targets:
        if not target.enabled or target.name in state.shutdowns_sent:
            continue
        if not force and (elapsed is None or elapsed < target.delay_seconds):
            continue

        rc, _out, err = deps.ssh_shutdown(target)
        state.shutdowns_sent.append(target.name)
        if rc == 0:
            deps.notifier.send(
                Notification(
                    title=f"🛑 {ups.label} — shutdown sent to {target.name}",
                    body=f"Graceful shutdown issued to {target.user}@{target.host}.",
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
