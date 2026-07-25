from __future__ import annotations

import json

from conftest import make_ups
from ups_orchestrator.baseline import _percentile, compute_baselines, render_text
from ups_orchestrator.config import Config


def test_percentile_interpolates() -> None:
    assert _percentile([10], 50) == 10
    assert _percentile([10, 20, 30, 40], 50) == 25  # (20+30)/2
    assert _percentile([10, 20, 30, 40], 95) == 38  # 30 + (40-30)*0.85, round-half-even
    assert _percentile([100, 100, 100], 95) == 100


def _write(path, rows: list[tuple[float, dict[str, int]]]) -> None:
    with path.open("w") as fh:
        for t, watts in rows:
            fh.write(
                json.dumps(
                    {
                        "unix_time": t,
                        "upses": {n: {"estimated_load_watts": w} for n, w in watts.items()},
                    }
                )
                + "\n"
            )


def test_compute_baselines_over_window(tmp_path) -> None:
    samples = tmp_path / "samples.jsonl"
    # First line is dropped by the tail-seek slice, so lead with a throwaway.
    _write(
        samples,
        [(0.0, {"ups1": 0})]
        + [(1000.0 + i, {"ups1": 100 + i * 10}) for i in range(10)],  # 100..190
    )
    stats = compute_baselines(samples, ["ups1", "ups2"], hours=999, now=2000.0)
    s1 = stats["ups1"]
    assert s1.samples == 10  # the throwaway lead line is dropped; 10 real samples remain
    assert s1.lo == 100 and s1.hi == 190
    assert s1.median == 145  # midpoint of 100..190
    assert s1.mean == 145  # round(sum(100..190)/10) — pins mean against a */× mutant
    assert stats["ups2"].samples == 0  # no data → empty, not a crash


def test_render_text_handles_empty_and_populated(tmp_path) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    samples = tmp_path / "samples.jsonl"
    _write(samples, [(0.0, {"ups1": 0}), (1.0, {"ups1": 200}), (2.0, {"ups1": 300})])
    stats = compute_baselines(samples, ["ups1"], hours=999, now=3.0)
    out = render_text(cfg, stats)
    assert "Per-UPS draw baseline" in out
    assert "Test ups1" in out
    assert "median" in out
