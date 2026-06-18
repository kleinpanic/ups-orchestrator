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
    return UpsSnapshot(
        status=upsc_var(ups_name, "ups.status"),
        charge=_as_int(upsc_var(ups_name, "battery.charge")),
        runtime_seconds=_as_int(upsc_var(ups_name, "battery.runtime")),
        load=_as_int(upsc_var(ups_name, "ups.load")),
        input_voltage=_as_float(upsc_var(ups_name, "input.voltage")),
        output_voltage=_as_float(upsc_var(ups_name, "output.voltage")),
        realpower_nominal=_as_int(upsc_var(ups_name, "ups.realpower.nominal")),
    )
