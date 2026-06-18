from __future__ import annotations

from conftest import make_ups
from ups_orchestrator import status
from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot


def test_status_labels_runtime_as_estimated_time_to_empty(monkeypatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr(
        status,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 600, 50, 120.0, realpower_nominal=900),
    )

    rendered = status.render(cfg, color=False, now=0)

    assert "EST. TO 0%" in rendered
    assert "LOAD" in rendered
    assert "WATTS" in rendered
    assert "MARGIN" in rendered
    assert "50% WATCH" in rendered
    assert "~450/900W" in rendered
    assert "50%" in rendered
    assert "10m 0s" in rendered
    assert "RUNTIME" not in rendered
