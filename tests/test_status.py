from __future__ import annotations

import dataclasses
import os

from conftest import make_ups
from ups_orchestrator import status
from ups_orchestrator.config import Config, ConfigNotice
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


def test_status_healthy_config_has_no_degraded_block(monkeypatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr(
        status, "read_snapshot", lambda _name: UpsSnapshot("OL", 100, 600, 20, 120.0)
    )
    rendered = status.render(cfg, color=False, now=0)
    assert "DEGRADED" not in rendered


def test_status_shows_error_and_advisory_notices(monkeypatch) -> None:
    degraded = (
        ConfigNotice(severity="error", subject="mt", message="no serial device — disarmed"),
        ConfigNotice(severity="advisory", subject="spark", message="shutdown_cmd needs sudo"),
    )
    cfg = Config(
        webhook_url="", upses={"ups1": make_ups("ups1")}, degraded=degraded
    )
    monkeypatch.setattr(
        status, "read_snapshot", lambda _name: UpsSnapshot("OL", 100, 600, 20, 120.0)
    )
    rendered = status.render(cfg, color=False, now=0)
    assert "DEGRADED" in rendered
    assert "ERROR" in rendered and "mt" in rendered and "no serial device — disarmed" in rendered
    assert "ADVISORY" in rendered and "spark" in rendered and "shutdown_cmd needs sudo" in rendered


def test_status_degraded_block_appears_before_first_ups_card(monkeypatch) -> None:
    degraded = (ConfigNotice(severity="error", subject="mt", message="disarmed"),)
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")}, degraded=degraded)
    monkeypatch.setattr(
        status, "read_snapshot", lambda _name: UpsSnapshot("OL", 100, 600, 20, 120.0)
    )
    rendered = status.render(cfg, color=False, now=0)
    assert rendered.index("DEGRADED") < rendered.index("Test ups1")


def test_status_degraded_block_no_color_has_no_escape_codes(monkeypatch) -> None:
    degraded = (
        ConfigNotice(severity="error", subject="mt", message="disarmed"),
        ConfigNotice(severity="advisory", subject="spark", message="needs sudo"),
    )
    cfg = Config(webhook_url="", upses={}, degraded=degraded)
    rendered = status.render(cfg, color=False, now=0)
    assert "\033[" not in rendered


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


# --- MED-05 / MED-06: the degrade panel must stay readable and inert -----------


def _force_80_columns(monkeypatch) -> None:
    """Pin the rendered width, so the assertions do not depend on the runner's tty."""
    monkeypatch.setattr(
        status.shutil, "get_terminal_size", lambda fallback=(80, 24): os.terminal_size((80, 24))
    )

_LONG = (
    "governed by BOTH shutdown regimes: declared shutdown_method='native' and also an "
    "enabled shutdown_target 'mt' on UPS 'cyberpower'. The legacy target has been "
    "disabled. This machine remains ARMED and is now the single surviving authority — "
    "its own upsmon halts it on this primary's FSD and lives in that box's /etc, so no "
    "config change here can disarm it. Run 'monitor verify mt' to check that secondary."
)


def test_degraded_panel_wraps_instead_of_widening(monkeypatch) -> None:
    # MED-05. `_panel` sized the box to its longest line with no cap, and the notice
    # messages are deliberately ~500 characters, so the block rendered 600+ columns
    # wide: on an 80-column terminal every line wrapped into ~8 rows, the box drawing
    # was destroyed, the UPS cards were pushed off screen, and `status --watch` — which
    # assumes one logical line is one terminal row — ghosted every frame.
    _force_80_columns(monkeypatch)
    cfg = Config(
        webhook_url="",
        upses={},
        degraded=(ConfigNotice(severity="error", subject="mt", message=_LONG),),
    )

    lines = status.render(cfg, color=False, now=0).split("\n")

    assert max(status._vlen(x) for x in lines) <= 80
    assert _LONG.split(". ")[0] not in "\n".join(lines)  # it was wrapped, not truncated
    assert "monitor verify mt" in " ".join(x.strip("| ") for x in lines)  # nothing lost


def test_degraded_panel_borders_still_align_after_wrapping(monkeypatch) -> None:
    _force_80_columns(monkeypatch)
    cfg = Config(
        webhook_url="",
        upses={},
        degraded=(
            ConfigNotice(severity="error", subject="mt", message=_LONG),
            ConfigNotice(severity="advisory", subject="spark", message="short one"),
        ),
    )

    block = [x for x in status.render(cfg, color=False, now=0).split("\n") if x.strip()]
    boxed = [x for x in block if x.startswith(("+", "|"))]

    assert len({status._vlen(x) for x in boxed}) == 1


def test_control_characters_in_a_notice_are_neutralised() -> None:
    # MED-06. `_ANSI_RE` matches only SGR sequences, so `_vlen` neither strips nor
    # accounts for any other escape, and the subject/message are operator-authored:
    # JSON can encode any control character as \uXXXX. A machine named
    # "mt\x1b[2J\x1b[H" printed raw clears the screen and homes the cursor — a machine
    # name erasing the degrade banner that is reporting on it.
    cfg = Config(
        webhook_url="",
        upses={},
        degraded=(
            ConfigNotice(severity="error", subject="mt\x1b[2J\x1b[H", message="pwned\x07"),
        ),
    )

    rendered = status.render(cfg, color=False, now=0)

    assert "\x1b" not in rendered
    assert "\x07" not in rendered
    assert "mt?[2J?[H" in rendered  # neutralised, not silently dropped


def test_control_characters_in_a_ups_label_are_neutralised(monkeypatch) -> None:
    monkeypatch.setattr(
        status, "read_snapshot", lambda _name: UpsSnapshot("OL", 100, 600, 20, 120.0)
    )
    ups = make_ups("ups1")
    cfg = Config(
        webhook_url="",
        upses={"ups1": dataclasses.replace(ups, label="CP\x1b[2J")},
    )

    rendered = status.render(cfg, color=False, now=0)

    assert "\x1b" not in rendered
    assert "CP?[2J" in rendered
