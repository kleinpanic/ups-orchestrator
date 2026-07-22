"""Load-step drop detection (device-death hint) in the tick handler."""

from __future__ import annotations

from conftest import FakeNotifier, make_ups, snap
from ups_orchestrator.config import LoadStepPolicy
from ups_orchestrator.events import Deps, dispatch
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
    )
    return deps, notifier, log


def drops(log: EventLog) -> list[dict[str, object] | None]:
    return [data for event, data in log.events if event == "load_step_drop"]


def test_big_drop_logs_and_notifies() -> None:
    deps, notifier, log = deps_for([snap("OL", load=42), snap("OL", load=18)])
    ups, state = make_ups(), UpsState()
    dispatch("tick", ups, state, deps)
    dispatch("tick", ups, state, deps)
    (data,) = drops(log)
    assert data == {
        "previous_load": 42,
        "new_load": 18,
        "drop_points": 24,
        "estimated_watts_delta": 216,
    }
    assert len(notifier.sent) == 1
    assert "24 points" in notifier.sent[0].title
    assert state.last_load == 18


def test_small_drop_and_rise_are_quiet() -> None:
    deps, notifier, log = deps_for(
        [snap("OL", load=10), snap("OL", load=40), snap("OL", load=33)]
    )
    ups, state = make_ups(), UpsState()
    for _ in range(3):
        dispatch("tick", ups, state, deps)
    assert drops(log) == []
    assert notifier.sent == []
    assert state.last_load == 33


def test_disabled_policy_still_tracks_baseline() -> None:
    deps, notifier, log = deps_for(
        [snap("OL", load=42), snap("OL", load=10)],
        policy=LoadStepPolicy(enabled=False),
    )
    ups, state = make_ups(), UpsState()
    dispatch("tick", ups, state, deps)
    dispatch("tick", ups, state, deps)
    assert drops(log) == []
    assert notifier.sent == []
    assert state.last_load == 10


def test_cooldown_limits_notifications_not_events() -> None:
    deps, notifier, log = deps_for(
        [snap("OL", load=40), snap("OL", load=20), snap("OL", load=40), snap("OL", load=20)],
        policy=LoadStepPolicy(drop_percent=15, cooldown_seconds=600),
    )
    ups, state = make_ups(), UpsState()
    for _ in range(4):
        dispatch("tick", ups, state, deps)
    assert len(drops(log)) == 2
    assert len(notifier.sent) == 1


def test_missing_load_neither_crashes_nor_poisons_baseline() -> None:
    deps, notifier, log = deps_for(
        [snap("OL", load=None), snap("OL", load=40), snap("OL", load=20)]
    )
    ups, state = make_ups(), UpsState()
    for _ in range(3):
        dispatch("tick", ups, state, deps)
    (data,) = drops(log)
    assert data is not None and data["drop_points"] == 20
    assert len(notifier.sent) == 1


def test_state_roundtrip_keeps_load_fields() -> None:
    state = UpsState(last_load=37, last_load_step_notified=1234)
    restored = UpsState.from_dict(
        {"last_load": 37, "last_load_step_notified": 1234}
    )
    assert restored.last_load == state.last_load
    assert restored.last_load_step_notified == state.last_load_step_notified
