from __future__ import annotations

from conftest import FakeNotifier, make_deps, make_ups, snap
from ups_orchestrator.config import ShutdownTarget
from ups_orchestrator.events import dispatch, fmt_duration
from ups_orchestrator.notify import Level
from ups_orchestrator.state import UpsState


def _remote(name: str = "srv", **kw: object) -> ShutdownTarget:
    return ShutdownTarget(name=name, kind="remote", enabled=True, host="h", user="u", **kw)  # type: ignore[arg-type]


def _local(name: str = "pi", **kw: object) -> ShutdownTarget:
    return ShutdownTarget(name=name, kind="local", enabled=True, **kw)  # type: ignore[arg-type]


def test_fmt_duration() -> None:
    assert fmt_duration(0) == "0s"
    assert fmt_duration(65) == "1m 5s"
    assert fmt_duration(3725) == "1h 2m 5s"
    assert fmt_duration(None) == "unknown"


def test_onbatt_records_state_and_notifies() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), now=1000)
    state = UpsState()
    assert dispatch("onbatt", make_ups(), state, deps) is True
    assert state.onbatt_since == 1000
    assert state.shutdowns_sent == []
    assert notifier.sent[0].level is Level.WARNING
    assert "ON BATTERY" in notifier.sent[0].title


def test_online_reports_outage_duration() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"), now=1300)
    state = UpsState(onbatt_since=1000)
    dispatch("online", make_ups(), state, deps)
    assert state.onbatt_since is None
    assert notifier.sent[0].level is Level.SUCCESS
    assert ("Outage duration", "5m 0s") in notifier.sent[0].fields


def test_lowbatt_only_notifies() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB LB", charge=4))
    dispatch("lowbatt", make_ups(), UpsState(), deps)
    assert calls == []  # NUT does the real shutdown, not us
    assert notifier.sent[0].level is Level.CRITICAL


def test_tick_silent_when_online() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))
    dispatch("tick", make_ups(), UpsState(), deps)
    assert notifier.sent == []


def test_tick_countdown_when_on_battery() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB", runtime=420))
    state = UpsState(onbatt_since=1)
    dispatch("tick", make_ups(), state, deps)
    assert "still on battery" in notifier.sent[0].title


def test_tick_countdown_respects_cadence() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), now=1000, countdown_every=60)
    state = UpsState(onbatt_since=1, last_tick_notified=990)  # only 10s since last
    dispatch("tick", make_ups(), state, deps)
    assert notifier.sent == []


def test_tick_countdown_disabled() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), countdown_every=0)
    dispatch("tick", make_ups(), UpsState(onbatt_since=1), deps)
    assert notifier.sent == []


def test_target_fires_on_battery_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=18), countdown_every=0)
    state = UpsState(onbatt_since=1)
    dispatch("tick", make_ups(targets=(_remote(battery_below=20),)), state, deps)
    assert calls == ["srv"]
    assert "srv" in state.shutdowns_sent


def test_target_fires_on_runtime_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=90, runtime=100), countdown_every=0)
    dispatch(
        "tick", make_ups(targets=(_remote(runtime_below=120),)), UpsState(onbatt_since=1), deps
    )
    assert calls == ["srv"]


def test_target_not_fired_above_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=80, runtime=600), countdown_every=0)
    dispatch(
        "tick",
        make_ups(targets=(_remote(battery_below=20, runtime_below=120),)),
        UpsState(onbatt_since=1),
        deps,
    )
    assert calls == []


def test_serial_target_fires() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=40), countdown_every=0)
    serial = ShutdownTarget(
        name="r630", kind="serial", enabled=True, device="/dev/ttyUSB0", battery_below=50
    )
    dispatch("tick", make_ups(targets=(serial,)), UpsState(onbatt_since=1), deps)
    assert calls == ["r630"]


def test_serial_counts_as_remote_for_local_last() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (
        ShutdownTarget(name="r630", kind="serial", enabled=True, device="/dev/x", battery_below=50),
        _local("pi", battery_below=10),
    )
    dispatch("tick", make_ups(targets=targets), UpsState(onbatt_since=1), deps)
    assert calls == ["r630", "local"]  # serial (remote) before local


def test_local_fires_after_remote() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (_remote("srv", battery_below=50), _local("pi", battery_below=10))
    dispatch("tick", make_ups(targets=targets), UpsState(onbatt_since=1), deps)
    assert calls == ["srv", "local"]  # remote first, local last


def test_local_waits_while_remote_pending() -> None:
    notifier = FakeNotifier()
    # remote threshold not yet crossed (needs <=5), local's is (<=10) — local must wait
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (_remote("srv", battery_below=5), _local("pi", battery_below=10))
    dispatch("tick", make_ups(targets=targets), UpsState(onbatt_since=1), deps)
    assert calls == []


def test_remote_shutdown_forces_all_regardless_of_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=100))
    targets = (_remote("srv", battery_below=5), _local("pi", battery_below=5))
    dispatch("remote_shutdown", make_ups(targets=targets), UpsState(onbatt_since=1), deps)
    assert calls == ["srv", "local"]


def test_unknown_event_returns_false() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))
    assert dispatch("nonsense", make_ups(), UpsState(), deps) is False
