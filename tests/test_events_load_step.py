"""Load-step drop detection (device-death hint) in the tick handler."""

from __future__ import annotations

import json
from pathlib import Path

from conftest import FakeNotifier, make_ups, snap
from ups_orchestrator.config import LoadStepPolicy
from ups_orchestrator.events import Deps, _draw_sparkline, dispatch
from ups_orchestrator.nut import UpsSnapshot
from ups_orchestrator.state import UpsState


class EventLog:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, object] | None]] = []

    def __call__(
        self,
        event: str,
        _ups: object,
        _snap: UpsSnapshot | None,
        _message: str,
        data: dict[str, object] | None,
    ) -> None:
        self.events.append((event, data))


def deps_for(
    snapshots: list[UpsSnapshot],
    *,
    policy: LoadStepPolicy | None = None,
    now: int = 1000,
    sample_path: Path | None = None,
) -> tuple[Deps, FakeNotifier, EventLog]:
    notifier = FakeNotifier()
    log = EventLog()
    feed = list(snapshots)
    deps = Deps(
        notifier=notifier,
        read_snapshot=lambda _name: feed.pop(0),
        now=lambda: now,
        event_log=log,
        load_step=policy or LoadStepPolicy(),
        sample_path=sample_path,
    )
    return deps, notifier, log


def run_loads(
    loads: list[int | None],
    *,
    policy: LoadStepPolicy | None = None,
    sample_path: Path | None = None,
) -> tuple[UpsState, FakeNotifier, EventLog]:
    deps, notifier, log = deps_for(
        [snap("OL", load=load) for load in loads], policy=policy, sample_path=sample_path
    )
    ups, state = make_ups(), UpsState()
    for _ in loads:
        dispatch("tick", ups, state, deps)
    return state, notifier, log


def drops(log: EventLog) -> list[dict[str, object] | None]:
    return [data for event, data in log.events if event == "load_step_drop"]


def test_big_drop_logs_and_notifies() -> None:
    state, notifier, log = run_loads([42, 18])
    (data,) = drops(log)
    assert data == {
        "peak_load": 42,
        "new_load": 18,
        "drop_points": 24,
        "estimated_watts_delta": 216,
        "window": [42],
    }
    assert len(notifier.sent) == 1
    assert "24 points" in notifier.sent[0].title
    assert state.recent_loads == [18]


def test_straddled_collapse_still_trips() -> None:
    # A real regression: the UPS reported an intermediate value mid-decay, so
    # neither consecutive step (45->33, 33->25) clears the threshold alone.
    state, notifier, log = run_loads([45, 33, 25])
    (data,) = drops(log)
    assert data is not None
    assert data["peak_load"] == 45 and data["drop_points"] == 20
    assert len(notifier.sent) == 1


def test_collapse_does_not_refire_from_stale_peak() -> None:
    _, notifier, log = run_loads([45, 25, 24, 23], policy=LoadStepPolicy(cooldown_seconds=0))
    assert len(drops(log)) == 1
    assert len(notifier.sent) == 1


def test_small_drop_and_rise_are_quiet() -> None:
    state, notifier, log = run_loads([10, 40, 33, 30])
    assert drops(log) == []
    assert notifier.sent == []
    assert state.recent_loads == [10, 40, 33, 30]


def test_window_is_bounded_by_policy() -> None:
    state, _, _ = run_loads([1, 2, 3, 4, 5], policy=LoadStepPolicy(window_polls=3))
    assert state.recent_loads == [3, 4, 5]


def test_disabled_policy_still_tracks_baseline() -> None:
    state, notifier, log = run_loads([42, 10], policy=LoadStepPolicy(enabled=False))
    assert drops(log) == []
    assert notifier.sent == []
    assert state.recent_loads == [42, 10]


def test_cooldown_limits_notifications_not_events() -> None:
    state, notifier, log = run_loads(
        [40, 20, 40, 20], policy=LoadStepPolicy(drop_percent=15, cooldown_seconds=600)
    )
    assert len(drops(log)) == 2
    assert len(notifier.sent) == 1


def test_missing_load_neither_crashes_nor_poisons_baseline() -> None:
    _, notifier, log = run_loads([None, 40, 20])
    (data,) = drops(log)
    assert data is not None and data["drop_points"] == 20
    assert len(notifier.sent) == 1


def test_notification_includes_sparkline_when_samples_exist(tmp_path) -> None:
    samples = tmp_path / "samples.jsonl"
    with samples.open("w") as fh:
        for i, watts in enumerate([200, 250, 380, 390, 200]):
            fh.write(
                json.dumps(
                    {
                        "unix_time": 1000.0 + i,
                        "upses": {"ups1": {"estimated_load_watts": watts}},
                    }
                )
                + "\n"
            )
    _, notifier, _ = run_loads([44, 22], sample_path=samples)
    (note,) = notifier.sent
    assert "200–390 W" in note.body


def test_sparkline_handles_missing_or_empty_file(tmp_path) -> None:
    assert _draw_sparkline(tmp_path / "absent.jsonl", "ups1") == ""
    empty = tmp_path / "empty.jsonl"
    empty.write_text("")
    assert _draw_sparkline(empty, "ups1") == ""


def test_state_roundtrip_and_legacy_migration() -> None:
    restored = UpsState.from_dict({"recent_loads": [37, 40], "last_load_step_notified": 1234})
    assert restored.recent_loads == [37, 40]
    assert restored.last_load_step_notified == 1234
    legacy = UpsState.from_dict({"last_load": 31})
    assert legacy.recent_loads == [31]
