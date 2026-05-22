"""Terminal status view for ``ups-orchestrator status`` (zero deps, ANSI only)."""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass

from ups_orchestrator.config import Config
from ups_orchestrator.events import charge_bar, fmt_duration
from ups_orchestrator.nut import UpsSnapshot, read_snapshot

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CLEAR = "\033[2J\033[H"


@dataclass
class _Row:
    label: str
    state: str
    state_color: str
    battery: str
    runtime: str
    load: str
    inp: str


def _classify(snap: UpsSnapshot) -> tuple[str, str]:
    """Return (state text, ANSI colour) for a snapshot."""
    if snap.status is None:
        return "✖ NO COMM", _RED
    if snap.low_battery:
        return "⚠ Low Battery", _RED
    if snap.on_battery:
        return "⚡ On Battery", _YELLOW
    suffix = " (charging)" if "CHRG" in snap.status else ""
    return f"● Online{suffix}", _GREEN


def _battery_cell(snap: UpsSnapshot) -> str:
    if snap.charge is None:
        return "—"
    return f"{charge_bar(snap.charge)} {snap.charge:>3}%"


def _row(label: str, snap: UpsSnapshot) -> _Row:
    state, color = _classify(snap)
    return _Row(
        label=label,
        state=state,
        state_color=color,
        battery=_battery_cell(snap),
        runtime="—" if snap.runtime_seconds is None else fmt_duration(snap.runtime_seconds),
        load="—" if snap.load is None else f"{snap.load}%",
        inp="—" if snap.input_voltage is None else f"{snap.input_voltage:.0f}V",
    )


def _pad(text: str, width: int) -> str:
    # Pad on plain text, then colour separately so escape codes don't skew width.
    return text + " " * max(0, width - len(text))


def render(cfg: Config, *, color: bool = True, now: float | None = None) -> str:
    """Build the full status block for every configured UPS."""

    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if color else text

    rows = [_row(u.label, read_snapshot(name)) for name, u in cfg.upses.items()]

    w_label = max([len("UPS"), *(len(r.label) for r in rows)]) + 2
    w_state = max([len("STATE"), *(len(r.state) for r in rows)]) + 2
    w_batt = max([len("BATTERY"), *(len(r.battery) for r in rows)]) + 2
    w_rt = max([len("RUNTIME"), *(len(r.runtime) for r in rows)]) + 2
    w_load = 7

    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    lines = [c(f"⚡ UPS Orchestrator — {ts}", _BOLD)]
    header = (
        _pad("UPS", w_label)
        + _pad("STATE", w_state)
        + _pad("BATTERY", w_batt)
        + _pad("RUNTIME", w_rt)
        + _pad("LOAD", w_load)
        + "INPUT"
    )
    lines.append(c(header, _DIM))
    for r in rows:
        line = (
            _pad(r.label, w_label)
            + c(_pad(r.state, w_state), r.state_color)
            + _pad(r.battery, w_batt)
            + _pad(r.runtime, w_rt)
            + _pad(r.load, w_load)
            + r.inp
        )
        lines.append(line)
    if not rows:
        lines.append(c("(no UPSes configured)", _DIM))
    return "\n".join(lines)


def run(cfg: Config, *, watch: bool = False, interval: float = 2.0) -> int:
    """Print the status view once, or live-refresh it until interrupted."""
    color = sys.stdout.isatty()
    if not watch:
        print(render(cfg, color=color))
        return 0
    try:
        while True:
            sys.stdout.write(_CLEAR)
            sys.stdout.write(render(cfg, color=color) + "\n")
            sys.stdout.write(f"\n{_DIM}refreshing every {interval:g}s — Ctrl-C to exit{_RESET}\n")
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
