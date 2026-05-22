from __future__ import annotations

from conftest import FakeNotifier, make_deps, make_ups, snap
from ups_orchestrator.config import R630Config
from ups_orchestrator.events import dispatch, fmt_duration
from ups_orchestrator.notify import Level
from ups_orchestrator.state import UpsState


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


def test_lowbatt_shuts_down_pi_when_enabled() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB LB", charge=4))
    dispatch("lowbatt", make_ups(shutdown_pi=True), UpsState(), deps)
    assert calls == ["pi"]
    assert notifier.sent[0].level is Level.CRITICAL


def test_lowbatt_defers_to_nut_when_disabled() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB LB", charge=4))
    dispatch("lowbatt", make_ups(shutdown_pi=False), UpsState(), deps)
    assert calls == []
    assert "NUT will shut" in notifier.sent[0].body


def test_tick_silent_when_online() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))
    dispatch("tick", make_ups(), UpsState(), deps)
    assert notifier.sent == []


def test_tick_countdown_when_on_battery() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB", runtime=420))
    dispatch("tick", make_ups(), UpsState(onbatt_since=1), deps)
    assert "still on battery" in notifier.sent[0].title


def test_tick_triggers_deferred_r630_after_delay() -> None:
    notifier = FakeNotifier()
    r630 = R630Config(enabled=True, host="h", user="u", delay_seconds=300)
    deps, calls = make_deps(notifier, snap("OB"), now=1000)
    state = UpsState(onbatt_since=600)  # 400s elapsed > 300s delay
    dispatch("tick", make_ups(r630=r630), state, deps)
    assert "ssh" in calls
    assert state.r630_shutdown_sent is True


def test_tick_no_r630_before_delay() -> None:
    notifier = FakeNotifier()
    r630 = R630Config(enabled=True, host="h", user="u", delay_seconds=300)
    deps, calls = make_deps(notifier, snap("OB"), now=1000)
    state = UpsState(onbatt_since=900)  # only 100s elapsed
    dispatch("tick", make_ups(r630=r630), state, deps)
    assert "ssh" not in calls


def test_unknown_event_returns_false() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))
    assert dispatch("nonsense", make_ups(), UpsState(), deps) is False
