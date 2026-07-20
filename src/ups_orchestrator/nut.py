"""Thin wrappers around the NUT ``upsc`` CLI for reading UPS variables."""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass

UPSC_BIN = shutil.which("upsc") or "/bin/upsc"


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
    except (OSError, subprocess.SubprocessError):
        return {}
    if result.returncode != 0:
        return {}
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
    )
