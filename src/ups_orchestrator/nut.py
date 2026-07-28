"""Thin wrappers around the NUT ``upsc`` CLI for reading UPS variables."""

from __future__ import annotations

import logging
import shutil
import subprocess
import time
from dataclasses import dataclass

UPSC_BIN = shutil.which("upsc") or "/bin/upsc"
UPSCMD_BIN = shutil.which("upscmd") or "/bin/upscmd"

LOG = logging.getLogger("ups_orchestrator.nut")

# Throttled per UPS: the recorder polls every few seconds, so an unthrottled
# warning would bury the journal during the very outage it exists to surface.
_FAILURE_LOG_INTERVAL_SECONDS = 300.0
_last_failure_log: dict[str, float] = {}


def _note_upsc_failure(ups_name: str, detail: str) -> None:
    """Record that ``upsc`` could not be read, at most once per interval per UPS.

    This module used to swallow every failure and return an empty result. That
    silence is what let a two-day outage pass unnoticed: upsd stopped listening
    on localhost, ``upsc`` was refused on every poll, the recorder wrote nulls,
    and every renderer printed "unknown" — so a total communications loss and a
    genuinely idle UPS produced identical output. Nothing anywhere said "I could
    not read this". A log line is the cheapest thing that would have caught it.
    """
    now = time.monotonic()
    last = _last_failure_log.get(ups_name)
    if last is not None and now - last < _FAILURE_LOG_INTERVAL_SECONDS:
        return
    _last_failure_log[ups_name] = now
    LOG.warning("upsc %s: read failed — %s", ups_name, detail or "no detail")


def _note_upsc_success(ups_name: str) -> None:
    """Clear the throttle, and say so if this UPS had been failing."""
    if _last_failure_log.pop(ups_name, None) is not None:
        LOG.info("upsc %s: communication restored", ups_name)


def upscmd(
    ups_name: str, command: str, *, user: str, password: str, timeout: float = 15.0
) -> tuple[int, str, str]:
    """Run an instant command via ``upscmd`` (e.g. ``test.battery.start.quick``).

    Needs a NUT admin account (``instcmds``/``actions`` in ``upsd.users``); the
    credentials are the caller's responsibility and must never be logged. Returns
    ``(returncode, stdout, stderr)``; a non-zero code on failure.
    """
    try:
        result = subprocess.run(
            [UPSCMD_BIN, "-u", user, "-p", password, ups_name, command],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, "", str(exc)
    return result.returncode, result.stdout.strip(), result.stderr.strip()


def upsc_var(ups_name: str, key: str, timeout: float = 10.0) -> str | None:
    """Return a single UPS variable via ``upsc <ups> <key>``, or ``None`` on failure."""
    try:
        result = subprocess.run(
            [UPSC_BIN, ups_name, key],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def upsc_vars(ups_name: str, timeout: float = 10.0) -> dict[str, str]:
    """Return all variables for one UPS using a single ``upsc`` process.

    The recorder samples several UPSes continuously. Reading each field with a
    separate process wastes CPU and still returns values from the same NUT
    driver poll. A single bulk read is both cheaper and internally consistent.
    """
    try:
        result = subprocess.run(
            [UPSC_BIN, ups_name],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        _note_upsc_failure(ups_name, str(exc))
        return {}
    if result.returncode != 0:
        _note_upsc_failure(ups_name, result.stderr.strip())
        return {}
    _note_upsc_success(ups_name)
    values: dict[str, str] = {}
    for raw in result.stdout.splitlines():
        key, separator, value = raw.partition(":")
        if separator and key.strip():
            values[key.strip()] = value.strip()
    return values


@dataclass(frozen=True)
class UpsSnapshot:
    """A point-in-time read of the variables we report on."""

    status: str | None
    charge: int | None
    runtime_seconds: int | None
    load: int | None
    input_voltage: float | None
    output_voltage: float | None = None
    realpower_nominal: int | None = None
    test_result: str | None = None
    timer_shutdown: int | None = None
    timer_start: int | None = None
    alarm: str | None = None
    # Extended NUT fields (Phase G). battery.mfr.date is intentionally omitted —
    # these CyberPower units report the vendor string ("CPS") there, not a date,
    # so battery age is not derivable.
    battery_voltage: float | None = None
    battery_voltage_nominal: float | None = None
    battery_type: str | None = None
    input_voltage_nominal: float | None = None
    driver_state: str | None = None
    beeper_status: str | None = None
    delay_shutdown: int | None = None
    delay_start: int | None = None
    device_serial: str | None = None
    charge_low: int | None = None
    charge_warning: int | None = None
    runtime_low: int | None = None

    @property
    def battery_voltage_percent(self) -> int | None:
        """Battery voltage as a percent of nominal.

        A charged lead-acid pack sits well above 100% (e.g. 27/24 V ≈ 113%); the
        aging signal is a declining *charged peak* over time, not a single
        reading, so this is context/trend data — not a clamped health gauge.
        """
        if self.battery_voltage is None or not self.battery_voltage_nominal:
            return None
        return round(self.battery_voltage / self.battery_voltage_nominal * 100)

    @property
    def input_voltage_percent(self) -> int | None:
        """Input voltage as a percent of nominal line voltage (line quality)."""
        if self.input_voltage is None or not self.input_voltage_nominal:
            return None
        return round(self.input_voltage / self.input_voltage_nominal * 100)

    @property
    def load_headroom_watts(self) -> int | None:
        """Unused output capacity in watts (nominal − estimated draw)."""
        watts = self.estimated_load_watts
        if watts is None or self.realpower_nominal is None:
            return None
        return max(0, self.realpower_nominal - watts)

    @property
    def on_battery(self) -> bool:
        """True when the UPS status contains the NUT ``OB`` (on battery) flag."""
        return self.status is not None and "OB" in self.status

    @property
    def low_battery(self) -> bool:
        """True when the UPS status contains the NUT ``LB`` (low battery) flag."""
        return self.status is not None and "LB" in self.status

    @property
    def estimated_load_watts(self) -> int | None:
        """Approximate active load in watts when NUT reports load% and nominal watts."""
        if self.load is None or self.realpower_nominal is None:
            return None
        return round(self.realpower_nominal * self.load / 100)

    @property
    def load_margin_percent(self) -> int | None:
        """Approximate unused UPS output capacity as a percentage."""
        if self.load is None:
            return None
        return max(0, 100 - self.load)

    @property
    def load_level(self) -> str:
        """Human load classification based on percent of rated UPS output."""
        if self.load is None:
            return "UNKNOWN"
        if self.load >= 100:
            return "OVER"
        if self.load >= 90:
            return "CRIT"
        if self.load >= 75:
            return "HIGH"
        if self.load >= 50:
            return "WATCH"
        return "OK"

    @property
    def load_is_high(self) -> bool:
        """True when output load deserves a warning."""
        return self.load is not None and self.load >= 75


def _as_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(float(value))
    except ValueError:
        return None


def _as_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def read_snapshot(ups_name: str) -> UpsSnapshot:
    """Read the variables we care about for ``ups_name`` in one pass."""
    values = upsc_vars(ups_name)
    return UpsSnapshot(
        status=values.get("ups.status"),
        charge=_as_int(values.get("battery.charge")),
        runtime_seconds=_as_int(values.get("battery.runtime")),
        load=_as_int(values.get("ups.load")),
        input_voltage=_as_float(values.get("input.voltage")),
        output_voltage=_as_float(values.get("output.voltage")),
        realpower_nominal=_as_int(values.get("ups.realpower.nominal")),
        test_result=values.get("ups.test.result"),
        timer_shutdown=_as_int(values.get("ups.timer.shutdown")),
        timer_start=_as_int(values.get("ups.timer.start")),
        alarm=values.get("ups.alarm"),
        battery_voltage=_as_float(values.get("battery.voltage")),
        battery_voltage_nominal=_as_float(values.get("battery.voltage.nominal")),
        battery_type=values.get("battery.type"),
        input_voltage_nominal=_as_float(values.get("input.voltage.nominal")),
        driver_state=values.get("driver.state"),
        beeper_status=values.get("ups.beeper.status"),
        delay_shutdown=_as_int(values.get("ups.delay.shutdown")),
        delay_start=_as_int(values.get("ups.delay.start")),
        device_serial=values.get("device.serial") or values.get("ups.serial"),
        charge_low=_as_int(values.get("battery.charge.low")),
        charge_warning=_as_int(values.get("battery.charge.warning")),
        runtime_low=_as_int(values.get("battery.runtime.low")),
    )
