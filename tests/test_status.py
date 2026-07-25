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


def test_vlen_ignores_ansi() -> None:
    assert status._vlen("abc") == 3
    assert status._vlen(f"{status._RED}abc{status._RESET}") == 3


def test_panel_borders_align_even_with_color() -> None:
    # Every rendered line must share the same visible width, colour or not.
    lines = status._panel(
        f"{status._BOLD}Title{status._RESET}",
        status._vlen(f"{status._BOLD}Title{status._RESET}"),
        [f"{status._CYAN}short{status._RESET}", "a much longer content line here"],
        use_color=True,
    )
    widths = {status._vlen(x) for x in lines}
    assert len(widths) == 1  # top, body, bottom all equal visible width


def test_panel_ascii_fallback_when_no_color() -> None:
    lines = status._panel("T", 1, ["body"], use_color=False)
    assert lines[0].startswith("+-") and lines[-1].startswith("+-")
    assert all("\033[" not in x for x in lines)


def test_battery_and_load_gauge_colors_by_threshold() -> None:
    assert status._battery_color(100) == status._GREEN
    assert status._battery_color(45) == status._YELLOW
    assert status._battery_color(15) == status._RED
    assert status._battery_color(None) == status._DIM
    assert status._load_color("OK") == status._GREEN
    assert status._load_color("WATCH") == status._YELLOW
    assert status._load_color("HIGH") == status._YELLOW
    assert status._load_color("CRIT") == status._RED
    assert status._load_color("OVER") == status._RED
