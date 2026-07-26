"""Terminal status view for ``ups-orchestrator status`` (zero deps, ANSI only)."""

from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from ups_orchestrator.config import Config, ConfigNotice
from ups_orchestrator.events import fmt_duration
from ups_orchestrator.nut import UpsSnapshot, read_snapshot

_RESET = "\033[0m"
_DIM = "\033[2m"
_BOLD = "\033[1m"
_GREEN = "\033[32m"
_YELLOW = "\033[33m"
_RED = "\033[31m"
_CYAN = "\033[36m"
_HOME = "\033[H"
_CLEAR_EOL = "\033[K"
_CLEAR_EOS = "\033[J"

_BLOCKS = "▁▂▃▄▅▆▇█"
_GAUGE_W = 14

_ANSI_RE = re.compile(r"\033\[[0-9;]*m")


def _vlen(text: str) -> int:
    """Visible length — the printable width once ANSI colour codes are stripped."""
    return len(_ANSI_RE.sub("", text))


def _panel(title: str, title_vlen: int, lines: list[str], *, use_color: bool) -> list[str]:
    """Box a card's content lines under a titled top border, ANSI-width aware."""
    inner = max([title_vlen, *(_vlen(x) for x in lines)]) if lines else title_vlen
    tl, tr, bl, br, h, v = (
        ("╭", "╮", "╰", "╯", "─", "│") if use_color else ("+", "+", "+", "+", "-", "|")
    )  # noqa: E501
    dim = _DIM if use_color else ""
    reset = _RESET if use_color else ""
    top = f"{dim}{tl}{h} {reset}{title} {dim}{h * (inner - title_vlen - 1)}{tr}{reset}"
    out = [top]
    for x in lines:
        out.append(f"{dim}{v}{reset} {x}{' ' * (inner - _vlen(x))} {dim}{v}{reset}")
    out.append(f"{dim}{bl}{h * (inner + 2)}{br}{reset}")
    return out


def _classify(snap: UpsSnapshot) -> tuple[str, str]:
    """Return (state text, ANSI colour) for a snapshot."""
    if snap.status is None:
        return "✖ NO COMM", _RED
    if snap.low_battery:
        return "⚠ LOW BATTERY", _RED
    if snap.on_battery:
        return "⚡ ON BATTERY", _YELLOW
    suffix = " (charging)" if "CHRG" in snap.status else ""
    return f"● Online{suffix}", _GREEN


def _load_color(level: str) -> str:
    if level in ("CRIT", "OVER"):
        return _RED
    if level in ("HIGH", "WATCH"):
        return _YELLOW
    return _GREEN


def _battery_color(charge: int | None) -> str:
    if charge is None:
        return _DIM
    if charge <= 20:
        return _RED
    if charge <= 50:
        return _YELLOW
    return _GREEN


def _gauge(frac: float, color: str, *, use_color: bool, width: int = _GAUGE_W) -> str:
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    bar = "█" * filled + "░" * (width - filled)
    return f"{color}{bar}{_RESET}" if use_color else bar


def _sparkline(watts: list[int], cols: int = 34) -> str:
    if len(watts) < 2:
        return ""
    step = max(1, len(watts) // cols)
    buckets = [max(watts[i : i + step]) for i in range(0, len(watts), step)][:cols]
    lo, hi = min(buckets), max(buckets)
    if hi == lo:
        return _BLOCKS[3] * len(buckets)
    return "".join(_BLOCKS[(w - lo) * 7 // (hi - lo)] for w in buckets)


def _recent_watts(sample_path: Path | None, name: str, now: float, minutes: int = 30) -> list[int]:
    if sample_path is None:
        return []
    try:
        from ups_orchestrator.dashboard import _read_series

        series = _read_series(sample_path, [name], now - minutes * 60)
    except Exception:  # noqa: BLE001 — telemetry is best-effort; never break status
        return []
    return [w for _, w in series.get(name, [])]


def _card(
    ups_label: str, snap: UpsSnapshot, watts_hist: list[int], *, use_color: bool
) -> list[str]:
    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if use_color else text

    state, scolor = _classify(snap)
    title = f"{c(ups_label, _BOLD)}  {c(state, scolor)}"

    v_in = "—" if snap.input_voltage is None else f"{snap.input_voltage:.0f}V"
    v_out = "" if snap.output_voltage is None else f" / {snap.output_voltage:.0f}V out"
    volt = f"Input   {c(f'{v_in}{v_out}', _DIM)}"

    charge = snap.charge
    bv = f"  {snap.battery_voltage:.1f}V" if snap.battery_voltage is not None else ""
    rt = "" if snap.runtime_seconds is None else f"  ~{fmt_duration(snap.runtime_seconds)} to 0%"
    batt = (
        f"Battery {_gauge((charge or 0) / 100, _battery_color(charge), use_color=use_color)} "
        f"{'—' if charge is None else f'{charge:>3}%'}{bv}{c(rt, _DIM)}"
    )

    load = snap.load
    lg = _gauge((load or 0) / 100, _load_color(snap.load_level), use_color=use_color)
    watts, nominal, head = (
        snap.estimated_load_watts,
        snap.realpower_nominal,
        snap.load_headroom_watts,
    )
    wtext = "" if watts is None or nominal is None else f"  {watts}/{nominal} W"
    htext = "" if head is None else c(f"  {head} W free", _DIM)
    lpct = "—" if load is None else f"{load:>3}%"
    loadline = f"Load    {lg} {lpct} {snap.load_level}{wtext}{htext}"

    inner = [volt, batt, loadline]
    spark = _sparkline(watts_hist)
    if spark:
        rng = f"{min(watts_hist)}–{max(watts_hist)} W, 30m"
        inner.append(f"Draw    {c(spark, _CYAN)} {c(rng, _DIM)}")
    return ["", *_panel(title, _vlen(title), inner, use_color=use_color)]


def _degraded_block(degraded: tuple[ConfigNotice, ...], *, use_color: bool) -> list[str]:
    """Render every load-time degrade notice above the per-UPS cards.

    A disarmed shutdown authority is a standing condition, not a footnote — this is
    what makes RA-01's degrade visible without opening a terminal to run
    ``monitor list``. Returns no lines at all when ``degraded`` is empty, so a
    healthy config's status output is unchanged.
    """
    if not degraded:
        return []

    def c(text: str, code: str) -> str:
        return f"{code}{text}{_RESET}" if use_color else text

    title = c("⚠ DEGRADED CONFIG", _RED)
    lines = []
    for n in degraded:
        label = "ERROR" if n.severity == "error" else "ADVISORY"
        color = _RED if n.severity == "error" else _YELLOW
        lines.append(f"{c(label, color)} {c(n.subject, _BOLD)}: {n.message}")
    return ["", *_panel(title, _vlen(title), lines, use_color=use_color)]


def render(
    cfg: Config, *, color: bool = True, now: float | None = None, sample_path: Path | None = None
) -> str:
    """Build the full status block for every configured UPS."""
    now = time.time() if now is None else now
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(now))
    header = f"⚡ UPS Orchestrator — {ts}"
    lines = [f"{_BOLD}{header}{_RESET}" if color else header]
    lines.extend(_degraded_block(cfg.degraded, use_color=color))
    for name, ups in cfg.upses.items():
        snap = read_snapshot(name)
        lines.extend(_card(ups.label, snap, _recent_watts(sample_path, name, now), use_color=color))
    if not cfg.upses:
        lines.append(f"{_DIM}(no UPSes configured){_RESET}" if color else "(no UPSes configured)")
    return "\n".join(lines)


def run(
    cfg: Config, *, watch: bool = False, interval: float = 2.0, sample_path: Path | None = None
) -> int:
    """Print the status view once, or live-refresh it until interrupted."""
    color = sys.stdout.isatty() and not os.environ.get("NO_COLOR")
    if not watch:
        print(render(cfg, color=color, sample_path=sample_path))
        return 0
    try:
        # Home-and-overwrite (not full clear) to avoid flicker; clear to EOL per
        # line and to end-of-screen at the bottom so shorter frames don't ghost.
        while True:
            frame = render(cfg, color=color, sample_path=sample_path).split("\n")
            out = _HOME + "\n".join(line + _CLEAR_EOL for line in frame)
            out += f"\n{_DIM}refreshing every {interval:g}s — Ctrl-C to exit{_RESET}{_CLEAR_EOL}"
            sys.stdout.write(out + _CLEAR_EOS)
            sys.stdout.flush()
            time.sleep(interval)
    except KeyboardInterrupt:
        return 0
