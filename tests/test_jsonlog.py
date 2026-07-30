from __future__ import annotations

from pathlib import Path

from ups_orchestrator.jsonlog import _rotate_locked, append_jsonl


def test_rotate_replaces_existing_rotation(tmp_path) -> None:
    # Regression: rotation must atomically replace an existing .1, not error or
    # depend on an unlink-then-rename TOCTOU.
    log = tmp_path / "log.jsonl"
    log.write_text("current\n")
    (tmp_path / "log.jsonl.1").write_text("stale\n")

    _rotate_locked(log, max_bytes=1, max_rotations=3)  # size >= 1 → rotate

    assert (tmp_path / "log.jsonl.1").read_text() == "current\n"
    assert not log.exists()


def test_rotate_noop_when_under_limit(tmp_path) -> None:
    log = tmp_path / "log.jsonl"
    log.write_text("x\n")
    _rotate_locked(log, max_bytes=10_000, max_rotations=3)
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


# --- these logs are the only record of what happened during an outage ---


def test_rotation_keeps_several_generations(tmp_path: Path) -> None:
    """One generation was the shortest retention in the project.

    The recorder keeps twenty; the shutdown_attempt/shutdown_result records — the ones
    that answer "did we push to mt, and when" — kept one. An outage burst crossing the
    threshold twice destroyed the evidence of the outage that produced it.
    """
    log = tmp_path / "events.jsonl"
    for i in range(400):
        append_jsonl(log, {"n": i}, max_bytes=200, max_rotations=3)

    generations = sorted(p.name for p in tmp_path.glob("events.jsonl.*") if p.suffix[1:].isdigit())
    assert generations == ["events.jsonl.1", "events.jsonl.2", "events.jsonl.3"], generations


def test_rotation_never_leaves_a_generation_empty(tmp_path: Path) -> None:
    """The unlocked stat-then-rename race left .1 holding a single line.

    A stats over-threshold and renames; A recreates the log and writes one record; B,
    which stat'd before A's rename, renames THAT one-record file over .1 — destroying
    the full generation. Under the lock each rotation carries a full file forward.
    """
    log = tmp_path / "events.jsonl"
    for i in range(300):
        append_jsonl(log, {"n": i}, max_bytes=500, max_rotations=3)

    for gen in sorted(tmp_path.glob("events.jsonl.[0-9]")):
        assert gen.stat().st_size > 100, f"{gen.name} holds almost nothing — a lost generation"


def test_the_lock_sidecar_is_not_mistaken_for_a_generation(tmp_path: Path) -> None:
    """`.lock` must not be picked up by the numeric-suffix rotation walkers."""
    log = tmp_path / "events.jsonl"
    append_jsonl(log, {"n": 1}, max_bytes=10_000)
    assert (tmp_path / "events.jsonl.lock").exists()
    numeric = [p for p in tmp_path.glob("events.jsonl.*") if p.suffix[1:].isdigit()]
    assert numeric == []
