from __future__ import annotations

from conftest import make_ups
from ups_orchestrator import status
from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot


def test_status_card_shows_gauges_load_and_headroom(monkeypatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr(
        status,
        "read_snapshot",
        lambda _name: UpsSnapshot(
            "OL",
            100,
            600,
            50,
            120.0,
            output_voltage=119.0,
            realpower_nominal=900,
            battery_voltage=27.0,
        ),
    )

    rendered = status.render(cfg, color=False, now=0)

    assert "Test ups1" in rendered
    assert "● Online" in rendered
    assert "Battery" in rendered and "Load" in rendered
    assert "█" in rendered and "░" in rendered  # gauge bars
    assert "50% WATCH" in rendered
    assert "450/900 W" in rendered
    assert "450 W free" in rendered  # headroom
    assert "27.0V" in rendered  # battery voltage
    assert "119V out" in rendered
    assert "~10m 0s to 0%" in rendered


def test_status_no_color_has_no_escape_codes(monkeypatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr(
        status, "read_snapshot", lambda _name: UpsSnapshot("OL", 100, 600, 20, 120.0)
    )
    rendered = status.render(cfg, color=False, now=0)
    assert "\033[" not in rendered


def test_status_sparkline_renders_from_history() -> None:
    spark = status._sparkline([100, 200, 150, 300, 250, 400])
    assert spark
    assert all(ch in status._BLOCKS for ch in spark)
    assert status._sparkline([50]) == ""  # too few points


def test_status_empty_config() -> None:
    cfg = Config(webhook_url="", upses={})
    assert "(no UPSes configured)" in status.render(cfg, color=False, now=0)
