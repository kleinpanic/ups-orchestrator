from __future__ import annotations

import logging

from conftest import FakeNotifier, make_deps, make_ups, shutdown_policy, snap
from ups_orchestrator import events as events_mod
from ups_orchestrator.config import MonitoredMachine, ShutdownTarget
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
    state = UpsState(onbatt_since=1000, onbatt_notified=True)
    dispatch("online", make_ups(), state, deps)
    assert state.onbatt_since is None
    assert notifier.sent[0].level is Level.SUCCESS
    assert ("Outage duration", "5m 0s") in notifier.sent[0].fields


def test_tick_within_grace_does_not_page() -> None:
    # A fresh transfer sets onbatt_since but must not page inside the grace window
    # (this is what keeps blips and self-tests silent).
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), now=1000, countdown_every=0)
    state = UpsState()
    dispatch("tick", make_ups(), state, deps)
    assert notifier.sent == []
    assert state.onbatt_since == 1000
    assert state.onbatt_notified is False


def test_tick_pages_after_grace_once() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), now=1030, countdown_every=0)  # 30s >= 20 grace
    state = UpsState(onbatt_since=1000)
    dispatch("tick", make_ups(), state, deps)
    assert "ON BATTERY" in notifier.sent[0].title
    assert state.onbatt_notified is True
    dispatch("tick", make_ups(), state, deps)  # second poll must not re-page
    assert len(notifier.sent) == 1


def test_online_silent_when_outage_never_paged() -> None:
    # Power restored after a sub-grace blip: no ON BATTERY was sent, so no RESTORED.
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"), now=1005)
    state = UpsState(onbatt_since=1000)  # onbatt_notified defaults False
    dispatch("online", make_ups(), state, deps)
    assert notifier.sent == []
    assert state.onbatt_since is None


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
    state = UpsState(onbatt_since=1, onbatt_notified=True)
    dispatch("tick", make_ups(), state, deps)
    assert "still on battery" in notifier.sent[0].title


def test_tick_countdown_respects_cadence() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), now=1000, countdown_every=60)
    # only 10s since last countdown, and already paged
    state = UpsState(onbatt_since=1, last_tick_notified=990, onbatt_notified=True)
    dispatch("tick", make_ups(), state, deps)
    assert notifier.sent == []


def test_tick_countdown_disabled() -> None:
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OB"), countdown_every=0)
    dispatch("tick", make_ups(), UpsState(onbatt_since=1, onbatt_notified=True), deps)
    assert notifier.sent == []


def test_target_fires_on_battery_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=18), countdown_every=0)
    state = UpsState(onbatt_since=1, onbatt_notified=True)
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


# --- Phase 2: projecting monitored machines into ephemeral shutdown targets ----
#
# Every test below drives the real firing path with *injected* runners (conftest
# make_deps). Nothing here can open a serial device, run ssh, or halt a host.

# The live by-id path to mt's console; the line is 9600 baud, NOT 115200 (P2-08).
MT_DEVICE = (
    "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller_CJAQb152808-if00-port0"
)
MT_CMD = "/sbin/shutdown -h now"
SPARK_CMD = "sudo /sbin/shutdown -h +0"


def _mt(*, ups: str = "ups1", baud: int = 9600, name: str = "mt") -> MonitoredMachine:
    return MonitoredMachine(
        name=name,
        ups=ups,
        shutdown_method="serial",
        serial_device=MT_DEVICE,
        serial_baud=baud,
        shutdown_cmd=MT_CMD,
    )


def _spark(*, method: str = "ssh", ups: str = "ups1", name: str = "spark") -> MonitoredMachine:
    return MonitoredMachine(
        name=name,
        ups=ups,
        ssh="spark",
        shutdown_method=method,
        shutdown_cmd=SPARK_CMD,
    )


def _project(ups, machines) -> list[ShutdownTarget]:
    from ups_orchestrator.events import _machine_targets

    return list(_machine_targets(ups, machines))


def _low() -> object:
    """On battery and past the default external thresholds (15% / 300s)."""
    return snap("OB LB", charge=8, runtime=90)


def test_machine_targets_project_serial_and_ssh() -> None:
    targets = _project(make_ups("ups1"), (_mt(), _spark()))

    assert [t.name for t in targets] == ["mt", "spark"]
    serial, ssh = targets
    assert (serial.kind, serial.enabled, serial.device, serial.baud, serial.cmd) == (
        "serial",
        True,
        MT_DEVICE,
        9600,
        MT_CMD,
    )
    assert (ssh.kind, ssh.enabled, ssh.host, ssh.cmd) == ("remote", True, "spark", SPARK_CMD)


def test_machine_targets_native_exclusion() -> None:
    # spark's upsmon secondary self-shuts on the primary's FSD. Projecting it as
    # well would shut the box down twice (P2-06).
    assert _project(make_ups("ups1"), (_spark(method="native"),)) == []


def test_machine_targets_none_method_excluded() -> None:
    assert _project(make_ups("ups1"), (MonitoredMachine(name="idle", ups="ups1"),)) == []


def test_machine_targets_skip_other_ups() -> None:
    assert _project(make_ups("ups1"), (_mt(ups="ups2"),)) == []
    # ...and a host-suffixed UPS reference still matches its bare UPS name.
    assert [t.name for t in _project(make_ups("ups1"), (_mt(ups="ups1@localhost"),))] == ["mt"]


def test_machine_targets_duplicate_name_not_swallowed(caplog) -> None:
    # shutdowns_sent is keyed on target name, so a second "mt" would be dropped by
    # the dedupe with no trace. The projector drops it loudly instead.
    with caplog.at_level(logging.ERROR, logger="ups_orchestrator.events"):
        targets = _project(make_ups("ups1"), (_mt(), _mt()))

    assert [t.name for t in targets] == ["mt"]
    assert "mt" in caplog.text and "duplicate" in caplog.text.lower()


def test_machine_targets_ignore_disabled_legacy_same_name() -> None:
    # The migration shape: mt keeps a disabled legacy target and gains a machine
    # record. The disabled target never fires, so it must not block the projection.
    legacy = ShutdownTarget(name="mt", kind="serial", enabled=False, device="/dev/ttyUSB0")
    assert [t.name for t in _project(make_ups("ups1", targets=(legacy,)), (_mt(),))] == ["mt"]


def test_projection_fires_serial_before_ssh() -> None:
    # Serial is network-independent; ssh dies with the switch. Order must not depend
    # on the order the operator happened to declare the machines in.
    for machines in ((_mt(), _spark()), (_spark(), _mt())):
        notifier = FakeNotifier()
        deps, calls = make_deps(
            notifier, _low(), countdown_every=0, monitored_machines=machines
        )
        ups = make_ups("ups1", shutdown_policy=shutdown_policy())
        dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)
        assert calls == ["mt", "spark"]


def test_projection_keeps_local_last() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, _low(), countdown_every=0, monitored_machines=(_mt(), _spark())
    )
    ups = make_ups(
        "ups1",
        targets=(_local("pi"),),
        shutdown_policy=shutdown_policy(internal_enabled=True, internal_battery_below=10),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)
    assert calls == ["mt", "spark", "local"]


def test_build_deps_wires_monitored_machines() -> None:
    from ups_orchestrator.cli import _build_deps
    from ups_orchestrator.config import Config

    machines = (_mt(), _spark(method="native"))
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")}, monitored_machines=machines)

    assert _build_deps(cfg).monitored_machines == machines
