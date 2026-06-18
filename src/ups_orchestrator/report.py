"""Daily UPS load/status report notifications."""

from __future__ import annotations

from collections.abc import Callable

from ups_orchestrator.config import Config
from ups_orchestrator.events import charge_bar, fmt_duration
from ups_orchestrator.notify import Level, Notification
from ups_orchestrator.nut import UpsSnapshot, read_snapshot


def _status_text(snap: UpsSnapshot) -> str:
    if snap.status is None:
        return "NO COMM"
    flags = set(snap.status.split())
    if "LB" in flags:
        return f"LOW BATTERY (`{snap.status}`)"
    if "OB" in flags:
        return f"ON BATTERY (`{snap.status}`)"
    if "OL" in flags and "DISCHRG" in flags:
        return f"ONLINE + DISCHARGING (`{snap.status}`)"
    if "OL" in flags:
        return f"Online (`{snap.status}`)"
    return f"`{snap.status}`"


def _is_degraded(snap: UpsSnapshot) -> bool:
    if snap.status is None or snap.low_battery or snap.on_battery:
        return True
    flags = set(snap.status.split())
    return ("OL" in flags and "DISCHRG" in flags) or snap.load_is_high


def _battery_text(snap: UpsSnapshot) -> str:
    if snap.charge is None:
        return "unknown"
    return f"{charge_bar(snap.charge)} {snap.charge}%"


def _load_text(snap: UpsSnapshot) -> str:
    if snap.load is None:
        return "unknown"
    watts = snap.estimated_load_watts
    margin = snap.load_margin_percent
    margin_text = "unknown margin" if margin is None else f"{margin}% margin"
    if watts is None or snap.realpower_nominal is None:
        return f"{snap.load}% {snap.load_level} ({margin_text})"
    return (
        f"{snap.load}% {snap.load_level} (~{watts} W / {snap.realpower_nominal} W, {margin_text})"
    )


def _voltage_text(snap: UpsSnapshot) -> str:
    inp = "unknown" if snap.input_voltage is None else f"{snap.input_voltage:.1f} V in"
    out = "unknown" if snap.output_voltage is None else f"{snap.output_voltage:.1f} V out"
    return f"{inp} / {out}"


def _field_value(snap: UpsSnapshot) -> str:
    est_to_empty = "unknown" if snap.runtime_seconds is None else fmt_duration(snap.runtime_seconds)
    lines = [
        f"Status: {_status_text(snap)}",
        f"Battery: {_battery_text(snap)}",
        f"Expected time before 0%: {est_to_empty}",
        f"Load: {_load_text(snap)}",
        f"Voltage: {_voltage_text(snap)}",
    ]
    if snap.load_is_high:
        lines.append("Action: rebalance load or move devices to another UPS")
    if snap.status and "OL" in snap.status.split() and "DISCHRG" in snap.status.split():
        lines.append("Action: investigate CyberPower online-discharge state")
    if snap.status is None:
        lines.append("Action: check USB/NUT driver communication")
    return "\n".join(lines)


def build_report(
    cfg: Config,
    *,
    snapshot_reader: Callable[[str], UpsSnapshot] = read_snapshot,
) -> Notification:
    """Build a Discord-ready status report for every configured UPS."""
    fields: list[tuple[str, str]] = []
    degraded = False
    for name, ups in cfg.upses.items():
        snap = snapshot_reader(name)
        degraded = degraded or _is_degraded(snap)
        fields.append((ups.label, _field_value(snap)))

    return Notification(
        title="📊 UPS load and runtime report",
        body=(
            "Current battery, expected time before 0%, load, voltage, and action flags "
            "for configured UPSes."
        ),
        level=Level.WARNING if degraded else Level.INFO,
        fields=fields,
        footer=f"{len(fields)} UPS(es) configured",
    )


def render_text(note: Notification) -> str:
    """Render a report notification for terminal dry-runs."""
    lines = [note.title, note.body]
    for name, value in note.fields:
        lines.append("")
        lines.append(name)
        lines.append(value)
    return "\n".join(lines)
