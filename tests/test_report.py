from __future__ import annotations

from conftest import make_ups
from ups_orchestrator.config import Config, MonitoredMachine, UpsConfig, UpsDevice
from ups_orchestrator.notify import Level
from ups_orchestrator.nut import UpsSnapshot
from ups_orchestrator.report import build_report, render_text


def test_build_report_includes_all_ups_load_and_voltage() -> None:
    cfg = Config(
        webhook_url="",
        upses={
            "ups1": make_ups("ups1"),
            "ups2": make_ups("ups2"),
        },
    )
    snaps = {
        "ups1": UpsSnapshot("OL", 100, 600, 50, 120.1, 119.8, 900),
        "ups2": UpsSnapshot("OL", 90, 300, 0, 121.0, 121.0, 810),
    }

    note = build_report(cfg, snapshot_reader=snaps.__getitem__)

    assert note.level is Level.INFO
    assert len(note.fields) == 2
    assert "Expected time before 0%: 10m 0s" in note.fields[0][1]
    assert "50% WATCH (~450 W / 900 W, 450 W free, 50% margin)" in note.fields[0][1]
    assert "120.1 V in / 119.8 V out" in note.fields[0][1]


def test_report_warns_on_cyberpower_online_discharge_state() -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})

    note = build_report(
        cfg,
        snapshot_reader=lambda _name: UpsSnapshot("OL DISCHRG", 100, 600, 10, 120.0),
    )

    assert note.level is Level.WARNING
    assert "ONLINE + DISCHARGING" in render_text(note)


def test_report_warns_on_high_load() -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})

    note = build_report(
        cfg,
        snapshot_reader=lambda _name: UpsSnapshot("OL", 100, 600, 80, 120.0, realpower_nominal=900),
    )

    assert note.level is Level.WARNING
    assert "80% HIGH" in render_text(note)


def test_report_surfaces_battery_health_alarm_and_selftest() -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    snap = UpsSnapshot(
        "OL",
        100,
        1800,
        22,
        117.5,
        output_voltage=118.0,
        realpower_nominal=900,
        input_voltage_nominal=120.0,
        battery_voltage=26.4,
        battery_voltage_nominal=24.0,
        battery_type="PbAcid",
        test_result="Battery test failed",
        alarm="Replace battery",
    )
    note = build_report(cfg, snapshot_reader=lambda _n: snap)
    text = render_text(note)

    assert note.level is Level.WARNING  # alarm + failed test → degraded
    assert "Battery health: 26.4 V · 110% of 24 V nominal · PbAcid" in text
    assert "702 W free" in text  # load headroom
    assert "nom 120" in text  # input nominal
    assert "Last self-test: Battery test failed" in text
    assert "⚠️ Alarm: Replace battery" in text
    assert "self-test FAILED" in text


# --- inventory + summary body -------------------------------------------------


def _inventory_cfg() -> Config:
    ups = UpsConfig(
        name="ups1",
        label="Network UPS",
        devices=(
            UpsDevice(name="router", kind="network"),
            UpsDevice(name="modem", kind="network"),
        ),
    )
    return Config(
        webhook_url="",
        upses={"ups1": ups},
        monitored_machines=(MonitoredMachine(name="spark", ups="ups1"),),
    )


def test_report_field_names_what_dies_with_the_ups() -> None:
    # The operator's complaint about the old embed was that it never said what
    # was actually at risk — only percentages.
    cfg = _inventory_cfg()
    snaps = {"ups1": UpsSnapshot("OL", 100, 600, 50, 120.1, 119.8, 900)}

    note = build_report(cfg, snapshot_reader=snaps.__getitem__)

    assert "Shuts down on battery: spark" in note.fields[0][1]
    assert "Also powers: router, modem" in note.fields[0][1]


def test_report_body_carries_live_numbers_not_boilerplate() -> None:
    cfg = _inventory_cfg()
    snaps = {"ups1": UpsSnapshot("OL", 100, 600, 50, 120.1, 119.8, 900)}

    note = build_report(cfg, snapshot_reader=snaps.__getitem__)

    assert "~450 W" in note.body
    assert "1 machine(s) set to shut down automatically" in note.body


def test_report_body_leads_with_total_comms_failure() -> None:
    # This is the state the host was actually in for two days while the embed
    # still read as a normal report.
    cfg = _inventory_cfg()
    snaps = {"ups1": UpsSnapshot(None, None, None, None, None)}

    note = build_report(cfg, snapshot_reader=snaps.__getitem__)

    assert "No communication with any UPS" in note.body
    assert note.level is Level.WARNING


def test_report_body_flags_on_battery_and_high_load() -> None:
    cfg = Config(
        webhook_url="",
        upses={"ups1": make_ups("ups1"), "ups2": make_ups("ups2")},
    )
    snaps = {
        "ups1": UpsSnapshot("OB DISCHRG", 80, 600, 50, 120.0, 119.0, 900),
        "ups2": UpsSnapshot("OL", 100, 600, 95, 120.0, 119.0, 900),
    }

    note = build_report(cfg, snapshot_reader=snaps.__getitem__)

    assert "ON BATTERY: ups1" in note.body
    assert "High load: ups2" in note.body
