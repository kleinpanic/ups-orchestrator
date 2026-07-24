from __future__ import annotations

from conftest import FakeNotifier, make_deps, make_ups, shutdown_policy, snap
from ups_orchestrator import events as events_mod
from ups_orchestrator.config import ShutdownTarget
from ups_orchestrator.events import _default_serial_shutdown, dispatch, fmt_duration
from ups_orchestrator.notify import Level
from ups_orchestrator.state import UpsState


class _FakePort:
    """Serial port stand-in whose command write returns a fixed byte count."""

    def __init__(self, cmd_written: int) -> None:
        self._cmd_written = cmd_written

    def __enter__(self) -> _FakePort:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def write(self, data: bytes) -> int:
        return len(data) if data == b"\r" else self._cmd_written


def _serial_target() -> ShutdownTarget:
    return ShutdownTarget(  # type: ignore[arg-type]
        name="r630", kind="serial", enabled=True, device="/dev/ttyUSB0", cmd="poweroff"
    )


def test_serial_short_write_reports_failure(monkeypatch) -> None:
    monkeypatch.setattr(events_mod.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(events_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr("builtins.open", lambda *a, **k: _FakePort(cmd_written=0))

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert "short serial write" in err


def test_serial_full_write_succeeds(monkeypatch) -> None:
    payload_len = len(b"poweroff\n")
    monkeypatch.setattr(events_mod.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(events_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr("builtins.open", lambda *a, **k: _FakePort(cmd_written=payload_len))

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 0
    assert err == ""


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
    ups = make_ups(
        targets=(_remote(),),
        shutdown_policy=shutdown_policy(external_battery_below=20, external_runtime_below=None),
    )
    dispatch("tick", ups, state, deps)
    assert calls == ["srv"]
    assert "srv" in state.shutdowns_sent
    assert "shutdown attempt" in notifier.sent[0].title
    assert "shutdown sent" in notifier.sent[1].title


def test_target_fires_on_runtime_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=90, runtime=100), countdown_every=0)
    ups = make_ups(
        targets=(_remote(),),
        shutdown_policy=shutdown_policy(external_battery_below=None, external_runtime_below=120),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == ["srv"]


def test_shutdown_policy_disabled_by_default() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=4, runtime=30), countdown_every=0)
    dispatch("tick", make_ups(targets=(_remote(),)), UpsState(onbatt_since=1), deps)
    assert calls == []


def test_target_not_fired_when_runtime_is_healthy_even_if_battery_threshold_matches() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=18, runtime=1800), countdown_every=0)
    ups = make_ups(
        targets=(_remote(),),
        shutdown_policy=shutdown_policy(external_battery_below=20, external_runtime_below=300),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == []


def test_target_waits_for_minimum_outage_age() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=4, runtime=30), now=1050, countdown_every=0)
    ups = make_ups(
        targets=(_remote(),),
        shutdown_policy=shutdown_policy(min_on_battery_seconds=120),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1000), deps)
    assert calls == []


def test_serial_target_fires() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=40, runtime=100), countdown_every=0)
    serial = ShutdownTarget(name="r630", kind="serial", enabled=True, device="/dev/ttyUSB0")
    ups = make_ups(
        targets=(serial,),
        shutdown_policy=shutdown_policy(external_battery_below=50, external_runtime_below=120),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == ["r630"]


def test_serial_counts_as_remote_for_local_last() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (
        ShutdownTarget(name="r630", kind="serial", enabled=True, device="/dev/x"),
        _local("pi"),
    )
    ups = make_ups(
        targets=targets,
        shutdown_policy=shutdown_policy(
            internal_enabled=True,
            external_battery_below=50,
            external_runtime_below=None,
            internal_battery_below=10,
            internal_runtime_below=None,
        ),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == ["r630", "local"]  # serial (remote) before local


def test_local_fires_after_remote() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (_remote("srv"), _local("pi"))
    ups = make_ups(
        targets=targets,
        shutdown_policy=shutdown_policy(
            internal_enabled=True,
            external_battery_below=50,
            external_runtime_below=None,
            internal_battery_below=10,
            internal_runtime_below=None,
        ),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == ["srv", "local"]  # remote first, local last


def test_local_waits_while_remote_pending() -> None:
    notifier = FakeNotifier()
    # External group is enabled but not due; local must wait so remotes go first.
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (_remote("srv"), _local("pi"))
    ups = make_ups(
        targets=targets,
        shutdown_policy=shutdown_policy(
            internal_enabled=True,
            external_battery_below=5,
            external_runtime_below=None,
            internal_battery_below=10,
            internal_runtime_below=None,
        ),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == []


def test_internal_group_disabled_skips_local_fires_remote() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=8), countdown_every=0)
    targets = (_remote("srv"), _local("pi"))
    ups = make_ups(
        targets=targets,
        shutdown_policy=shutdown_policy(
            internal_enabled=False,
            external_battery_below=50,
            external_runtime_below=None,
        ),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1), deps)
    assert calls == ["srv"]


def test_remote_shutdown_respects_disabled_policy() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=100))
    targets = (_remote("srv"), _local("pi"))
    dispatch("remote_shutdown", make_ups(targets=targets), UpsState(onbatt_since=1), deps)
    assert calls == []
    assert "shutdown skipped" in notifier.sent[0].title


def test_remote_shutdown_does_not_bypass_close_to_empty_gate() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=100, runtime=1800))
    targets = (_remote("srv"), _local("pi"))
    ups = make_ups(
        targets=targets,
        shutdown_policy=shutdown_policy(internal_enabled=True),
    )
    dispatch("remote_shutdown", ups, UpsState(onbatt_since=1), deps)
    assert calls == []


def test_unknown_event_returns_false() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))
    assert dispatch("nonsense", make_ups(), UpsState(), deps) is False


class _Proc:
    def __init__(self, rc: int, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def test_ssh_shutdown_reports_returncode_and_dest(monkeypatch) -> None:
    from ups_orchestrator.events import _default_ssh_shutdown, ssh_dest

    captured: dict[str, object] = {}

    def fake_run(cmd, **_kw):
        captured["cmd"] = cmd
        return _Proc(255, "", "Permission denied")

    monkeypatch.setattr(events_mod.subprocess, "run", fake_run)
    t = ShutdownTarget(name="srv", kind="remote", enabled=True, host="h", user="u", cmd="poweroff")  # type: ignore[arg-type]
    rc, _out, err = _default_ssh_shutdown(t)
    assert rc == 255
    assert "Permission denied" in err
    assert ssh_dest(t) == "u@h"
    assert captured["cmd"][-2:] == ["u@h", "poweroff"]


def test_ssh_dest_uses_host_alias_when_no_user() -> None:
    from ups_orchestrator.events import ssh_dest

    t = ShutdownTarget(name="srv", kind="remote", enabled=True, host="mt", user="")  # type: ignore[arg-type]
    assert ssh_dest(t) == "mt"


def test_local_shutdown_reports_returncode(monkeypatch) -> None:
    from ups_orchestrator.events import _default_local_shutdown

    monkeypatch.setattr(
        events_mod.subprocess, "run", lambda *a, **k: _Proc(1, "", "no such command")
    )
    rc, _out, err = _default_local_shutdown("/sbin/shutdown -h now")
    assert rc == 1
    assert "no such command" in err
