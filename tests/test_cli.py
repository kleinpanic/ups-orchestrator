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


_SNAP = UpsSnapshot("OL", 100, 600, 12, 120.0, realpower_nominal=900)


def test_audit_command_prints(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr("ups_orchestrator.audit._journal_since", lambda _s: [])
    monkeypatch.setattr("ups_orchestrator.audit._list_boots", lambda: [])
    monkeypatch.setattr("ups_orchestrator.audit.read_snapshot", lambda _n: _SNAP)
    assert cli.main(["audit"]) == 0
    assert capsys.readouterr().out.strip()


def test_boot_audit_command(env_config, monkeypatch) -> None:
    monkeypatch.setattr("ups_orchestrator.audit._current_boot_id", lambda: "b1")
    monkeypatch.setattr("ups_orchestrator.audit._journal_current_boot", lambda: [])
    monkeypatch.setattr("ups_orchestrator.audit.read_snapshot", lambda _n: _SNAP)
    assert cli.main(["boot-audit"]) == 0


def test_notify_test_print(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr("ups_orchestrator.report.read_snapshot", lambda _n: _SNAP)
    assert cli.main(["notify-test", "--print"]) == 0
    assert "delivery test" in capsys.readouterr().out.lower()


def test_notify_test_send_without_webhook_returns_one(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setattr("ups_orchestrator.report.read_snapshot", lambda _n: _SNAP)
    rc = cli.main(["notify-test"])  # empty webhook → NullNotifier → ok=False
    assert "configured=False" in capsys.readouterr().out
    assert rc == 1


def test_logs_missing_files_report_not_found(env_config, capsys) -> None:
    for kind in ("events", "notifications", "samples"):
        assert cli.main(["logs", kind]) == 0
    assert "not found" in capsys.readouterr().out


def test_logs_tails_existing_file(env_config, tmp_path, capsys) -> None:
    (tmp_path / "events.jsonl").write_text('{"a": 1}\n{"a": 2}\n{"a": 3}\n')
    assert cli.main(["logs", "events", "--lines", "2"]) == 0
    out = capsys.readouterr().out
    assert '{"a": 3}' in out
    assert '{"a": 1}' not in out


def test_record_command_dispatches_with_args(env_config, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(_cfg, **kw: object) -> None:
        seen.update(kw)

    monkeypatch.setattr("ups_orchestrator.recorder.run", fake_run)
    assert cli.main(["record", "--interval", "0.5", "--max-rotations", "7"]) == 0
    assert seen["max_rotations"] == 7
    assert seen["interval"] == 0.5


def test_watch_command_runs_until_stopped(env_config, monkeypatch) -> None:
    # Drive one tick then stop by making the post-tick sleep raise the loop's exit.
    monkeypatch.setattr(cli, "dispatch", lambda *_a, **_k: None)
    ticks = {"n": 0}

    def fake_sleep(_secs: float) -> None:
        ticks["n"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", fake_sleep)
    # _cmd_watch does not catch KeyboardInterrupt, so it propagates out cleanly.
    with pytest.raises(KeyboardInterrupt):
        cli.main(["watch"])
    assert ticks["n"] == 1


def test_path_resolvers_fall_back_to_base(monkeypatch, tmp_path) -> None:
    for var in (
        "UPS_ORCH_CONFIG",
        "UPS_ORCH_STATE",
        "UPS_ORCH_SAMPLES",
        "UPS_ORCH_EVENT_LOG",
        "UPS_ORCH_NOTIFICATION_LOG",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(cli, "_ETC_CONFIG", tmp_path / "nope.json")
    monkeypatch.setattr(cli, "_VAR_STATE", tmp_path / "novar" / "state.json")
    monkeypatch.setattr(cli, "_BASE", tmp_path)
    assert cli._config_path() == tmp_path / "config.json"
    assert cli._state_path() == tmp_path / "state.json"


def test_selftest_without_creds_errors(env_config, monkeypatch) -> None:
    monkeypatch.delenv("UPS_NUT_ADMIN_USER", raising=False)
    monkeypatch.delenv("UPS_NUT_ADMIN_PASSWORD", raising=False)
    assert cli.main(["selftest", "ups1"]) == 2


def test_selftest_runs_and_alerts_on_failure(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr(cli, "read_snapshot", lambda _n: _SNAP)
    from ups_orchestrator import selftest

    monkeypatch.setattr(
        selftest,
        "run_selftest",
        lambda name, *_a, **_k: selftest.SelfTestResult(name, "failed", "Test failed"),
    )
    rc = cli.main(["selftest", "ups1"])
    assert rc == 1  # a problem outcome → non-zero
    assert "failed" in capsys.readouterr().out


def test_baseline_command(env_config, tmp_path, capsys) -> None:
    (tmp_path / "samples.jsonl").write_text(
        '{"unix_time": 0, "upses": {"ups1": {"estimated_load_watts": 0}}}\n'
        '{"unix_time": 1, "upses": {"ups1": {"estimated_load_watts": 200}}}\n'
        '{"unix_time": 2, "upses": {"ups1": {"estimated_load_watts": 300}}}\n'
    )
    assert cli.main(["baseline", "--hours", "999"]) == 0
    out = capsys.readouterr().out
    assert "draw baseline" in out and "U1" in out


def test_webui_command_invokes_serve(env_config, monkeypatch) -> None:
    seen: dict[str, object] = {}

    def fake_serve(_cfg, _path, *, host, port):
        seen["host"], seen["port"] = host, port

    monkeypatch.setattr("ups_orchestrator.webui.serve", fake_serve)
    assert cli.main(["webui", "--host", "0.0.0.0", "--port", "9001"]) == 0
    assert seen == {"host": "0.0.0.0", "port": 9001}


def test_control_beeper_mute_all(env_config, monkeypatch, capsys) -> None:
    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    calls: list[tuple[str, str]] = []

    def fake_upscmd(name, command, **_kw):
        calls.append((name, command))
        return 0, "OK", ""

    monkeypatch.setattr("ups_orchestrator.nut.upscmd", fake_upscmd)
    assert cli.main(["control", "beeper-mute"]) == 0
    assert calls == [("ups1", "beeper.mute")]
    assert "beeper-mute" in capsys.readouterr().out


def test_control_without_creds_errors(env_config, monkeypatch) -> None:
    monkeypatch.delenv("UPS_NUT_ADMIN_USER", raising=False)
    monkeypatch.delenv("UPS_NUT_ADMIN_PASSWORD", raising=False)
    assert cli.main(["control", "beeper-mute"]) == 2


def test_control_reports_failure(env_config, monkeypatch) -> None:
    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr("ups_orchestrator.nut.upscmd", lambda *_a, **_k: (1, "", "access denied"))
    assert cli.main(["control", "test-quick"]) == 1


class _RecordingNotifier:
    def __init__(self) -> None:
        self.sent: list = []

    def send(self, note):
        self.sent.append(note)
        from ups_orchestrator.notify import DeliveryResult

        return DeliveryResult(configured=True, ok=True)


def _patch_recorder(monkeypatch) -> _RecordingNotifier:
    rec = _RecordingNotifier()
    monkeypatch.setattr(cli, "build_notifier", lambda *a, **k: rec)
    return rec


def test_control_sends_discord_counterpart(env_config, monkeypatch) -> None:
    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr("ups_orchestrator.nut.upscmd", lambda *_a, **_k: (0, "OK", ""))
    rec = _patch_recorder(monkeypatch)
    assert cli.main(["control", "beeper-mute"]) == 0
    assert len(rec.sent) == 1
    note = rec.sent[0]
    assert "beeper-mute" in note.title and "1/1 OK" in note.title
    assert note.fields == [("U1", "OK")]


def test_control_no_notify_skips_discord(env_config, monkeypatch) -> None:
    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr("ups_orchestrator.nut.upscmd", lambda *_a, **_k: (0, "OK", ""))
    rec = _patch_recorder(monkeypatch)
    assert cli.main(["control", "beeper-mute", "--no-notify"]) == 0
    assert rec.sent == []


def test_control_failure_notifies_warning(env_config, monkeypatch) -> None:
    from ups_orchestrator.notify import Level

    monkeypatch.setenv("UPS_NUT_ADMIN_USER", "admin")
    monkeypatch.setenv("UPS_NUT_ADMIN_PASSWORD", "pw")
    monkeypatch.setattr("ups_orchestrator.nut.upscmd", lambda *_a, **_k: (1, "", "access denied"))
    rec = _patch_recorder(monkeypatch)
    assert cli.main(["control", "test-quick"]) == 1
    assert rec.sent[0].level is Level.WARNING
