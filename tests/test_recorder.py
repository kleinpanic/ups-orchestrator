from __future__ import annotations

from typing import Any, cast

from conftest import make_ups
from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot
from ups_orchestrator.recorder import append_record, build_record


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


def test_append_record_writes_jsonl(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    append_record(path, {"a": 1}, max_bytes=1000)
    append_record(path, {"b": 2}, max_bytes=1000)

    assert path.read_text().splitlines() == ['{"a": 1}', '{"b": 2}']
