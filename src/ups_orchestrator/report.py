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


def _alarm_active(snap: UpsSnapshot) -> bool:
    return snap.alarm is not None and snap.alarm.strip().lower() not in ("", "none")


def _selftest_failed(snap: UpsSnapshot) -> bool:
    return snap.test_result is not None and "fail" in snap.test_result.lower()


def _is_degraded(snap: UpsSnapshot) -> bool:
    if snap.status is None or snap.low_battery or snap.on_battery:
        return True
    if _alarm_active(snap) or _selftest_failed(snap):
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
    headroom = snap.load_headroom_watts
    headroom_text = "" if headroom is None else f", {headroom} W free"
    return (
        f"{snap.load}% {snap.load_level} "
        f"(~{watts} W / {snap.realpower_nominal} W{headroom_text}, {margin_text})"
    )


def _voltage_text(snap: UpsSnapshot) -> str:
    if snap.input_voltage is None:
        inp = "unknown"
    else:
        nom = (
            "" if snap.input_voltage_nominal is None else f", nom {snap.input_voltage_nominal:.0f}"
        )
        inp = f"{snap.input_voltage:.1f} V in{nom}"
    out = "unknown" if snap.output_voltage is None else f"{snap.output_voltage:.1f} V out"
    return f"{inp} / {out}"


def _battery_health_text(snap: UpsSnapshot) -> str | None:
    if snap.battery_voltage is None:
        return None
    parts = [f"{snap.battery_voltage:.1f} V"]
    pct = snap.battery_voltage_percent
    if pct is not None:
        parts.append(f"{pct}% of {snap.battery_voltage_nominal:.0f} V nominal")
    if snap.battery_type:
        parts.append(snap.battery_type)
    return " · ".join(parts)


def _field_value(snap: UpsSnapshot) -> str:
    est_to_empty = "unknown" if snap.runtime_seconds is None else fmt_duration(snap.runtime_seconds)
    lines = [
        f"Status: {_status_text(snap)}",
        f"Battery: {_battery_text(snap)}",
        f"Expected time before 0%: {est_to_empty}",
        f"Load: {_load_text(snap)}",
        f"Voltage: {_voltage_text(snap)}",
    ]
    health = _battery_health_text(snap)
    if health is not None:
        lines.append(f"Battery health: {health}")
    if snap.test_result and snap.test_result.strip().lower() not in ("no test initiated", ""):
        lines.append(f"Last self-test: {snap.test_result}")
    if _alarm_active(snap):
        lines.append(f"⚠️ Alarm: {snap.alarm}")
    if _selftest_failed(snap):
        lines.append("Action: battery self-test FAILED — inspect/replace battery")
    if snap.load_is_high:
        lines.append("Action: rebalance load or move devices to another UPS")
    if snap.status and "OL" in snap.status.split() and "DISCHRG" in snap.status.split():
        lines.append("Action: investigate CyberPower online-discharge state")
    if snap.status is None:
        lines.append("Action: check USB/NUT driver communication")
    return "\n".join(lines)


def _inventory_text(cfg: Config, name: str) -> str | None:
    """Name what actually dies with this UPS, from config rather than memory."""
    managed, devices = cfg.ups_inventory(name)
    lines = []
    if managed:
        lines.append(f"Shuts down on battery: {', '.join(managed)}")
    if devices:
        lines.append(f"Also powers: {', '.join(device.name for device in devices)}")
    return "\n".join(lines) if lines else None


def _summary_body(cfg: Config, snaps: dict[str, UpsSnapshot]) -> str:
    """One line that changes day to day, instead of boilerplate that never does.

    The operator's complaint about the old report was exact: the embed looked
    good and said nothing, because the body was a fixed sentence and every real
    number was buried in a field. Lead with total draw and whatever is wrong.
    """
    no_comm = [name for name, snap in snaps.items() if snap.status is None]
    if no_comm and len(no_comm) == len(snaps):
        return "❌ No communication with any UPS — NUT is not answering. Nothing below is live."

    watts = [snap.estimated_load_watts for snap in snaps.values()]
    known = [w for w in watts if w is not None]
    parts = []
    if known:
        parts.append(f"Drawing ~{sum(known)} W across {len(known)} UPS(es).")
    protected = sum(len(cfg.ups_inventory(name)[0]) for name in snaps)
    if protected:
        parts.append(f"{protected} machine(s) set to shut down automatically.")
    if no_comm:
        parts.append(f"❌ No communication with: {', '.join(no_comm)}.")

    on_battery = [name for name, snap in snaps.items() if snap.on_battery]
    if on_battery:
        parts.append(f"⚠️ ON BATTERY: {', '.join(on_battery)}.")
    high = [name for name, snap in snaps.items() if snap.load_is_high]
    if high:
        parts.append(f"⚠️ High load: {', '.join(high)}.")
    return " ".join(parts)


def build_report(
    cfg: Config,
    *,
    snapshot_reader: Callable[[str], UpsSnapshot] = read_snapshot,
) -> Notification:
    """Build a Discord-ready status report for every configured UPS."""
    fields: list[tuple[str, str]] = []
    snaps: dict[str, UpsSnapshot] = {}
    degraded = False
    for name, ups in cfg.upses.items():
        snap = snapshot_reader(name)
        snaps[name] = snap
        degraded = degraded or _is_degraded(snap)
        value = _field_value(snap)
        inventory = _inventory_text(cfg, name)
        fields.append((ups.label, f"{value}\n{inventory}" if inventory else value))

    return Notification(
        title="📊 UPS load and runtime report",
        body=_summary_body(cfg, snaps),
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
