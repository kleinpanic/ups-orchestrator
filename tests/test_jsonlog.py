from __future__ import annotations

from ups_orchestrator.jsonlog import _rotate, append_jsonl


def test_rotate_replaces_existing_rotation(tmp_path) -> None:
    # Regression: rotation must atomically replace an existing .1, not error or
    # depend on an unlink-then-rename TOCTOU.
    log = tmp_path / "log.jsonl"
    log.write_text("current\n")
    (tmp_path / "log.jsonl.1").write_text("stale\n")

    _rotate(log, max_bytes=1)  # size >= 1 → rotate

    assert (tmp_path / "log.jsonl.1").read_text() == "current\n"
    assert not log.exists()


def test_rotate_noop_when_under_limit(tmp_path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text("x\n")
    _rotate(log, max_bytes=10_000)
    assert log.read_text() == "x\n"
    assert not (tmp_path / "log.jsonl.1").exists()


def test_append_jsonl_rotates_then_writes(tmp_path) -> None:
    log = tmp_path / "log.jsonl"
    append_jsonl(log, {"a": 1}, max_bytes=1)  # first: file absent → no rotate
    append_jsonl(log, {"b": 2}, max_bytes=1)  # second: rotate current → .1, write

    assert '"b": 2' in log.read_text()
    assert '"a": 1' in (tmp_path / "log.jsonl.1").read_text()


def test_boot_id_falls_back_when_unreadable(monkeypatch) -> None:
    import ups_orchestrator.jsonlog as jl

    monkeypatch.setattr(jl.Path, "read_text", lambda _self: (_ for _ in ()).throw(OSError("nope")))
    assert jl.boot_id() == "unknown"


def test_base_record_has_envelope() -> None:
    from ups_orchestrator.jsonlog import base_record

    rec = base_record("notification")
    assert rec["kind"] == "notification"
    assert "time" in rec and "unix_time" in rec and "boot_id" in rec


def test_append_event_writes_record(tmp_path) -> None:
    from ups_orchestrator.jsonlog import append_event

    log = tmp_path / "events.jsonl"
    append_event(log, "onbatt", ups_name="ups1", ups_label="U1", message="hi")
    import json

    rec = json.loads(log.read_text().splitlines()[-1])
    assert rec["event"] == "onbatt" and rec["ups"] == "ups1" and rec["label"] == "U1"
    assert rec["snapshot"] is None  # no snapshot passed
