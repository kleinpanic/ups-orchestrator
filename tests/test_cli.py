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
    # LO-C3: this used to patch `report.read_snapshot`, which `build_report` bound
    # as a DEFAULT ARGUMENT at def time — so the patch never took and the test
    # spawned a real `upsc`. The armed tripwire is what surfaced it. Patch the
    # snapshot source itself, which no default argument can capture around.
    _fake_ups(monkeypatch, "OL", 100, 600)
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
    _fake_ups(monkeypatch, "OL", 100, 600)  # see test_report_print_command (LO-C3)
    assert cli.main(["notify-test", "--print"]) == 0
    assert "delivery test" in capsys.readouterr().out.lower()


def test_notify_test_send_without_webhook_returns_one(env_config, monkeypatch, capsys) -> None:
    _fake_ups(monkeypatch, "OL", 100, 600)  # see test_report_print_command (LO-C3)
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


def test_power_dashboard_writes_out(env_config, monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("ups_orchestrator.dashboard.render_png", lambda *_a, **_k: b"PNGDATA")
    out = tmp_path / "d.png"
    assert cli.main(["power-dashboard", "--out", str(out)]) == 0
    assert out.read_bytes() == b"PNGDATA"


def test_power_dashboard_posts(env_config, monkeypatch) -> None:
    monkeypatch.setattr("ups_orchestrator.dashboard.render_png", lambda *_a, **_k: b"PNG")
    monkeypatch.setattr("ups_orchestrator.dashboard.post_png", lambda *_a, **_k: (True, 200))
    assert cli.main(["power-dashboard", "--post"]) == 0


def test_power_dashboard_nothing_to_do_returns_2(env_config, monkeypatch) -> None:
    monkeypatch.setattr("ups_orchestrator.dashboard.render_png", lambda *_a, **_k: b"PNG")
    assert cli.main(["power-dashboard"]) == 2


def test_power_dashboard_matplotlib_missing(env_config, monkeypatch, tmp_path) -> None:
    def _boom(*_a, **_k):
        raise ImportError("no matplotlib")

    monkeypatch.setattr("ups_orchestrator.dashboard.render_png", _boom)
    assert cli.main(["power-dashboard", "--out", str(tmp_path / "x.png")]) == 1


def test_send_dashboard_swallows_render_error(env_config, monkeypatch) -> None:
    from ups_orchestrator import cli as climod

    def _boom(*_a, **_k):
        raise RuntimeError("render blew up")

    monkeypatch.setattr("ups_orchestrator.dashboard.render_png", _boom)
    cfg = climod._load_config()
    ok, status = climod._send_dashboard(cfg, hours=24)
    assert ok is False and status == 0  # degrades, never raises


# =============================================================================
# 02-03 Task 3 — `remote-shutdown [ups] [--dry-run]` and `shutdown rehearse`
# (A-2 decisions 1 and 2; T-02-13, T-02-24, IW-07)
#
# Nothing here contacts a host, opens a device or writes outside tmp_path: every
# transport is a closure on the injected `Deps` runners.
# =============================================================================

from conftest import FakeNotifier, make_deps, make_ups, shutdown_policy, snap  # noqa: E402

_REHEARSAL = "logger -t ups-orchestrator PHASE2_REHEARSAL"


def _dry_run_config(
    cfg: Path,
    *,
    policy_enabled: bool = True,
    require_outage: bool = False,
    machines: list[dict] | None = None,
    second_ups: bool = False,
) -> None:
    upses: dict[str, object] = {
        "ups1": {
            "label": "U1",
            "shutdown_targets": [
                {
                    "name": "mt",
                    "kind": "remote",
                    "enabled": True,
                    "host": "mt",
                    "cmd": "sudo /sbin/shutdown -h now",
                }
            ],
        }
    }
    if second_ups:
        # HI-C1 needs two UPSes to tell "scoped to one" apart from "swept them all".
        upses["ups2"] = {"label": "U2"}
    cfg.write_text(
        json.dumps(
            {
                "upses": upses,
                "shutdown": {
                    "enabled": policy_enabled,
                    "require_power_outage": require_outage,
                    "min_on_battery_seconds": 0,
                    "external": {"enabled": True, "battery_below": 15, "runtime_below": 300},
                    "internal": {"enabled": True, "battery_below": 10, "runtime_below": 120},
                },
                "monitored_machines": machines or [],
            }
        )
    )


def _fake_ups(monkeypatch, status: str, charge: int, runtime: int) -> None:
    monkeypatch.setattr(
        "ups_orchestrator.nut.upsc_vars",
        lambda _n: {
            "ups.status": status,
            "battery.charge": str(charge),
            "battery.runtime": str(runtime),
        },
    )


# --- the _fire_target guard: the dry-run returns at the TOP (T-02-13) ---------


def test_fire_target_dry_run_returns_before_every_side_effect() -> None:
    from ups_orchestrator.config import ShutdownTarget
    from ups_orchestrator.events import _fire_target
    from ups_orchestrator.state import UpsState

    logged: list[str] = []
    target = ShutdownTarget(name="mt", kind="remote", enabled=True, host="mt", cmd="/sbin/poweroff")
    ups = make_ups("ups1", targets=(target,), shutdown_policy=shutdown_policy())
    notifier = FakeNotifier()
    s = snap("OB LB", charge=5, runtime=60)
    deps, calls = make_deps(notifier, s)
    deps.dry_run = True
    deps.event_log = lambda event, *_a: logged.append(event)
    state = UpsState(onbatt_since=900)

    _fire_target(ups, state, deps, target, s, "external shutdown allowed")

    assert calls == []  # no runner
    assert state.shutdowns_sent == []  # the dedupe key is not poisoned
    assert notifier.sent == []  # no real notification
    assert logged == []  # no event-log line


def test_fire_target_without_dry_run_still_fires() -> None:
    # The guard must be the flag, not an accident of the call site.
    from ups_orchestrator.config import ShutdownTarget
    from ups_orchestrator.events import _fire_target
    from ups_orchestrator.state import UpsState

    target = ShutdownTarget(name="mt", kind="remote", enabled=True, host="mt", cmd="/sbin/poweroff")
    ups = make_ups("ups1", targets=(target,), shutdown_policy=shutdown_policy())
    s = snap("OB LB", charge=5, runtime=60)
    deps, calls = make_deps(FakeNotifier(), s)
    state = UpsState(onbatt_since=900)
    _fire_target(ups, state, deps, target, s, "external shutdown allowed")
    assert calls == ["mt"] and state.shutdowns_sent == ["mt"]


# --- remote-shutdown --dry-run: reports the gate, bypasses nothing ------------


def test_remote_shutdown_dry_run_lists_targets_and_touches_nothing(
    env_config, monkeypatch, capsys, tmp_path
) -> None:
    _dry_run_config(env_config)
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    rec = _patch_recorder(monkeypatch)
    assert cli.main(["remote-shutdown", "ups1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "mt" in out and "ssh" in out and "sudo /sbin/shutdown -h now" in out
    assert "would fire: yes" in out
    assert rec.sent == []
    assert not (tmp_path / "events.jsonl").exists()
    assert not (tmp_path / "state.json").exists()


def test_remote_shutdown_dry_run_against_an_online_ups_still_lists_every_target(
    env_config, monkeypatch, capsys
) -> None:
    # The old bug was a blank screen; the alternative lie was a preview implying it
    # would fire. Neither: a FULL listing annotated with the concrete reason.
    _dry_run_config(env_config, require_outage=True)
    _fake_ups(monkeypatch, "OL", 100, 3600)
    assert cli.main(["remote-shutdown", "ups1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "mt" in out
    assert "would fire: no" in out and "not on battery" in out


def test_remote_shutdown_dry_run_reports_a_disabled_global_policy(
    env_config, monkeypatch, capsys
) -> None:
    # The live probe found shutdown.enabled false and the docs never explaining it.
    # This makes the disabled flag VISIBLE at the moment the operator asks.
    _dry_run_config(env_config, policy_enabled=False)
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    assert cli.main(["remote-shutdown", "ups1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "mt" in out
    assert "would fire: no" in out and "shutdown policy disabled" in out


def test_remote_shutdown_dry_run_lists_projected_machines(
    env_config, monkeypatch, capsys
) -> None:
    _dry_run_config(
        env_config,
        machines=[
            {
                "name": "spark",
                "ups": "ups1",
                "shutdown_method": "serial",
                "serial_device": "/dev/ttyUSB0",
                "serial_baud": 9600,
                "shutdown_cmd": "sudo /sbin/shutdown -h now",
            }
        ],
    )
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    assert cli.main(["remote-shutdown", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "spark" in out and "/dev/ttyUSB0" in out


def test_remote_shutdown_appears_in_the_usage_string(capsys, caplog) -> None:
    with caplog.at_level("ERROR"):
        assert cli.main([]) == 0
    assert "remote-shutdown" in caplog.text


# --- shutdown rehearse: non-destructive BY CONSTRUCTION (T-02-24) -------------


def _rehearse_config(cfg: Path) -> None:
    cfg.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {
                                "name": "pi",
                                "kind": "local",
                                "enabled": True,
                                "cmd": "/sbin/poweroff",
                            }
                        ],
                    }
                },
                # Deliberately the production state: rehearsal must work here.
                "shutdown": {"enabled": False},
                "monitored_machines": [
                    {
                        "name": "spark",
                        "ups": "ups1",
                        "shutdown_method": "serial",
                        "serial_device": "/dev/ttyUSB0",
                        "serial_baud": 9600,
                        "shutdown_cmd": "sudo /sbin/shutdown -h now -REALHALT",
                    },
                    {
                        "name": "mt",
                        "ups": "ups1",
                        "ssh": "mt",
                        "shutdown_method": "ssh",
                        "shutdown_cmd": "sudo /sbin/shutdown -h now -REALHALT",
                    },
                ],
            }
        )
    )


def _capture_runners(monkeypatch) -> list:
    """Replace Deps' transport defaults with recorders, via _build_deps."""
    seen: list = []
    real_build = cli._build_deps

    def _build(cfg, **kw):  # noqa: ANN001
        deps = real_build(cfg, **kw)
        deps.serial_shutdown = lambda t: (seen.append(("serial", t)), (0, "", ""))[1]
        deps.ssh_shutdown = lambda t: (seen.append(("ssh", t)), (0, "", ""))[1]
        deps.local_shutdown = lambda c: (seen.append(("local", c)), (0, "", ""))[1]
        return deps

    monkeypatch.setattr(cli, "_build_deps", _build)
    return seen


def test_rehearse_sends_the_hardcoded_logger_command_over_serial(
    env_config, monkeypatch, capsys, tmp_path
) -> None:
    _rehearse_config(env_config)
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "spark"]) == 0
    assert len(seen) == 1
    kind, target = seen[0]
    assert kind == "serial"
    assert target.cmd == _REHEARSAL
    assert '"' not in target.cmd  # quote-free, like _monitor_add demands
    assert target.device == "/dev/ttyUSB0" and target.baud == 9600
    out = capsys.readouterr().out
    assert "/dev/ttyUSB0" in out and "9600" in out
    assert "REALHALT" not in out  # the persisted shutdown_cmd is never read
    assert not (tmp_path / "state.json").exists()  # persists NOTHING


def test_rehearse_never_carries_the_persisted_shutdown_cmd(env_config, monkeypatch) -> None:
    _rehearse_config(env_config)
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "mt"]) == 0
    _kind, target = seen[0]
    assert target.cmd == _REHEARSAL
    assert "REALHALT" not in target.cmd


def test_rehearse_works_with_the_global_shutdown_policy_disabled(env_config, monkeypatch) -> None:
    # A-2 decision 2: gating this on `shutdown.enabled` would make it unusable in
    # exactly the state production is in — which is the state it exists to be used
    # in. Its safety is that the command cannot halt anything, not a policy flag.
    _rehearse_config(env_config)
    assert json.loads(env_config.read_text())["shutdown"]["enabled"] is False
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "spark"]) == 0
    assert seen


def test_rehearse_refuses_a_local_target(env_config, monkeypatch) -> None:
    _rehearse_config(env_config)
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "pi"]) == 2
    assert seen == []


def test_rehearse_requires_an_explicit_machine_name(env_config, monkeypatch) -> None:
    _rehearse_config(env_config)
    seen = _capture_runners(monkeypatch)
    with pytest.raises(SystemExit):
        cli.main(["shutdown", "rehearse"])  # never a sweep
    assert seen == []


def test_rehearse_refuses_an_unknown_machine(env_config, monkeypatch) -> None:
    _rehearse_config(env_config)
    _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "ghost"]) == 2


def test_rehearse_refuses_a_machine_with_no_push_transport(env_config, monkeypatch) -> None:
    env_config.write_text(
        json.dumps(
            {
                "upses": {"ups1": {"label": "U1"}},
                "monitored_machines": [
                    {"name": "spark", "ups": "ups1", "ssh": "spark", "shutdown_method": "native"}
                ],
            }
        )
    )
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "spark"]) == 2
    assert seen == []


def test_rehearse_refuses_an_option_shaped_legacy_user(env_config, monkeypatch) -> None:
    # HI-C3. `ssh_dest` builds f"{user}@{host}", so validating `host` alone left the
    # argv element that actually reaches ssh unchecked. `_rehearsal_target` also
    # force-enables the target, so a DISABLED legacy target is a live ssh sink here.
    env_config.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {
                                "name": "legacy",
                                "kind": "remote",
                                "enabled": False,
                                "host": "box",
                                "user": "-oProxyCommand=touch /tmp/pwn",
                            }
                        ],
                    }
                }
            }
        )
    )
    seen = _capture_runners(monkeypatch)

    assert cli.main(["shutdown", "rehearse", "legacy"]) == 2
    assert seen == []


def test_rehearse_still_accepts_an_ordinary_user_at_host(env_config, monkeypatch) -> None:
    # `_SSH_ALIAS_RE` permits the user@host shape, so the tighter check must not
    # refuse a legitimate operator spelling.
    env_config.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {
                                "name": "legacy",
                                "kind": "remote",
                                "enabled": True,
                                "host": "box",
                                "user": "root",
                            }
                        ],
                    }
                }
            }
        )
    )
    seen = _capture_runners(monkeypatch)

    assert cli.main(["shutdown", "rehearse", "legacy"]) == 0
    assert [kind for kind, _t in seen] == ["ssh"]


# --- IW-07: a failing projected push must not deadlock the local target -------


def test_a_failing_projected_push_still_lets_the_local_target_fire() -> None:
    # `local` targets are held until every enabled remote is in `shutdowns_sent`,
    # and `_fire_target` appends REGARDLESS of rc. That append is what makes the
    # hold undeadlockable — pinned here so nobody later "fixes" the ordering by
    # removing it and starves the watcher Pi's own poweroff behind a dead cable.
    from ups_orchestrator.config import MonitoredMachine, ShutdownTarget
    from ups_orchestrator.events import _run_shutdown_targets
    from ups_orchestrator.state import UpsState

    machine = MonitoredMachine(
        name="spark",
        ups="ups1",
        shutdown_method="serial",
        serial_device="/dev/ttyUSB0",
        serial_baud=9600,
        shutdown_cmd="sudo /sbin/shutdown -h now",
    )
    local = ShutdownTarget(name="pi", kind="local", enabled=True, cmd="/sbin/poweroff")
    ups = make_ups(
        "ups1",
        targets=(local,),
        shutdown_policy=shutdown_policy(internal_enabled=True),
    )
    s = snap("OB LB", charge=5, runtime=60)
    deps, calls = make_deps(FakeNotifier(), s, serial_rc=1, monitored_machines=(machine,))
    state = UpsState(onbatt_since=900)
    _run_shutdown_targets(ups, state, deps, s)
    assert calls == ["spark", "local"], "the local target starved behind a failing push"


# --- preview verdicts for targets the firing path would never reach -----------


def test_preview_annotates_a_disabled_target_without_implying_a_battery_wait(
    env_config, monkeypatch, capsys
) -> None:
    env_config.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {"name": "mt", "enabled": False, "host": "mt", "cmd": "/sbin/poweroff"}
                        ],
                    }
                },
                "shutdown": {"enabled": True, "external": {"enabled": True}},
            }
        )
    )
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    assert cli.main(["remote-shutdown", "ups1", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "would fire: no — target not enabled" in out


def test_preview_annotates_a_target_disarmed_at_load(env_config, monkeypatch, capsys) -> None:
    # A blank host is unfireable, so 02-06 disables the target. The preview must say
    # so rather than reporting the UPS's charge state as the reason.
    env_config.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {"name": "mt", "enabled": True, "host": "", "cmd": "/sbin/poweroff"}
                        ],
                    }
                },
                "shutdown": {"enabled": True, "external": {"enabled": True}},
            }
        )
    )
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    assert cli.main(["remote-shutdown", "ups1", "--dry-run"]) == 0
    assert "disarmed at load" in capsys.readouterr().out


def test_preview_reports_a_ups_with_no_resolved_targets(env_config, monkeypatch, capsys) -> None:
    _fake_ups(monkeypatch, "OL", 100, 3600)
    assert cli.main(["remote-shutdown", "--dry-run"]) == 0
    assert "no shutdown targets resolved" in capsys.readouterr().out


def test_preview_skips_an_unconfigured_ups(env_config, monkeypatch, caplog) -> None:
    _fake_ups(monkeypatch, "OL", 100, 3600)
    with caplog.at_level("WARNING"):
        assert cli.main(["remote-shutdown", "ghost", "--dry-run"]) == 0
    assert "unconfigured UPS" in caplog.text


def test_remote_shutdown_without_dry_run_takes_the_real_event_route(
    env_config, monkeypatch
) -> None:
    _dry_run_config(env_config, policy_enabled=False)
    _fake_ups(monkeypatch, "OB LB", 5, 60)
    rec = _patch_recorder(monkeypatch)
    assert cli.main(["remote-shutdown", "ups1"]) == 0
    # handle_remote_shutdown short-circuits on the disabled policy and says so.
    assert any("skipped" in n.title for n in rec.sent)


def test_remote_shutdown_dry_run_returns_1_when_the_config_cannot_be_loaded(
    env_config,
) -> None:
    env_config.write_text("{ not json")
    assert cli.main(["remote-shutdown", "--dry-run"]) == 1


# --- rehearse: the refusals ---------------------------------------------------


def test_shutdown_verb_without_rehearse_is_rc2(env_config, caplog) -> None:
    with caplog.at_level("ERROR"):
        assert cli.main(["shutdown"]) == 2
        assert cli.main(["shutdown", "now"]) == 2
    assert "shutdown rehearse" in caplog.text


def test_rehearse_returns_1_when_the_config_cannot_be_loaded(env_config) -> None:
    env_config.write_text("{ not json")
    assert cli.main(["shutdown", "rehearse", "spark"]) == 1


def _one_machine_config(cfg: Path, machine: dict) -> None:
    cfg.write_text(
        json.dumps({"upses": {"ups1": {"label": "U1"}}, "monitored_machines": [machine]})
    )


def test_rehearse_refuses_a_serial_record_with_no_device(env_config, monkeypatch) -> None:
    _one_machine_config(
        env_config,
        {"name": "spark", "ups": "ups1", "shutdown_method": "serial", "serial_baud": 9600},
    )
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "spark"]) == 2
    assert seen == []


def test_rehearse_refuses_to_guess_a_baud(env_config, monkeypatch, caplog) -> None:
    _one_machine_config(
        env_config,
        {
            "name": "spark",
            "ups": "ups1",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
            "serial_baud": "fast",
        },
    )
    seen = _capture_runners(monkeypatch)
    with caplog.at_level("ERROR"):
        assert cli.main(["shutdown", "rehearse", "spark"]) == 2
    assert "9600" in caplog.text
    assert seen == []


def test_rehearse_refuses_an_option_shaped_ssh_alias(env_config, monkeypatch) -> None:
    # The alias is config-sourced and becomes an argv element; validate at the sink.
    _one_machine_config(
        env_config,
        {"name": "mt", "ups": "ups1", "ssh": "-oProxyCommand=id", "shutdown_method": "ssh"},
    )
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "mt"]) == 2
    assert seen == []


def test_rehearse_reports_a_transport_failure_as_rc1(env_config, monkeypatch, capsys) -> None:
    _one_machine_config(
        env_config, {"name": "mt", "ups": "ups1", "ssh": "mt", "shutdown_method": "ssh"}
    )
    real_build = cli._build_deps

    def _build(cfg, **kw):  # noqa: ANN001
        deps = real_build(cfg, **kw)
        deps.ssh_shutdown = lambda _t: (255, "", "no route to host")
        return deps

    monkeypatch.setattr(cli, "_build_deps", _build)
    assert cli.main(["shutdown", "rehearse", "mt"]) == 1
    assert "FAIL" in capsys.readouterr().out


def test_rehearse_resolves_a_legacy_serial_shutdown_target(env_config, monkeypatch) -> None:
    # Not every rehearsable transport is a monitored machine; a legacy serial
    # target is the shape that makes the local-target refusal non-vacuous.
    env_config.write_text(
        json.dumps(
            {
                "upses": {
                    "ups1": {
                        "label": "U1",
                        "shutdown_targets": [
                            {
                                "name": "mt",
                                "kind": "serial",
                                "enabled": False,
                                "device": "/dev/ttyUSB1",
                                "baud": 9600,
                                "cmd": "sudo /sbin/shutdown -h now -REALHALT",
                            }
                        ],
                    }
                }
            }
        )
    )
    seen = _capture_runners(monkeypatch)
    assert cli.main(["shutdown", "rehearse", "mt"]) == 0
    kind, target = seen[0]
    assert kind == "serial" and target.device == "/dev/ttyUSB1"
    assert target.cmd == _REHEARSAL and "REALHALT" not in target.cmd


# --- HI-C1: the preview and the real command agree about SCOPE ----------------


def test_remote_shutdown_with_no_name_is_loud_not_a_silent_success(
    env_config, monkeypatch, caplog
) -> None:
    """`remote-shutdown` with no name evaluated NOTHING and returned 0.

    `_cmd_event`'s `list(cfg.upses)` fallback is gated on `event == "tick"`, so the
    manual verb resolved an empty target list, logged `No UPS name provided` and
    reported SUCCESS — the identical "silently did nothing" bug
    `_cmd_remote_shutdown`'s own docstring says it was written to fix, moved one
    argument to the left. An operator who validates with `--dry-run` and then runs
    it for real must not get a clean exit for a no-op.
    """
    _dry_run_config(env_config)
    monkeypatch.delenv("UPSNAME", raising=False)
    _fake_ups(monkeypatch, "OB LB", 5, 60)

    with caplog.at_level("ERROR"):
        rc = cli.main(["remote-shutdown"])

    assert rc == 2
    assert "No UPS name provided" in caplog.text


def test_nut_event_route_with_no_name_still_exits_zero(env_config, monkeypatch, caplog) -> None:
    """The other half: a non-zero exit on the NUT route wedges upssched's pipeline.

    `deploy/upssched-cmd.sh` invokes onbatt/lowbatt/remote_shutdown, so the two
    callers genuinely need different answers — which is why the loudness is keyed
    on the caller and not on the event name.
    """
    _dry_run_config(env_config)
    monkeypatch.delenv("UPSNAME", raising=False)
    _fake_ups(monkeypatch, "OB LB", 5, 60)

    with caplog.at_level("ERROR"):
        assert cli.main(["remote_shutdown"]) == 0
    assert "No UPS name provided" in caplog.text


def test_remote_shutdown_dry_run_honours_upsname_like_the_real_path(
    env_config, monkeypatch, capsys
) -> None:
    """The preview read `args.ups` directly and never consulted `$UPSNAME`.

    Under `upssched` — the only place `$UPSNAME` is set — the preview therefore
    reported every configured UPS while the real run touched exactly one.
    """
    _dry_run_config(env_config, second_ups=True)
    monkeypatch.setenv("UPSNAME", "ups2")
    _fake_ups(monkeypatch, "OB LB", 5, 60)

    assert cli.main(["remote-shutdown", "--dry-run"]) == 0
    out = capsys.readouterr().out

    assert "(ups2)" in out
    assert "(ups1)" not in out, "the preview must scope to $UPSNAME the way the real path does"


def test_remote_shutdown_dry_run_states_that_a_real_run_would_refuse(
    env_config, monkeypatch, capsys
) -> None:
    """The one remaining asymmetry is printed rather than left for the operator.

    Sweeping every UPS is still the useful answer to "what is configured?", but
    the real command with no name evaluates nothing and exits 2. Saying so is what
    stops the preview from promising what the real command will not do.
    """
    _dry_run_config(env_config)
    monkeypatch.delenv("UPSNAME", raising=False)
    _fake_ups(monkeypatch, "OB LB", 5, 60)

    assert cli.main(["remote-shutdown", "--dry-run"]) == 0
    out = capsys.readouterr().out

    assert "$UPSNAME is unset" in out
    assert "exits 2" in out


# --- F1: a degraded config must not kill the daemon through the notifier -------


def test_watch_survives_a_degraded_config_with_a_malformed_webhook_url(
    monkeypatch, tmp_path, caplog
) -> None:
    """The whole premise of RA-01, defeated through the notification path.

    `_notify_degraded` is the ONE `notifier.send` on the daemon's startup path
    outside every guard, and it fires ONLY when the config is degraded. A
    malformed webhook URL made `urllib.request.Request` raise ValueError — not an
    OSError, so uncaught — so `watch` never reached its poll loop and exited 1,
    `Restart=always` respawned it, and the box monitored NOTHING in a permanent
    restart loop.

    RA-01 replaced hard-fail with degrade-and-disarm precisely so a bad config
    could not stop monitoring. This is the regression test for that promise.
    """
    secret = "TOKENabcdef0123456789"
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps(
            {
                "discord_webhook_url": f"https//discord.com/api/webhooks/1/{secret}",  # no colon
                "upses": {"ups1": {"label": "U1"}},
                # A serial record with a blank `ups` is disarmed at load, so
                # `cfg.degraded` is non-empty and `_notify_degraded` fires.
                "monitored_machines": [
                    {
                        "name": "spark",
                        "ups": "",
                        "shutdown_method": "serial",
                        "serial_device": "/dev/ttyUSB0",
                        "serial_baud": 9600,
                    }
                ],
            }
        )
    )
    monkeypatch.delenv("UPS_DISCORD_WEBHOOK", raising=False)  # the file value must win
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(cfg))
    monkeypatch.setenv("UPS_ORCH_STATE", str(tmp_path / "state.json"))
    monkeypatch.setenv("UPS_ORCH_NOTIFICATION_LOG", str(tmp_path / "notify.jsonl"))
    monkeypatch.setattr(cli, "dispatch", lambda *_a, **_k: None)

    ticks = {"n": 0}

    def _stop_after_one_tick(_secs: float) -> None:
        ticks["n"] += 1
        raise KeyboardInterrupt

    monkeypatch.setattr(cli.time, "sleep", _stop_after_one_tick)

    with caplog.at_level("WARNING"), pytest.raises(KeyboardInterrupt):
        cli.main(["watch"])

    assert ticks["n"] == 1, "watch must reach its poll loop despite the degrade + bad webhook"
    assert secret not in caplog.text, "a webhook token must never reach the journal"


def test_watch_notification_log_never_records_the_webhook_token(monkeypatch, tmp_path) -> None:
    """`AuditedNotifier` persists `DeliveryResult.error` to notifications.jsonl.

    So an unredacted transport error puts the credential on DISK, not just in the
    journal — and that file outlives the process.
    """
    secret = "TOKENabcdef0123456789"
    url = f"https://discord.example.invalid/api/webhooks/1/{secret}"
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"discord_webhook_url": url, "upses": {"ups1": {"label": "U1"}}}))
    log = tmp_path / "notify.jsonl"
    monkeypatch.delenv("UPS_DISCORD_WEBHOOK", raising=False)
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(cfg))
    monkeypatch.setenv("UPS_ORCH_NOTIFICATION_LOG", str(log))

    import urllib.error

    import ups_orchestrator.notify as notify_mod

    monkeypatch.setattr(notify_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(
        notify_mod.urllib.request,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(urllib.error.URLError(f"cannot reach {url}")),
    )

    _fake_ups(monkeypatch, "OL", 100, 600)
    assert cli.main(["notify-test"]) == 1
    assert secret not in log.read_text()
