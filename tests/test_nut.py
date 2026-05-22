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
    }
    monkeypatch.setattr(nut, "upsc_var", lambda _ups, key, timeout=10.0: values.get(key))
    s = read_snapshot("anything")
    assert s.status == "OB"
    assert s.charge == 73
    assert s.runtime_seconds == 612
    assert s.load == 15
    assert s.input_voltage == 117.5


def test_read_snapshot_handles_missing(monkeypatch) -> None:
    monkeypatch.setattr(nut, "upsc_var", lambda _ups, key, timeout=10.0: None)
    s = read_snapshot("gone")
    assert s == UpsSnapshot(None, None, None, None, None)
    assert s.on_battery is False
