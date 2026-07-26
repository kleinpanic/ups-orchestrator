from __future__ import annotations

import fcntl
import logging
import os
import stat
import subprocess
from dataclasses import replace

from conftest import FakeNotifier, make_deps, make_ups, shutdown_policy, snap
from ups_orchestrator import events as events_mod
from ups_orchestrator.config import ConfigNotice, MonitoredMachine, ShutdownTarget
from ups_orchestrator.events import (
    _default_local_shutdown,
    _default_serial_shutdown,
    _default_ssh_shutdown,
    dispatch,
    fmt_duration,
)
from ups_orchestrator.notify import Level
from ups_orchestrator.state import UpsState

SERIAL_DEVICE = "/dev/ttyUSB0"


class _Proc:
    """Minimal ``CompletedProcess`` stand-in: a return code and two streams."""

    def __init__(self, rc: int, out: str = "", err: str = "") -> None:
        self.returncode = rc
        self.stdout = out
        self.stderr = err


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


_SENTINEL_FD = 987654  # never a real descriptor; the fdopen fake keys on it
_CHAR_DEVICE_MODE = stat.S_IFCHR | 0o660


class _SerialWiring:
    """What the serial runner actually did — no real device is ever involved."""

    def __init__(self) -> None:
        self.stty_argv: list[list[str]] = []
        self.opened: list[tuple[str, int]] = []  # os.open(path, flags)
        self.blocking_opened: list[str] = []  # builtins.open — must stay empty (T-02-25)
        self.flags_set: list[int] = []  # fcntl F_SETFL history
        self.closed: list[int] = []  # descriptors handed back to os.close


class _FakeFcntl:
    """Stand-in for the ``fcntl`` module, tracking the descriptor's flag word."""

    F_GETFL = fcntl.F_GETFL
    F_SETFL = fcntl.F_SETFL

    def __init__(self, wiring: _SerialWiring, initial: int, error: OSError | None = None) -> None:
        self._wiring = wiring
        self._flags = initial
        self._error = error

    def fcntl(self, _fd: int, op: int, arg: int = 0) -> int:
        if op == self.F_GETFL:
            return self._flags
        if self._error is not None:
            raise self._error
        self._flags = arg
        self._wiring.flags_set.append(arg)
        return 0


def _wire_serial(
    monkeypatch,
    *,
    device: str = SERIAL_DEVICE,
    cmd_written: int | None = None,
    stty: _Proc | None = None,
    run_raises: BaseException | None = None,
    st_mode: int = _CHAR_DEVICE_MODE,
    stat_error: OSError | None = None,
    fcntl_error: OSError | None = None,
) -> _SerialWiring:
    """Drive ``_default_serial_shutdown`` against fakes only.

    ``os.stat``/``os.open``/``os.fdopen`` pass through untouched for any path or
    descriptor that is not this fake device, so patching them cannot disturb the rest
    of the test session. ``builtins.open`` stays wired as a TRIPWIRE: the runner must
    reach the device through the non-blocking ``os.open``, never through a blocking
    ``open`` that would wait forever on a console cable with no DCD.

    ``cmd_written=None`` means the port is expected never to be opened. The attempt is
    RECORDED rather than raised: the runner converts every exception into a failure
    tuple, so a probe that raised would be swallowed and read as an ordinary failure.
    Assert on ``wiring.opened`` instead.
    """
    wiring = _SerialWiring()
    completed = _Proc(0) if stty is None else stty
    real_stat, real_open, real_fdopen = os.stat, os.open, os.fdopen
    real_close = os.close

    def fake_stat(path, *a, **k):
        if path != device:
            return real_stat(path, *a, **k)
        if stat_error is not None:
            raise stat_error
        return os.stat_result((st_mode, 0, 0, 1, 0, 0, 0, 0, 0, 0))

    def fake_run(argv, **_kw):
        wiring.stty_argv.append(list(argv))
        if run_raises is not None:
            raise run_raises
        return completed

    def fake_os_open(path, flags, *a):
        if path != device:
            return real_open(path, flags, *a)
        wiring.opened.append((path, flags))
        return _SENTINEL_FD

    def fake_fdopen(fd, *a, **k):
        if fd != _SENTINEL_FD:
            return real_fdopen(fd, *a, **k)
        return _FakePort(cmd_written or 0)

    def fake_close(fd):
        if fd != _SENTINEL_FD:
            return real_close(fd)
        wiring.closed.append(fd)
        return None

    def fake_blocking_open(path, *_a, **_k):
        wiring.blocking_opened.append(path)
        return _FakePort(cmd_written or 0)

    monkeypatch.setattr(events_mod.subprocess, "run", fake_run)
    monkeypatch.setattr(events_mod.time, "sleep", lambda _s: None)
    monkeypatch.setattr(events_mod.os, "stat", fake_stat)
    monkeypatch.setattr(events_mod.os, "open", fake_os_open)
    monkeypatch.setattr(events_mod.os, "fdopen", fake_fdopen)
    monkeypatch.setattr(events_mod.os, "close", fake_close)
    monkeypatch.setattr(
        events_mod,
        "fcntl",
        _FakeFcntl(wiring, os.O_WRONLY | os.O_NONBLOCK, fcntl_error),
    )
    monkeypatch.setattr("builtins.open", fake_blocking_open)
    return wiring


def _serial_target(*, baud: int | None = 9600) -> ShutdownTarget:
    return ShutdownTarget(  # type: ignore[arg-type]
        name="r630",
        kind="serial",
        enabled=True,
        device=SERIAL_DEVICE,
        baud=baud,
        cmd="poweroff",
    )


def test_serial_short_write_reports_failure(monkeypatch) -> None:
    _wire_serial(monkeypatch, cmd_written=0)

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert "short serial write" in err


def test_serial_full_write_succeeds(monkeypatch) -> None:
    _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 0
    assert err == ""


# --- HI-06 (narrowed): a LOCAL line-configuration failure is reported ---------
#
# What is proven here is that `stty` rejected the configuration of the LOCAL tty.
# Nothing in this section observes the far end: `stty -F <dev> <rate>` returns 0 for
# 9600, 19200, 115200 *and* 0 alike, so a MISMATCHED far-end speed is not detectable
# by this transport at all (deferred as OQ-02). Only a MALFORMED rate, or a path that
# cannot be configured, produces the non-zero return code these tests pin.


def test_serial_stty_failure_names_device_and_declared_baud(monkeypatch) -> None:
    wiring = _wire_serial(monkeypatch, stty=_Proc(1, "", "stty: invalid argument '9600'"))

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert "9600" in err
    assert "invalid argument" in err
    assert wiring.opened == []  # nothing is written to a line that could not be configured


def test_serial_stty_failure_does_not_open_the_port(monkeypatch) -> None:
    wiring = _wire_serial(monkeypatch, cmd_written=None, stty=_Proc(1, "", "boom"))

    _default_serial_shutdown(_serial_target())

    assert wiring.opened == []


def test_serial_declared_baud_reaches_stty_argv_and_the_failure_message(monkeypatch) -> None:
    # The operator's declared rate is passed verbatim and echoed back on failure — the
    # orchestrator never substitutes one (P2-08, as narrowed).
    wiring = _wire_serial(monkeypatch, stty=_Proc(1, "", "unsupported"))

    rc, _out, err = _default_serial_shutdown(_serial_target(baud=19200))

    assert rc == 1
    assert wiring.stty_argv == [["stty", "-F", SERIAL_DEVICE, "19200", "raw", "-echo"]]
    assert "19200" in err


def test_serial_zero_return_still_reaches_the_success_path(monkeypatch) -> None:
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"), stty=_Proc(0))

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert (rc, err) == (0, "")
    assert [path for path, _flags in wiring.opened] == [SERIAL_DEVICE]


# --- MED-10 transport half + T-02-25: what is opened, and how ------------------


def test_serial_refuses_a_path_that_is_not_a_character_device(monkeypatch) -> None:
    # A serial_device typo landing on a regular file would be TRUNCATED by the "wb"
    # open, have the shutdown command written into it, and report success. 02-06's
    # config-side /dev/ prefix check does not cover this: a path under /dev/ can still
    # be a regular file, and a hand-constructed target never passes through it at all.
    wiring = _wire_serial(monkeypatch, st_mode=stat.S_IFREG | 0o644)

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert "character device" in err
    assert wiring.stty_argv == []  # no stty against a regular file either
    assert wiring.opened == []
    assert wiring.blocking_opened == []


def test_serial_character_device_is_unaffected_by_the_guard(monkeypatch) -> None:
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"), st_mode=_CHAR_DEVICE_MODE)

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert (rc, err) == (0, "")
    assert wiring.stty_argv and [p for p, _f in wiring.opened] == [SERIAL_DEVICE]


def test_serial_refuses_an_unparseable_declared_baud(monkeypatch) -> None:
    # 02-06's strict parser yields None for a declared-but-unparseable baud. Rendering
    # that into the argv would run `stty -F <dev> None`.
    wiring = _wire_serial(monkeypatch)

    rc, _out, err = _default_serial_shutdown(_serial_target(baud=None))

    assert rc == 1
    assert "baud" in err
    assert "None" not in "".join(a for argv in wiring.stty_argv for a in argv)
    assert wiring.stty_argv == []
    assert wiring.opened == []


def test_serial_missing_device_keeps_its_existing_failure_shape(monkeypatch) -> None:
    wiring = _wire_serial(
        monkeypatch,
        stat_error=FileNotFoundError(2, "No such file or directory"),
    )

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert wiring.stty_argv == []
    assert wiring.opened == []


def test_serial_opens_non_blocking_and_then_clears_the_flag(monkeypatch) -> None:
    # T-02-25. `stty raw` does not set clocal and the kernel's default termios leaves
    # CLOCAL clear, so a BLOCKING open on a tty waits for DCD — which the 3-wire
    # TX/RX/GND console cable that is mt's topology never asserts. handle_tick would
    # never return, the poll loop would wedge, all three UPSes would stop being polled,
    # and Restart=always would never fire because the process is still alive. A
    # regression to a plain blocking open cannot be caught by a test that would simply
    # hang, so the flags are asserted directly.
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    rc, _out, _err = _default_serial_shutdown(_serial_target())

    assert rc == 0
    assert len(wiring.opened) == 1
    _path, flags = wiring.opened[0]
    assert flags & os.O_NONBLOCK  # the DCD wait is bypassed
    assert flags & os.O_WRONLY == os.O_WRONLY
    # ...and the flag is cleared straight afterwards, so the blocking write semantics
    # the short-write guard depends on are preserved.
    assert wiring.flags_set and not (wiring.flags_set[-1] & os.O_NONBLOCK)
    assert wiring.blocking_opened == []  # never through a blocking builtins.open


def test_serial_closes_the_descriptor_when_clearing_the_flag_fails(monkeypatch) -> None:
    # The descriptor is already open by then, so failing to clear the flag must not
    # leak it — the poll loop runs this every tick for the life of an outage.
    wiring = _wire_serial(
        monkeypatch,
        cmd_written=len(b"poweroff\n"),
        fcntl_error=OSError(9, "Bad file descriptor"),
    )

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert wiring.closed == [_SENTINEL_FD]


# --- T-02-24: a transport runner returns a failure tuple, it never raises ------
#
# `state.shutdowns_sent.append` sits AFTER the runner call, so a RAISING runner never
# marks the target sent, unwinds the whole tick, and the local targets are never
# reached. `cyberpower` powers BOTH mt and eulerpi5 (this host), so a hung push to mt
# starves the orchestrator's own poweroff on the battery the two of them share.


def test_serial_runner_returns_failure_when_stty_times_out(monkeypatch) -> None:
    # TimeoutExpired is NOT an OSError, so it escaped the old `except OSError`.
    wiring = _wire_serial(
        monkeypatch, run_raises=subprocess.TimeoutExpired(cmd="stty", timeout=5)
    )

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert wiring.opened == []


def test_ssh_runner_returns_failure_when_it_times_out(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=20)

    monkeypatch.setattr(events_mod.subprocess, "run", boom)
    target = ShutdownTarget(name="srv", kind="remote", enabled=True, host="h", user="u")  # type: ignore[arg-type]

    rc, _out, err = _default_ssh_shutdown(target)

    assert rc == 1
    assert "ssh" in err
    assert "u@h" in err


def test_ssh_argv_terminates_option_parsing_before_the_destination(monkeypatch) -> None:
    # BL-01 at the sink. `config.validate_legacy_targets` disarms an option-shaped
    # host/user at load, but a hand-constructed target never passes through the
    # validator — so an argv element that ssh would read as `-oProxyCommand=...` has to
    # be un-readable as an option in the first place. `--` is what does that.
    seen: list[list[str]] = []

    class _Done:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        events_mod.subprocess, "run", lambda argv, **_kw: (seen.append(list(argv)), _Done())[1]
    )
    target = ShutdownTarget(  # type: ignore[arg-type]
        name="evil", kind="remote", enabled=True, host="-oProxyCommand=touch /tmp/pwn", cmd="halt"
    )

    _default_ssh_shutdown(target)

    (argv,) = seen
    assert "--" in argv
    assert argv.index("--") < argv.index("-oProxyCommand=touch /tmp/pwn")
    assert argv[-1] == "halt"


def test_local_runner_returns_failure_when_it_times_out(monkeypatch) -> None:
    def boom(*_a, **_k):
        raise subprocess.TimeoutExpired(cmd="shutdown", timeout=20)

    monkeypatch.setattr(events_mod.subprocess, "run", boom)

    rc, _out, err = _default_local_shutdown("/sbin/shutdown -h now")

    assert rc == 1
    assert "local" in err


def test_local_runner_returns_failure_on_unbalanced_quote() -> None:
    # shlex.split raises ValueError before subprocess.run is ever reached.
    rc, _out, err = _default_local_shutdown('/sbin/shutdown -h "now')

    assert rc == 1
    assert "local" in err


def test_local_runner_returns_failure_on_empty_command() -> None:
    # subprocess.run([]) raises IndexError before any process is spawned.
    rc, _out, err = _default_local_shutdown("   ")

    assert rc == 1
    assert "local" in err


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
    monkeypatch.setattr(
        events_mod.subprocess, "run", lambda *a, **k: _Proc(1, "", "no such command")
    )
    rc, _out, err = _default_local_shutdown("/sbin/shutdown -h now")
    assert rc == 1
    assert "no such command" in err


# --- T-02-24 at the CALL SITE: an INJECTED runner that raises ------------------
#
# The per-runner handlers cover the three defaults. `Deps` carries injected runners
# (tests, and any future transport) that they do not cover, and the invariant that
# matters — shutdowns_sent is always appended and the local targets are always
# reached — can only be guaranteed where the dispatch happens.


def _record_events(deps) -> list[tuple[str, dict[str, object]]]:
    seen: list[tuple[str, dict[str, object]]] = []
    deps.event_log = lambda ev, _u, _s, _m, data: seen.append((ev, dict(data or {})))
    return seen


def test_fire_target_reports_a_raising_injected_runner() -> None:
    notifier = FakeNotifier()
    deps, _calls = make_deps(notifier, snap("OB LB", charge=8, runtime=90), countdown_every=0)
    seen = _record_events(deps)

    def boom(_target):
        raise RuntimeError("the switch is dead")

    deps.ssh_shutdown = boom
    ups = make_ups("ups1", targets=(_remote("srv"),), shutdown_policy=shutdown_policy())
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)

    assert state.shutdowns_sent == ["srv"]  # attempted-once still holds
    results = [data for ev, data in seen if ev == "shutdown_result"]
    assert len(results) == 1
    assert results[0]["returncode"] == 1
    assert "the switch is dead" in str(results[0]["stderr"])
    assert "shutdown FAILED" in notifier.sent[-1].title


def test_local_target_fires_even_when_the_remote_runner_raises() -> None:
    # THE starvation regression. `cyberpower` powers BOTH mt and eulerpi5 — this host
    # — so a hung push to mt used to drain the very battery meant to carry the
    # orchestrator to a clean halt. A raising remote must not strand the local target.
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB LB", charge=8, runtime=90), countdown_every=0)

    def boom(_target):
        raise subprocess.TimeoutExpired(cmd="ssh", timeout=20)

    deps.ssh_shutdown = boom
    ups = make_ups(
        "ups1",
        targets=(_remote("srv"), _local("pi")),
        shutdown_policy=shutdown_policy(
            internal_enabled=True, internal_battery_below=10, internal_runtime_below=None
        ),
    )
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)

    assert calls == ["local"]  # the raising remote recorded nothing; the local still fired
    assert state.shutdowns_sent == ["srv", "pi"]


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


def _mt(*, ups: str = "ups1", baud: int | None = 9600, name: str = "mt") -> MonitoredMachine:
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


def _project_with_deps(ups, machines, deps) -> list[ShutdownTarget]:
    from ups_orchestrator.events import _machine_targets

    return list(_machine_targets(ups, machines, deps))


def _error_notice(subject: str = "mt") -> ConfigNotice:
    return ConfigNotice(severity="error", subject=subject, message="disarmed at load")


def _advisory_notice(subject: str = "spark") -> ConfigNotice:
    return ConfigNotice(severity="advisory", subject=subject, message="needs escalation")


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


def test_projected_serial_carries_declared_baud_verbatim() -> None:
    # L1: the whole command, not just the target name. _default_serial_shutdown runs
    # `stty -F <device> <baud>`, so a substituted baud writes garbage down the line
    # and still returns rc=0 — a silent no-shutdown (P2-08).
    notifier = FakeNotifier()
    captured: list[tuple[str, int, str]] = []
    deps, calls = make_deps(
        notifier,
        _low(),
        countdown_every=0,
        monitored_machines=(_mt(),),
        serial_capture=captured,
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt"]
    assert captured == [(MT_DEVICE, 9600, MT_CMD)]


def test_projected_baud_override_is_not_rewritten() -> None:
    # An operator who declares a non-default baud gets exactly that baud.
    notifier = FakeNotifier()
    captured: list[tuple[str, int, str]] = []
    deps, _ = make_deps(
        notifier,
        _low(),
        countdown_every=0,
        monitored_machines=(_mt(baud=19200),),
        serial_capture=captured,
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert captured == [(MT_DEVICE, 19200, MT_CMD)]


def test_projected_routing_serial_and_ssh_runners() -> None:
    notifier = FakeNotifier()
    captured: list[tuple[str, int, str]] = []
    deps, calls = make_deps(
        notifier,
        _low(),
        countdown_every=0,
        monitored_machines=(_mt(), _spark()),
        serial_capture=captured,
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt", "spark"]
    # Only the serial machine reached the serial runner; spark went out over ssh.
    assert [device for device, _baud, _cmd in captured] == [MT_DEVICE]


def test_native_machine_issues_no_push() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, _low(), countdown_every=0, monitored_machines=(_spark(method="native"),)
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == []  # upsmon on spark self-shuts on FSD; a push would double it


def test_none_machine_issues_no_push() -> None:
    notifier = FakeNotifier()
    machines = (MonitoredMachine(name="idle", ups="ups1", ssh="idle"),)
    deps, calls = make_deps(notifier, _low(), countdown_every=0, monitored_machines=machines)
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == []


def test_mixed_native_and_serial_fires_only_serial() -> None:
    # The live topology: spark is a native secondary, mt is a serial push. One UPS,
    # two regimes — mt must be pushed and spark must be left alone.
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier,
        _low(),
        countdown_every=0,
        monitored_machines=(_spark(method="native"), _mt()),
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt"]


def test_duplicate_projected_name_does_not_swallow_other_machine() -> None:
    # Two machines share a name (a hand-edited config). The duplicate is dropped,
    # but a *distinct* machine's shutdown must still go out.
    #
    # `calls` alone passes under a no-guard mutant — with the dedupe removed, the
    # second "mt" is simply skipped by the shutdowns_sent check and nothing changes.
    # The block event and the notification are what make removing the guard fail.
    notifier = FakeNotifier()
    machines = (_mt(), _spark(method="ssh", name="mt"), _spark())
    deps, calls = make_deps(notifier, _low(), countdown_every=0, monitored_machines=machines)
    seen: list[tuple[str, dict[str, object]]] = []
    deps.event_log = lambda ev, _u, _s, _m, data: seen.append((ev, dict(data or {})))
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt", "spark"]
    blocked = [data for ev, data in seen if ev == "shutdown_target_blocked"]
    assert [d["target"] for d in blocked] == ["mt"]
    assert "duplicate" in str(blocked[0]["reason"]).lower()
    assert any("mt" in note.body and "duplicate" in note.body.lower() for note in notifier.sent)


def test_projected_target_blocked_when_policy_disabled() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, _low(), countdown_every=0, monitored_machines=(_mt(), _spark())
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy(enabled=False))

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == []


def test_projected_target_fires_once_across_ticks() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, _low(), countdown_every=0, monitored_machines=(_mt(),))
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)
    dispatch("tick", ups, state, deps)

    assert calls == ["mt"]
    assert state.shutdowns_sent == ["mt"]


def test_projected_push_fires_without_nut_lb_at_threshold() -> None:
    # "UPS-low" for a push is the configured close-to-empty threshold, NOT NUT's LB
    # flag — LB drives the native secondaries, which are a separate mechanism. An
    # on-battery snapshot at/below the threshold fires even with no LB in status.
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, snap("OB", charge=8, runtime=90), countdown_every=0, monitored_machines=(_mt(),)
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt"]


def test_projected_push_does_not_fire_above_threshold() -> None:
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier,
        snap("OB", charge=80, runtime=1800),
        countdown_every=0,
        monitored_machines=(_mt(),),
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == []


def test_failed_projected_push_is_not_retried() -> None:
    # Attempt-once: _fire_target records the target whatever the runner returned, so
    # a box that failed to answer is not hammered on every remaining poll.
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, _low(), countdown_every=0, ssh_rc=1, monitored_machines=(_spark(),)
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)
    dispatch("tick", ups, state, deps)

    assert calls == ["spark"]
    assert state.shutdowns_sent == ["spark"]
    assert "shutdown FAILED" in notifier.sent[-1].title


def test_failed_projected_serial_push_is_not_retried() -> None:
    # Same contract over the serial transport: a short write (adapter unplugged, no
    # getty on the far end) is reported and recorded, not retried on the next poll.
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier, _low(), countdown_every=0, serial_rc=1, monitored_machines=(_mt(),)
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)
    dispatch("tick", ups, state, deps)

    assert calls == ["mt"]
    assert state.shutdowns_sent == ["mt"]
    assert "shutdown FAILED" in notifier.sent[-1].title


# --- Task 3: the projector's association rule and its effective-state reads ----
#
# The measured baseline, from the integration audit: a derived-ssh record with no `ups`
# projects [] on every UPS; an explicit ssh/serial method with no `ups` projects []; the
# same record with a matching `ups` projects; and `ups:"CyberPower2"` against the key
# `cyberpower2` used to project [] — that last one is the regression this task closes.
# The first three stay empty BY DESIGN; 02-06 is what makes them loud at load.


def test_machine_targets_match_a_case_mismatched_ups() -> None:
    # THE regression. _machine_targets compared normalize_ups_name outputs directly and
    # normalize_ups_name never case-folds, so a machine that loads clean and reports an
    # active method silently projected nothing.
    ups = make_ups("cyberpower2")

    assert [t.name for t in _project(ups, (_mt(ups="CyberPower2"),))] == ["mt"]


def test_machine_targets_match_a_case_mismatched_host_suffixed_ups() -> None:
    ups = make_ups("cyberpower2")

    assert [t.name for t in _project(ups, (_mt(ups="CyberPower2@localhost"),))] == ["mt"]


def test_machine_targets_blank_ups_projects_on_no_ups() -> None:
    # Shipped behaviour, and it stays. 02-06 is what makes it loud, by disarming such a
    # machine at load rather than letting it look protected.
    assert _project(make_ups("ups1"), (_mt(ups=""),)) == []
    assert _project(make_ups("ups1"), (_mt(ups="   "),)) == []
    # ...including against a UPS whose own name canonicalises to blank.
    assert _project(make_ups("   "), (_mt(ups=""),)) == []


def test_machine_targets_unknown_ups_projects_on_no_ups() -> None:
    assert _project(make_ups("ups1"), (_mt(ups="not-a-real-ups"),)) == []


def test_machine_targets_ambiguous_case_pair_is_deduped_by_name() -> None:
    # Both `ups` values canonicalise to the same key, so both are CONSIDERED for this
    # UPS — and the name-collision dedupe then drops the second.
    ups = make_ups("ups1")

    targets = _project(ups, (_mt(ups="ups1"), _mt(ups="UPS1@localhost")))

    assert [t.name for t in targets] == ["mt"]


def test_disarmed_machine_is_not_projected_but_keeps_its_declaration() -> None:
    disarmed = replace(_spark(), load_notices=(_error_notice("spark"),))

    assert _project(make_ups("ups1"), (disarmed,)) == []
    assert disarmed.shutdown_method == "ssh"  # the declaration on disk is untouched
    assert disarmed.effective_method == "none"


def test_advisory_only_push_machine_projects_identically_to_one_with_no_notices() -> None:
    # The projection half of the INV-SEVERITY guarantee: the NEW-2 and BL-02 advisories
    # 02-06 attaches to LIVE push machines must not silently unarm them.
    plain = _spark()
    advised = replace(plain, load_notices=(_advisory_notice("spark"),))
    ups = make_ups("ups1")

    assert _project(ups, (advised,)) == _project(ups, (plain,))
    assert [t.name for t in _project(ups, (advised,))] == ["spark"]


def test_disarmed_legacy_target_neither_fires_nor_reserves_its_name() -> None:
    legacy = ShutdownTarget(
        name="mt",
        kind="serial",
        enabled=True,
        device="/dev/x",
        load_notices=(_error_notice("ups1/mt"),),
    )
    notifier = FakeNotifier()
    captured: list[tuple[str, int, str]] = []
    deps, calls = make_deps(
        notifier,
        _low(),
        countdown_every=0,
        monitored_machines=(_mt(),),
        serial_capture=captured,
    )
    ups = make_ups("ups1", targets=(legacy,), shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == ["mt"]  # once — the disarmed legacy target did not fire
    assert captured == [(MT_DEVICE, 9600, MT_CMD)]  # ...and the machine's own device won


def test_serial_machine_with_no_parsed_baud_is_not_projected() -> None:
    # 02-06's strict parser leaves serial_baud None for a value it could not read.
    # Projecting it would carry None into `stty -F <dev> None`.
    assert _project(make_ups("ups1"), (_mt(baud=None),)) == []


def _blocked(seen) -> list[dict[str, object]]:
    return [data for ev, data in seen if ev == "shutdown_target_blocked"]


def test_dropped_duplicate_emits_a_block_event_and_a_notification() -> None:
    # NEW-3: mid-outage syslog is the channel least likely to be read, and the
    # consequence of the drop is a machine that will not shut down.
    notifier = FakeNotifier()
    deps, _calls = make_deps(notifier, snap("OL"))
    seen = _record_events(deps)
    ups = make_ups("ups1")

    targets = _project_with_deps(ups, (_mt(), _mt()), deps)

    assert [t.name for t in targets] == ["mt"]
    assert [d["target"] for d in _blocked(seen)] == ["mt"]
    assert notifier.sent and "mt" in notifier.sent[-1].body


def test_no_baud_skip_emits_a_block_event_and_a_notification() -> None:
    notifier = FakeNotifier()
    deps, _calls = make_deps(notifier, snap("OL"))
    seen = _record_events(deps)

    assert _project_with_deps(make_ups("ups1"), (_mt(baud=None),), deps) == []
    assert [d["target"] for d in _blocked(seen)] == ["mt"]
    assert notifier.sent and "mt" in notifier.sent[-1].body


def test_machine_targets_without_deps_reports_through_the_log_alone(caplog) -> None:
    # 02-02's two-argument call sites must keep working; deps is optional.
    with caplog.at_level(logging.ERROR, logger="ups_orchestrator.events"):
        assert [t.name for t in _project(make_ups("ups1"), (_mt(), _mt()))] == ["mt"]
        assert _project(make_ups("ups1"), (_mt(baud=None),)) == []

    assert "duplicate" in caplog.text.lower()
    assert "baud" in caplog.text.lower()


def test_build_deps_wires_monitored_machines() -> None:
    from ups_orchestrator.cli import _build_deps
    from ups_orchestrator.config import Config

    machines = (_mt(), _spark(method="native"))
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")}, monitored_machines=machines)

    assert _build_deps(cfg).monitored_machines == machines
