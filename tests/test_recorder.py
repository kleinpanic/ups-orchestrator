from __future__ import annotations

from typing import Any, cast

from conftest import make_ups
from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot
from ups_orchestrator.recorder import DEFAULT_MAX_ROTATIONS, append_record, build_record


def test_build_record_includes_all_ups_fields(monkeypatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr("ups_orchestrator.recorder._boot_id", lambda: "boot-1")

    record = build_record(
        cfg,
        snapshot_reader=lambda _name: UpsSnapshot(
            "OL", 100, 120, 25, 119.0, output_voltage=119.0, realpower_nominal=900
        ),
    )

    assert record["boot_id"] == "boot-1"
    upses = cast(dict[str, dict[str, Any]], record["upses"])
    ups = upses["ups1"]
    assert ups["status"] == "OL"
    assert ups["estimated_load_watts"] == 225
    assert ups["load_margin_percent"] == 75
    assert ups["test_result"] is None
    assert ups["timer_shutdown"] is None


def test_append_record_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    append_record(path, {"a": 1}, max_bytes=1000)
    append_record(path, {"b": 2}, max_bytes=1000)

    assert path.read_text().splitlines() == ['{"a": 1}', '{"b": 2}']


def test_rotation_retains_requested_historical_segments(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    for number in range(5):
        append_record(path, {"number": number}, max_bytes=1, max_rotations=3)

    assert path.read_text().strip() == '{"number": 4}'
    assert (tmp_path / "samples.jsonl.1").read_text().strip() == '{"number": 3}'
    assert (tmp_path / "samples.jsonl.2").read_text().strip() == '{"number": 2}'
    assert (tmp_path / "samples.jsonl.3").read_text().strip() == '{"number": 1}'
    assert not (tmp_path / "samples.jsonl.4").exists()


def test_default_retention_is_twenty_segments(tmp_path) -> None:
    # The default deepened from 10 to 20; the deployed recorder.service passes
    # --max-rotations 20 to match, so both must move together.
    assert DEFAULT_MAX_ROTATIONS == 20
    path = tmp_path / "samples.jsonl"
    for number in range(25):
        append_record(path, {"number": number}, max_bytes=1)

    assert path.read_text().strip() == '{"number": 24}'
    assert (tmp_path / f"samples.jsonl.{DEFAULT_MAX_ROTATIONS}").exists()
    assert not (tmp_path / f"samples.jsonl.{DEFAULT_MAX_ROTATIONS + 1}").exists()
