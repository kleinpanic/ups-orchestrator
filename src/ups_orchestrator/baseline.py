"""Per-UPS load baseline stats computed from the recorder's sample history.

Read-only analysis over the existing telemetry JSONL — no new collection. Gives
the operator (and, later, an adaptive ``load_step`` threshold) a sense of each
UPS's *normal* draw so a steady 300 W host and a bursty one aren't judged by one
global percentage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from ups_orchestrator.config import Config


@dataclass(frozen=True)
class BaselineStats:
    """Draw statistics (watts) for one UPS over the analysis window."""

    samples: int
    median: int | None = None
    p95: int | None = None
    mean: int | None = None
    lo: int | None = None
    hi: int | None = None


def _percentile(values: list[int], pct: float) -> int:
    """Linear-interpolated percentile (0–100) over a non-empty list."""
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct / 100.0
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    frac = rank - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * frac)


def _stats(watts: list[int]) -> BaselineStats:
    if not watts:
        return BaselineStats(samples=0)
    return BaselineStats(
        samples=len(watts),
        median=_percentile(watts, 50),
        p95=_percentile(watts, 95),
        mean=round(sum(watts) / len(watts)),
        lo=min(watts),
        hi=max(watts),
    )


def compute_baselines(
    sample_path: Path, names: list[str], *, hours: int = 168, now: float | None = None
) -> dict[str, BaselineStats]:
    """Return per-UPS draw baselines over the last ``hours`` of recorder samples."""
    now = time.time() if now is None else now
    from ups_orchestrator.dashboard import _read_series

    series = _read_series(sample_path, names, now - hours * 3600)
    return {name: _stats([w for _, w in series.get(name, [])]) for name in names}


def render_text(cfg: Config, baselines: dict[str, BaselineStats], *, hours: int = 168) -> str:
    """Render the baselines as a small aligned terminal table."""
    lines = [f"Per-UPS draw baseline — last {hours}h of recorder samples", ""]
    header = f"{'UPS':<18} {'n':>7} {'median':>8} {'p95':>7} {'mean':>7} {'min':>6} {'max':>6}"
    lines.append(header)
    lines.append("-" * len(header))
    for name, ups in cfg.upses.items():
        s = baselines.get(name, BaselineStats(samples=0))
        if s.samples == 0:
            lines.append(
                f"{ups.label[:18]:<18} {'0':>7} {'—':>8} {'—':>7} {'—':>7} {'—':>6} {'—':>6}"
            )
            continue
        lines.append(
            f"{ups.label[:18]:<18} {s.samples:>7} {f'{s.median} W':>8} {f'{s.p95} W':>7} "
            f"{f'{s.mean} W':>7} {f'{s.lo}':>6} {f'{s.hi}':>6}"
        )
    return "\n".join(lines)
