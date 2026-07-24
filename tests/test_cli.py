from __future__ import annotations

import json
from pathlib import Path

import pytest

from ups_orchestrator import cli
from ups_orchestrator.nut import UpsSnapshot


@pytest.fixture
def env_config(monkeypatch, tmp_path: Path) -> Path:
    """A minimal config + isolated state/log paths under tmp."""
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"upses": {"ups1": {"label": "U1"}}}))
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(cfg))
    monkeypatch.setenv("UPS_ORCH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UPS_ORCH_EVENT_LOG", str(tmp_path / "events.jsonl"))
    monkeypatch.setenv("UPS_ORCH_NOTIFICATION_LOG", str(tmp_path / "notify.jsonl"))
    monkeypatch.setenv("UPS_ORCH_SAMPLES", str(tmp_path / "samples.jsonl"))
    return cfg


def test_main_no_args_returns_zero() -> None:
    assert cli.main([]) == 0


def test_load_config_missing_returns_none(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(tmp_path / "nope.json"))
    # A missing file falls through to the repo default; point that away too.
    monkeypatch.setattr(cli, "_ETC_CONFIG", tmp_path / "also-nope.json")
    monkeypatch.setattr(cli, "_BASE", tmp_path)
    assert cli._load_config() is None


def test_event_exit_zero_for_unconfigured_ups(env_config) -> None:
    # The NUT event path must ALWAYS exit 0 so a bad handler can't wedge upssched.
    assert cli.main(["onbatt", "ghost"]) == 0


def test_event_handler_exception_still_exits_zero(env_config, monkeypatch) -> None:
    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError("handler blew up")

    monkeypatch.setattr(cli, "dispatch", _boom)
    assert cli.main(["onbatt", "ups1"]) == 0


def test_status_command_renders(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "ups_orchestrator.status.read_snapshot",
        lambda _n: UpsSnapshot("OL", 100, 600, 12, 120.0, realpower_nominal=900),
    )
    assert cli.main(["status"]) == 0
    assert "U1" in capsys.readouterr().out


def test_report_print_command(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "ups_orchestrator.report.read_snapshot",
        lambda _n: UpsSnapshot("OL", 100, 600, 12, 120.0, realpower_nominal=900),
    )
    assert cli.main(["report", "--print"]) == 0
    assert "UPS load and runtime report" in capsys.readouterr().out
