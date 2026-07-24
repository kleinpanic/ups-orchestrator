from __future__ import annotations

from ups_orchestrator import nut
from ups_orchestrator.nut import UpsSnapshot, read_snapshot


def test_snapshot_flags() -> None:
    assert UpsSnapshot("OB DISCHRG", 50, 300, 20, 119.0).on_battery is True
    assert UpsSnapshot("OL", 100, 0, 0, 120.0).on_battery is False
    assert UpsSnapshot("OB LB", 5, 60, 90, 0.0).low_battery is True
    assert UpsSnapshot(None, None, None, None, None).on_battery is False


def test_read_snapshot_parses_types(monkeypatch) -> None:
    values = {
        "ups.status": "OB",
        "battery.charge": "73",
        "battery.runtime": "612.0",
        "ups.load": "15",
        "input.voltage": "117.5",
        "output.voltage": "116.8",
        "ups.realpower.nominal": "900",
        "ups.test.result": "No test initiated",
        "ups.timer.shutdown": "-60",
        "ups.timer.start": "30",
        "ups.alarm": "none",
        "battery.voltage": "26.3",
        "battery.voltage.nominal": "24",
        "battery.type": "PbAcid",
        "input.voltage.nominal": "120",
        "driver.state": "quiet",
        "ups.beeper.status": "enabled",
        "ups.delay.shutdown": "20",
        "ups.delay.start": "30",
        "device.serial": "CYBMY7000053",
        "battery.charge.low": "10",
        "battery.charge.warning": "20",
        "battery.runtime.low": "300",
    }
    monkeypatch.setattr(nut, "upsc_vars", lambda _ups, timeout=10.0: values)
    s = read_snapshot("anything")
    assert s.status == "OB"
    assert s.charge == 73
    assert s.runtime_seconds == 612
    assert s.load == 15
    assert s.input_voltage == 117.5
    assert s.output_voltage == 116.8
    assert s.realpower_nominal == 900
    assert s.estimated_load_watts == 135
    assert s.test_result == "No test initiated"
    assert s.timer_shutdown == -60
    assert s.timer_start == 30
    assert s.alarm == "none"
    assert s.battery_voltage == 26.3
    assert s.battery_voltage_nominal == 24.0
    assert s.battery_type == "PbAcid"
    assert s.input_voltage_nominal == 120.0
    assert s.driver_state == "quiet"
    assert s.beeper_status == "enabled"
    assert s.delay_shutdown == 20
    assert s.delay_start == 30
    assert s.device_serial == "CYBMY7000053"
    assert (s.charge_low, s.charge_warning, s.runtime_low) == (10, 20, 300)


def test_derived_voltage_and_headroom_metrics() -> None:
    s = UpsSnapshot(
        "OL",
        100,
        1800,
        22,
        117.5,
        realpower_nominal=900,
        battery_voltage=26.4,
        battery_voltage_nominal=24.0,
        input_voltage_nominal=120.0,
    )
    assert s.battery_voltage_percent == 110  # 26.4/24
    assert s.input_voltage_percent == 98  # 117.5/120
    assert s.load_headroom_watts == 702  # 900 - 198
    # Absent inputs degrade to None, never crash.
    bare = UpsSnapshot(None, None, None, None, None)
    assert bare.battery_voltage_percent is None
    assert bare.input_voltage_percent is None
    assert bare.load_headroom_watts is None


def test_read_snapshot_handles_missing(monkeypatch) -> None:
    monkeypatch.setattr(nut, "upsc_vars", lambda _ups, timeout=10.0: {})
    s = read_snapshot("gone")
    assert s == UpsSnapshot(None, None, None, None, None)
    assert s.on_battery is False


def test_upsc_vars_uses_one_process_and_parses_all_fields(monkeypatch) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "ups.status: OL\nbattery.charge: 100\nups.test.result: No test initiated\n"

    def fake_run(args, **_kwargs):
        calls.append(args)
        return Result()

    monkeypatch.setattr(nut.subprocess, "run", fake_run)
    assert nut.upsc_vars("ups1") == {
        "ups.status": "OL",
        "battery.charge": "100",
        "ups.test.result": "No test initiated",
    }
    assert calls == [[nut.UPSC_BIN, "ups1"]]


def test_load_assessment_thresholds() -> None:
    assert UpsSnapshot("OL", 100, 600, 49, 120.0).load_level == "OK"
    assert UpsSnapshot("OL", 100, 600, 50, 120.0).load_level == "WATCH"
    assert UpsSnapshot("OL", 100, 600, 75, 120.0).load_level == "HIGH"
    assert UpsSnapshot("OL", 100, 600, 90, 120.0).load_level == "CRIT"
    assert UpsSnapshot("OL", 100, 600, 100, 120.0).load_level == "OVER"
    snap = UpsSnapshot("OL", 100, 600, 29, 120.0, realpower_nominal=900)
    assert snap.load_margin_percent == 71
    assert snap.estimated_load_watts == 261
    assert snap.load_is_high is False


def test_upscmd_success_and_failure(monkeypatch) -> None:
    class Result:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    calls = []

    def fake_run(args, **_kw):
        calls.append(args)
        return Result(0, "OK\n", "")

    monkeypatch.setattr(nut.subprocess, "run", fake_run)
    rc, out, err = nut.upscmd("ups1", "test.battery.start.quick", user="admin", password="pw")
    assert rc == 0 and out == "OK"
    assert calls[0][:1] == [nut.UPSCMD_BIN]
    assert "admin" in calls[0] and "pw" in calls[0]
    assert calls[0][-2:] == ["ups1", "test.battery.start.quick"]


def test_upscmd_handles_oserror(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise OSError("upscmd missing")

    monkeypatch.setattr(nut.subprocess, "run", boom)
    rc, _out, err = nut.upscmd("ups1", "x", user="u", password="p")
    assert rc == 1 and "upscmd missing" in err
