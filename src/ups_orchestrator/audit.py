"""UPS incident audit report built from current NUT state and journald."""

from __future__ import annotations

import json
import logging
import math
import os
import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from ups_orchestrator.config import Config
from ups_orchestrator.events import fmt_duration
from ups_orchestrator.notify import Level, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import write_json_preserving_metadata

_POWER_LOSS_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"corrupted or uncleanly shut down",
        r"\bDirty bit is set\b",
        r"\brecovering journal\b",
        r"not properly unmounted",
        r"some data may be corrupt",
    )
)
# `orphan cleanup on readonly fs` used to live in the list above and must not go
# back. ext4 logs it during the initial read-only root mount whenever the previous
# session had deleted-but-still-open inodes, which is ordinary on a CLEAN
# shutdown. It fired on every boot of this host and was a pure false positive.

_CLEAN_SHUTDOWN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"systemd-shutdown\[1\]: Syncing filesystems and block devices",
        r"systemd-shutdown\[1\]: (?:Powering off|Rebooting|Halting)",
        r"systemd\[1\]: Reached target (?:System Power Off|Power-Off|Reboot|Halt)",
    )
)

# Filesystem-recovery evidence is written while mounting, in the opening seconds of
# a boot. Anything later is journald rotating a stale file from an EARLIER boot —
# this host logged `corrupted or uncleanly shut down` for a user journal a full hour
# into an otherwise healthy boot, and counting it alarmed on a machine that was fine.
_BOOT_EVIDENCE_WINDOW_SECONDS = 120.0

LOG = logging.getLogger("ups_orchestrator.audit")

# The longest suppression any marker can buy, enforced on read. See
# read_maintenance for why the reader and not just the writer enforces it.
MAX_MAINTENANCE_SECONDS = 24 * 3600.0
_UPS_EVENT_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bONBATT\b",
        r"\bLOWBATT\b",
        r"OL\+DISCHRG",
        r"\bDISCHRG\b",
        r"Communications with UPS",
        r"Data for UPS \[",
        r"Poll UPS \[",
        r"UPS \[.*\] data is no longer stale",
        r"UPS \[.*\] is stale",
        r"upsmon.*UPS .* on battery",
        r"upsmon.*UPS .* on line power",
        r"upssched.*\b(onbatt|online|lowbatt)\b",
    )
)
_SHUTDOWN_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bshutdown (?:attempt|sent|failed)\b",
        r"\bforced shutdown\b",
        r"\bremote_shutdown\b",
        r"\b(load\.off|driver\.killpower|shutdown\.return|shutdown\.stayoff)\b",
        r"\b(ssh|serial|local).{0,80}\b(shutdown|poweroff|halt)\b",
        r"\b(shutdown|poweroff|halt).{0,80}\b(ssh|serial|local|r630|spark|mt)\b",
        r"\b(r630|spark|mt).{0,80}\b(shutdown|poweroff|halt)\b",
        r"(/sbin/shutdown|systemctl poweroff)",
    )
)


@dataclass(frozen=True)
class AuditReport:
    """Rendered audit plus counts that can be asserted in tests."""

    text: str
    power_loss_count: int
    ups_event_count: int
    shutdown_action_count: int


@dataclass(frozen=True)
class BootAuditResult:
    """Result of a one-shot boot recovery audit notification."""

    sent: bool
    boot_id: str
    power_loss_count: int
    shutdown_action_count: int
    text: str


@dataclass(frozen=True)
class MaintenanceWindow:
    """An operator-declared window in which abrupt power cuts are EXPECTED.

    Deliberately time-bounded. A boolean "maintenance mode" flag that an operator
    forgets to clear silences outage alerting forever, and the failure is invisible
    precisely because nothing is delivered; an expiry makes the safe state the
    default one.
    """

    active: bool
    until: float = 0.0
    reason: str = ""


@dataclass(frozen=True)
class SampleGapReport:
    """Interpretation of recorder samples across a boot boundary."""

    lines: list[str]
    summary: str
    suspicious_power_path: bool


def _run(args: list[str], timeout: float = 15.0) -> list[str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return [f"(failed to run {' '.join(args)}: {exc})"]
    if proc.returncode != 0 and not proc.stdout:
        err = proc.stderr.strip()
        return [f"(command failed: {' '.join(args)}{': ' + err if err else ''})"]
    return proc.stdout.splitlines()


def _contains_any(line: str, patterns: tuple[re.Pattern[str], ...]) -> bool:
    return any(pattern.search(line) for pattern in patterns)


def _matching(lines: list[str], patterns: tuple[re.Pattern[str], ...], limit: int) -> list[str]:
    matches = [line for line in lines if _contains_any(line, patterns)]
    return matches[-limit:]


def _journal_since(since: str) -> list[str]:
    return _run(["journalctl", "--since", since, "--no-pager"], timeout=30.0)


def _list_boots() -> list[str]:
    return _run(["journalctl", "--list-boots", "--no-pager"], timeout=10.0)


def _journal_current_boot() -> list[str]:
    return _run(["journalctl", "-b", "0", "--no-pager", "-o", "short-unix"], timeout=30.0)


def _strip_unix_prefix(lines: list[str]) -> list[str]:
    """Drop the ``-o short-unix`` timestamp prefix, keeping every line."""
    out: list[str] = []
    for line in lines:
        stamp, _, rest = line.partition(" ")
        try:
            float(stamp)
        except ValueError:
            out.append(line)
        else:
            out.append(rest)
    return out


def within_boot_window(
    lines: list[str], window: float = _BOOT_EVIDENCE_WINDOW_SECONDS
) -> list[str]:
    """Keep only ``short-unix`` lines logged within ``window`` of the boot's first line.

    The unix-timestamp prefix is stripped from every line it keeps. A line whose
    prefix will not parse is kept verbatim rather than dropped — losing real
    evidence to a format surprise is far worse than carrying an extra line.
    """
    first: float | None = None
    kept: list[str] = []
    for line in lines:
        stamp, _, rest = line.partition(" ")
        try:
            when = float(stamp)
        except ValueError:
            kept.append(line)
            continue
        if first is None:
            first = when
        if when - first <= window:
            kept.append(rest)
    return kept


def previous_boot_ended_cleanly() -> bool:
    """True when the PREVIOUS boot logged systemd's own shutdown sequence.

    This is the signal filesystem forensics only approximate. A clean ``poweroff``
    or ``reboot`` always writes these lines; an abrupt cut leaves the journal
    simply stopping mid-line. Filesystem evidence, by contrast, fires routinely on
    external USB disks and on a Pi's vfat ``/boot/firmware`` after shutdowns that
    were perfectly clean, which is what made the old alert cry wolf.
    """
    lines = _run(["journalctl", "-b", "-1", "--no-pager", "-n", "300"], timeout=20.0)
    return any(_contains_any(line, _CLEAN_SHUTDOWN_PATTERNS) for line in lines)


def _current_boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _snapshot_line(name: str, label: str, snap: UpsSnapshot) -> str:
    status = snap.status or "NO COMM"
    charge = "unknown" if snap.charge is None else f"{snap.charge}%"
    runtime = "unknown" if snap.runtime_seconds is None else fmt_duration(snap.runtime_seconds)
    if snap.estimated_load_watts is None or snap.realpower_nominal is None:
        load = "unknown" if snap.load is None else f"{snap.load}% {snap.load_level}"
    else:
        load = (
            f"{snap.load}% {snap.load_level} "
            f"(~{snap.estimated_load_watts}/{snap.realpower_nominal}W, "
            f"{snap.load_margin_percent}% margin)"
        )
    line = f"- {name} ({label}): {status}, battery {charge}, est. to 0% {runtime}, load {load}"
    extras: list[str] = []
    if snap.output_voltage is not None:
        extras.append(f"{snap.output_voltage:.1f}V out")
    if snap.battery_voltage is not None:
        bv = f"batt {snap.battery_voltage:.1f}V"
        if snap.battery_voltage_percent is not None:
            bv += f" ({snap.battery_voltage_percent}%)"
        extras.append(bv)
    if snap.test_result and snap.test_result.strip().lower() not in ("no test initiated", ""):
        extras.append(f"self-test: {snap.test_result}")
    if snap.alarm and snap.alarm.strip().lower() not in ("", "none"):
        extras.append(f"ALARM: {snap.alarm}")
    if extras:
        line += " · " + ", ".join(extras)
    return line


def _sample_ups_line(name: str, payload: object) -> str:
    if not isinstance(payload, dict):
        return f"{name}: malformed sample"
    status = payload.get("status") or "NO COMM"
    charge = payload.get("charge")
    runtime = payload.get("runtime_seconds")
    load = payload.get("load")
    input_voltage = payload.get("input_voltage")
    output_voltage = payload.get("output_voltage")
    charge_text = "unknown" if charge is None else f"{charge}%"
    runtime_text = (
        "unknown" if not isinstance(runtime, (int, float)) else fmt_duration(int(runtime))
    )
    load_text = "unknown" if load is None else f"{load}%"
    volts = []
    if isinstance(input_voltage, (int, float)):
        volts.append(f"{input_voltage:.1f}V in")
    if isinstance(output_voltage, (int, float)):
        volts.append(f"{output_voltage:.1f}V out")
    voltage_text = ", ".join(volts) if volts else "unknown voltage"
    return (
        f"{name}: {status}, battery {charge_text}, est. to 0% {runtime_text}, "
        f"load {load_text}, {voltage_text}"
    )


def _sample_is_healthy(payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    status = str(payload.get("status") or "")
    charge = payload.get("charge")
    runtime = payload.get("runtime_seconds")
    load = payload.get("load")
    return (
        "OL" in status.split()
        and "OB" not in status.split()
        and "LB" not in status.split()
        and (not isinstance(charge, (int, float)) or charge >= 50)
        and (not isinstance(runtime, (int, float)) or runtime >= 300)
        and (not isinstance(load, (int, float)) or load < 75)
    )


def _sample_seen_on_battery(payload: object) -> bool:
    return isinstance(payload, dict) and "OB" in str(payload.get("status") or "").split()


def _section(title: str, lines: list[str], *, empty: str) -> list[str]:
    out = [title]
    if lines:
        out.extend(f"  {line}" for line in lines)
    else:
        out.append(f"  {empty}")
    return out


def _state_lines(cfg: Config, state_path: Path | None) -> list[str]:
    if state_path is None:
        return ["state path not supplied"]
    try:
        raw = json.loads(state_path.read_text())
    except FileNotFoundError:
        return [f"{state_path}: not found"]
    except (OSError, ValueError) as exc:
        return [f"{state_path}: unreadable ({exc})"]

    lines: list[str] = []
    for name in cfg.upses:
        data = raw.get(name, {}) if isinstance(raw, dict) else {}
        sent = data.get("shutdowns_sent", []) if isinstance(data, dict) else []
        onbatt_since = data.get("onbatt_since") if isinstance(data, dict) else None
        last_tick = data.get("last_tick_notified") if isinstance(data, dict) else None
        last_status = data.get("last_status") if isinstance(data, dict) else None
        sent_text = ", ".join(str(item) for item in sent) if sent else "none"
        lines.append(
            f"{name}: shutdowns_sent={sent_text}; "
            f"onbatt_since={onbatt_since}; last_tick_notified={last_tick}; "
            f"last_status={last_status}"
        )
    return lines


def _redact(line: str) -> str:
    """Avoid echoing obvious webhook-like secrets if logs ever contain them."""
    return re.sub(r"https://discord(?:app)?\.com/api/webhooks/\S+", "<discord-webhook>", line)


def _summary_lines(
    power_loss: list[str],
    ups_events: list[str],
    shutdown_actions: list[str],
) -> list[str]:
    lines = [
        f"- Hard power-loss indicators: {len(power_loss)}",
        f"- UPS/NUT event lines: {len(ups_events)}",
        f"- Shutdown/killpower evidence lines: {len(shutdown_actions)}",
    ]
    if power_loss and not shutdown_actions:
        lines.append(
            "- Inference: this window shows abrupt host power-loss/recovery evidence "
            "without matching orchestrator shutdown or UPS killpower evidence."
        )
    elif shutdown_actions:
        lines.append(
            "- Inference: review the shutdown evidence section; at least one line "
            "matched a target-shutdown or UPS-output command pattern."
        )
    else:
        lines.append("- Inference: no obvious reboot or orchestrator shutdown evidence matched.")
    return lines


def _tail_jsonl(path: Path | None, limit: int) -> list[dict[str, object]]:
    if path is None:
        return []
    try:
        lines = path.read_text().splitlines()[-limit:]
    except (FileNotFoundError, OSError):
        return []

    records: list[dict[str, object]] = []
    for line in lines:
        try:
            raw = json.loads(line)
        except ValueError:
            continue
        if isinstance(raw, dict):
            records.append(raw)
    return records


def _event_log_lines(path: Path | None, limit: int) -> list[str]:
    lines: list[str] = []
    for record in _tail_jsonl(path, limit):
        snap = record.get("snapshot")
        status = ""
        if isinstance(snap, dict):
            status = (
                f" status={snap.get('status')} charge={snap.get('charge')} load={snap.get('load')}"
            )
        data = record.get("data")
        extra = f" data={data}" if data else ""
        lines.append(
            f"{record.get('time')} {record.get('ups')} {record.get('event')}: "
            f"{record.get('message')}{status}{extra}"
        )
    return lines


def _notification_log_lines(path: Path | None, limit: int) -> list[str]:
    lines: list[str] = []
    for record in _tail_jsonl(path, limit):
        status = "ok" if record.get("ok") else "failed"
        err = f" error={record.get('error')}" if record.get("error") else ""
        lines.append(
            f"{record.get('time')} {status} attempts={record.get('attempts')} "
            f"http={record.get('status_code')} title={record.get('title')}{err}"
        )
    return lines


def _sample_paths(path: Path) -> list[Path]:
    """Return retained sample segments oldest-first, then the active file."""
    rotations: list[tuple[int, Path]] = []
    for candidate in path.parent.glob(f"{path.name}.*"):
        suffix = candidate.name.removeprefix(f"{path.name}.")
        if suffix.isdigit():
            rotations.append((int(suffix), candidate))
    return [candidate for _, candidate in sorted(rotations, reverse=True)] + [path]


def _boot_boundary_samples(
    path: Path | None, boot_id: str
) -> tuple[dict[str, object] | None, dict[str, object] | None, bool]:
    """Stream retained files and return the records bracketing ``boot_id``.

    This intentionally keeps only one prior record in memory. Ten 50 MB
    rotations must not turn the audit command into a multi-gigabyte allocation.
    """
    if path is None:
        return None, None, False
    previous: dict[str, object] | None = None
    saw_record = False
    for candidate in _sample_paths(path):
        try:
            handle = candidate.open(encoding="utf-8")
        except OSError:
            continue
        with handle:
            for line in handle:
                try:
                    raw = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(raw, dict):
                    continue
                saw_record = True
                if raw.get("boot_id") == boot_id:
                    return previous, raw, True
                previous = raw
    return previous, None, saw_record


def _record_unix_time(record: dict[str, object]) -> float:
    value = record.get("unix_time", 0.0)
    return float(value) if isinstance(value, (int, float, str)) else 0.0


def sample_gap_report(path: Path | None, *, current_boot_id: str | None = None) -> SampleGapReport:
    """Explain what the recorder saw immediately before this boot."""
    boot_id = current_boot_id or _current_boot_id()
    previous, current, saw_record = _boot_boundary_samples(path, boot_id)
    if not saw_record:
        return SampleGapReport(
            lines=["no recorder samples found"],
            summary="No recorder samples were available for boot-boundary analysis.",
            suspicious_power_path=False,
        )

    if current is None:
        return SampleGapReport(
            lines=[f"no samples found for current boot {boot_id[:12]}"],
            summary="Recorder has not written a sample for the current boot yet.",
            suspicious_power_path=False,
        )
    if previous is None:
        return SampleGapReport(
            lines=["no prior-boot sample exists before the first current-boot sample"],
            summary=(
                "Recorder has no pre-reboot sample to compare. This is expected for "
                "incidents before the recorder was installed or after log rotation."
            ),
            suspicious_power_path=False,
        )

    prev_time = str(previous.get("time", "unknown"))
    current_time = str(current.get("time", "unknown"))
    prev_unix = previous.get("unix_time")
    current_unix = current.get("unix_time")
    gap = (
        int(float(current_unix) - float(prev_unix))
        if isinstance(prev_unix, (int, float)) and isinstance(current_unix, (int, float))
        else None
    )

    upses = previous.get("upses")
    sample_map = upses if isinstance(upses, dict) else {}
    lines = [
        f"last sample before reboot: {prev_time} boot={str(previous.get('boot_id', ''))[:12]}",
        f"first sample after reboot: {current_time} boot={boot_id[:12]}",
        f"sample gap: {fmt_duration(gap) if gap is not None else 'unknown'}",
    ]
    lines.extend(_sample_ups_line(name, payload) for name, payload in sample_map.items())

    payloads = list(sample_map.values())
    all_healthy = bool(payloads) and all(_sample_is_healthy(payload) for payload in payloads)
    any_on_battery = any(_sample_seen_on_battery(payload) for payload in payloads)
    if all_healthy:
        summary = (
            "Recorder gap is suspicious for a physical power-path problem: the host "
            "rebooted after the last sample showed every recorded UPS online, charged, "
            "and not overloaded."
        )
        suspicious = True
    elif any_on_battery:
        summary = (
            "Recorder saw at least one UPS on battery before the reboot. If runtime and "
            "charge were still healthy, suspect output transfer failure or downstream wiring."
        )
        suspicious = True
    else:
        summary = (
            "Recorder has a boot-boundary sample, but it does not conclusively classify "
            "the physical power path."
        )
        suspicious = False
    lines.insert(0, f"inference: {summary}")
    return SampleGapReport(lines=lines, summary=summary, suspicious_power_path=suspicious)


def build_audit(
    cfg: Config,
    *,
    since: str = "today",
    limit: int = 80,
    state_path: Path | None = None,
    event_log_path: Path | None = None,
    notification_log_path: Path | None = None,
    sample_path: Path | None = None,
) -> AuditReport:
    """Build a text audit report for UPS power incidents."""
    journal = [_redact(line) for line in _journal_since(since)]
    boots = _list_boots()[-8:]
    power_loss = _matching(journal, _POWER_LOSS_PATTERNS, limit)
    ups_events = _matching(journal, _UPS_EVENT_PATTERNS, limit)
    shutdown_actions = _matching(journal, _SHUTDOWN_PATTERNS, limit)
    state = _state_lines(cfg, state_path)
    local_events = _event_log_lines(event_log_path, min(limit, 40))
    notification_events = _notification_log_lines(notification_log_path, min(limit, 40))
    recorder_gap = sample_gap_report(sample_path)

    snapshots = [
        _snapshot_line(name, ups.label, read_snapshot(name)) for name, ups in cfg.upses.items()
    ]

    lines: list[str] = [
        "UPS Audit Report",
        f"Since: {since}",
        "",
        "Summary",
        *_summary_lines(power_loss, ups_events, shutdown_actions),
        "",
        "Current UPS State",
        *snapshots,
        "",
        *_section("Boot History", boots, empty="no boot history returned"),
        "",
        *_section("Orchestrator State", state, empty="no state returned"),
        "",
        *_section(
            "Hard Power-Loss Indicators",
            power_loss,
            empty="none found in selected window",
        ),
        "",
        *_section("UPS / NUT Events", ups_events, empty="none found in selected window"),
        "",
        *_section(
            "Shutdown Actions / Killpower Evidence",
            shutdown_actions,
            empty="none found in selected window",
        ),
        "",
        *_section("Local Orchestrator Event Log", local_events, empty="no local events logged"),
        "",
        *_section(
            "Discord Delivery Log",
            notification_events,
            empty="no notification deliveries logged",
        ),
        "",
        *_section(
            "Recorder Boot-Gap Analysis",
            recorder_gap.lines,
            empty="no recorder gap analysis",
        ),
        "",
        "Interpretation Hints",
        "  - Dirty-bit, corrupt-journal, or recovering-journal lines mean abrupt power loss.",
        "  - shutdown sent / shutdown FAILED lines are orchestrator target actions.",
        "  - load.off, driver.killpower, shutdown.return, or shutdown.stayoff "
        "indicate UPS output commands.",
        "  - OL+DISCHRG on CyberPower can be real battery discharge hidden behind online status.",
        "  - A reboot after healthy OL samples points at surge-only outlet use, "
        "downstream PDU/power-strip loss, or UPS transfer/output failure.",
    ]
    return AuditReport(
        text="\n".join(lines),
        power_loss_count=len(power_loss),
        ups_event_count=len(ups_events),
        shutdown_action_count=len(shutdown_actions),
    )


def _read_marker(path: Path) -> str | None:
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return None
    if isinstance(raw, dict):
        value = raw.get("boot_id")
        return str(value) if value else None
    return None


def read_maintenance(path: Path | None, *, now: float) -> MaintenanceWindow:
    """Read the maintenance marker, treating anything unreadable as "not active".

    The bound below is enforced by the READER, deliberately, because the writer
    is not the only thing that can create this file. ``/var/lib/ups-orchestrator``
    is group-writable so the ``--user`` recorder can write there, and uid ``nut``
    — which runs the LAN-facing ``upsd`` — owns it. A lower-privileged account can
    therefore plant a marker, and this file suppresses outage alerting. Capping
    the horizon on read means a forged ``until: 9e9`` expires immediately instead
    of silencing power-loss alerts forever (T-03-04).
    """
    if path is None:
        return MaintenanceWindow(active=False)
    if path.is_symlink():
        LOG.warning("maintenance marker %s is a symlink; ignoring it", path)
        return MaintenanceWindow(active=False)
    try:
        raw = json.loads(path.read_text())
    except (FileNotFoundError, OSError, ValueError):
        return MaintenanceWindow(active=False)
    if not isinstance(raw, dict):
        return MaintenanceWindow(active=False)
    until = raw.get("until")
    if not isinstance(until, (int, float)) or isinstance(until, bool):
        return MaintenanceWindow(active=False)
    until = float(until)
    if until - now > MAX_MAINTENANCE_SECONDS:
        LOG.warning(
            "maintenance marker %s ends %.0fs from now, beyond the %.0fs cap; ignoring it",
            path,
            until - now,
            MAX_MAINTENANCE_SECONDS,
        )
        return MaintenanceWindow(active=False)
    return MaintenanceWindow(
        active=now < until,
        until=until,
        reason=str(raw.get("reason", "")),
    )


def write_maintenance(path: Path, *, until: float, reason: str) -> None:
    """Declare a maintenance window ending at ``until`` (unix seconds).

    Opened ``O_NOFOLLOW``: the metadata-preserving writer used elsewhere resolves
    symlinks on purpose (MED-03), which is right for a root-owned /etc config and
    wrong for a marker in a directory a less-privileged account can write. A
    planted symlink must fail this call, not be followed through (T-03-04).
    """
    if not math.isfinite(until):
        raise ValueError(f"maintenance window end must be a finite timestamp, got {until!r}")
    payload = json.dumps({"until": until, "reason": reason}, sort_keys=True)
    # The metadata-preserving writer this replaced created the parent directory;
    # a $UPS_ORCH_MAINTENANCE_STATE pointing somewhere new must keep working.
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(payload + "\n")


def clear_maintenance(path: Path) -> bool:
    """End any maintenance window. Returns False if there was none to clear.

    Deliberately lets an OSError propagate. Swallowing it returned the same
    ``False`` as "no window existed", so the CLI printed "no window was open"
    while suppression remained in force — a security control reporting a
    success-shaped failure (T-03-06).
    """
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True


def _write_marker(path: Path, boot_id: str) -> None:
    # IF-08: this was a bare `tmp.replace(path)` — the same pattern state.py
    # documents as destroying the destination inode's mode, owner and ACL (T-02-23).
    # Lower blast radius than IF-02 because `install.sh` puts a DEFAULT ACL on
    # /var/lib/ups-orchestrator so a new inode inherits it, but the same unmigrated
    # class, and "lower blast radius" is not a property this file controls — an
    # operator who moves the marker with $UPS_ORCH_STATE takes the default ACL away.
    write_json_preserving_metadata(path, {"boot_id": boot_id}, indent=None)


def send_boot_audit(
    cfg: Config,
    notifier: Notifier,
    *,
    marker_path: Path,
    sample_path: Path | None = None,
    limit: int = 12,
    maintenance_path: Path | None = None,
    now: float | None = None,
    clean_shutdown: bool | None = None,
) -> BootAuditResult:
    """Send one Discord alert if the current boot looks like abrupt power loss.

    Three gates suppress the alert before any filesystem evidence is weighed, in
    order of how strongly each says "this was intentional":

    1. already reported for this boot (the marker),
    2. the PREVIOUS boot logged systemd's shutdown sequence, so it was a clean
       ``poweroff``/``reboot`` and no amount of fsck noise makes it an outage,
    3. an operator-declared maintenance window was still open.

    ``clean_shutdown`` and ``now`` are injectable so the gates are testable
    without spawning ``journalctl`` or depending on the wall clock.
    """
    boot_id = _current_boot_id()
    journal = [_redact(line) for line in _journal_current_boot()]
    # The window scopes FILESYSTEM-RECOVERY evidence only, which is written while
    # mounting. Shutdown/killpower evidence is the orchestrator's own actions and
    # happens at arbitrary uptime, so windowing it pinned shutdown_action_count to
    # zero — which in turn pinned the alert body to "No target shutdown or UPS
    # killpower evidence was found", regardless of what had actually happened.
    power_loss = _matching(within_boot_window(journal), _POWER_LOSS_PATTERNS, limit)
    shutdown_actions = _matching(_strip_unix_prefix(journal), _SHUTDOWN_PATTERNS, limit)

    if _read_marker(marker_path) == boot_id:
        return BootAuditResult(
            sent=False,
            boot_id=boot_id,
            power_loss_count=len(power_loss),
            shutdown_action_count=len(shutdown_actions),
            text="boot audit already sent for this boot",
        )

    if not power_loss:
        # Evaluated this boot with nothing to deliver — safe to mark done.
        _write_marker(marker_path, boot_id)
        return BootAuditResult(
            sent=False,
            boot_id=boot_id,
            power_loss_count=0,
            shutdown_action_count=len(shutdown_actions),
            text="current boot does not show abrupt power-loss indicators",
        )

    was_clean = previous_boot_ended_cleanly() if clean_shutdown is None else clean_shutdown
    if was_clean:
        _write_marker(marker_path, boot_id)
        return BootAuditResult(
            sent=False,
            boot_id=boot_id,
            power_loss_count=len(power_loss),
            shutdown_action_count=len(shutdown_actions),
            text=(
                "previous boot logged a clean systemd shutdown; filesystem recovery "
                "evidence is not an outage"
            ),
        )

    window = read_maintenance(maintenance_path, now=time.time() if now is None else now)
    if window.active:
        _write_marker(marker_path, boot_id)
        return BootAuditResult(
            sent=False,
            boot_id=boot_id,
            power_loss_count=len(power_loss),
            shutdown_action_count=len(shutdown_actions),
            text=(
                "operator maintenance window is open"
                + (f" ({window.reason})" if window.reason else "")
            ),
        )

    fields: list[tuple[str, str]] = [
        ("Boot", boot_id[:12]),
        ("Hard power-loss indicators", str(len(power_loss))),
        ("Shutdown/killpower evidence", str(len(shutdown_actions))),
    ]
    for name, ups in cfg.upses.items():
        snap = read_snapshot(name)
        load = "unknown" if snap.load is None else f"{snap.load}% {snap.load_level}"
        runtime = "unknown" if snap.runtime_seconds is None else fmt_duration(snap.runtime_seconds)
        fields.append((ups.label, f"{snap.status or 'NO COMM'}; {load}; est. to 0% {runtime}"))

    details = "\n".join(power_loss[-3:])
    gap = sample_gap_report(sample_path, current_boot_id=boot_id)
    gap_text = "\n".join(gap.lines[:8])
    result = notifier.send(
        Notification(
            title="⚠️ UPS host recovered from abrupt power loss",
            body=(
                "This host rebooted without a clean shutdown. No target shutdown "
                "or UPS killpower evidence was found in the current boot logs."
                if not shutdown_actions
                else "This host rebooted after an unclean shutdown; review audit output."
            )
            + (f"\n\nRecent evidence:\n```text\n{details}\n```" if details else "")
            + (f"\n\nRecorder gap:\n```text\n{gap_text}\n```" if gap_text else ""),
            level=Level.CRITICAL,
            fields=fields,
        )
    )
    if result.configured and not result.ok:
        # Delivery was attempted and failed — the network is frequently not up
        # yet right after a power-loss reboot. Do NOT write the marker, so the
        # next boot-audit run retries instead of suppressing the alert forever.
        return BootAuditResult(
            sent=False,
            boot_id=boot_id,
            power_loss_count=len(power_loss),
            shutdown_action_count=len(shutdown_actions),
            text="boot audit send failed; will retry next run",
        )
    # Delivered, or no notifier configured (nothing to retry) — mark this boot done.
    _write_marker(marker_path, boot_id)
    return BootAuditResult(
        sent=result.ok,
        boot_id=boot_id,
        power_loss_count=len(power_loss),
        shutdown_action_count=len(shutdown_actions),
        text="boot audit sent" if result.ok else "boot audit evaluated; no notifier configured",
    )
