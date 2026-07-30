from __future__ import annotations

import fcntl
import inspect
import json
import logging
import os
import re
import select
import stat
import subprocess
import sys
import termios
import threading
import time
import tty
from dataclasses import replace

import pytest

from conftest import FakeNotifier, make_deps, make_ups, shutdown_policy, snap
from ups_orchestrator import events as events_mod
from ups_orchestrator.config import (
    ConfigNotice,
    MonitoredMachine,
    ShutdownTarget,
    is_disarming,
)
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
    fdopen_error: OSError | None = None,
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
        if fdopen_error is not None:
            raise fdopen_error
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
    assert wiring.stty_argv == [
        ["stty", "-F", SERIAL_DEVICE, "19200", "raw", "-echo", "clocal", "-crtscts"]
    ]
    assert "19200" in err


def test_serial_declared_9600_reaches_stty_argv_on_the_SUCCESS_path(monkeypatch) -> None:
    # The argv assertion above only runs on the stty-FAILURE path and only for a rate
    # that is not the live console's. Both halves matter: the r630's console really is
    # 9600, and a substitution on the success path is the silent no-shutdown P2-08
    # exists for — `stty -F <dev> 115200` returns 0, the write returns 0, and the box
    # stays up while the orchestrator reports success. Pin the whole argv where the
    # shutdown actually happens.
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    rc, _out, err = _default_serial_shutdown(_serial_target(baud=9600))

    assert (rc, err) == (0, "")
    assert wiring.stty_argv == [
        ["stty", "-F", SERIAL_DEVICE, "9600", "raw", "-echo", "clocal", "-crtscts"]
    ]


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


def test_serial_open_does_not_acquire_a_controlling_terminal(monkeypatch) -> None:
    # HI-04. systemd puts each service in its own session, so `watch` is a session
    # leader with no controlling terminal — and under POSIX such a process opening a
    # tty without O_NOCTTY ACQUIRES that tty as its controlling terminal. With CLOCAL
    # clear (which `stty raw` does not change) a carrier transition on the console then
    # delivers SIGHUP to the session and the default disposition kills the daemon,
    # mid-outage, losing the poll loop's in-memory state. Asserted on the flag word,
    # because the failure is only observable on real hardware.
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    rc, _out, _err = _default_serial_shutdown(_serial_target())

    assert rc == 0
    _path, flags = wiring.opened[0]
    assert flags & os.O_NOCTTY


def test_serial_stty_sets_clocal_and_disables_hardware_flow_control(monkeypatch) -> None:
    # The other half of HI-04. `raw` touches nothing in c_cflag, so the O_NONBLOCK open
    # bypassed the DCD wait only for the open() itself — the flag is cleared straight
    # afterwards, restoring the kernel's carrier sensitivity for the blocking write and
    # the close(). `clocal` settles it for the whole line; `-crtscts` keeps a cable with
    # no CTS from blocking the write and the close (up to closing_wait, 30 s, inside
    # the poll loop).
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    _default_serial_shutdown(_serial_target())

    (argv,) = wiring.stty_argv
    assert "clocal" in argv
    assert "-crtscts" in argv


def test_serial_closes_the_descriptor_when_fdopen_fails(monkeypatch) -> None:
    # LO-01. The try/except/os.close covered the two fcntl calls but the fdopen sat
    # BELOW it, so an OSError there leaked the descriptor — once per poll, for the
    # whole outage, until the daemon's fd table was exhausted.
    wiring = _wire_serial(
        monkeypatch,
        cmd_written=len(b"poweroff\n"),
        fdopen_error=OSError(24, "Too many open files"),
    )

    rc, _out, err = _default_serial_shutdown(_serial_target())

    assert rc == 1
    assert SERIAL_DEVICE in err
    assert wiring.closed == [_SENTINEL_FD]


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
    wiring = _wire_serial(monkeypatch, run_raises=subprocess.TimeoutExpired(cmd="stty", timeout=5))

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


@pytest.mark.allow_subprocess
def test_local_runner_returns_failure_on_empty_command() -> None:
    # subprocess.run([]) raises IndexError before any process is spawned — which is
    # exactly the behaviour under test, so this is the one place in the suite that
    # must reach the real `subprocess.run` (LO-C3's opt-out). No process is created.
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


def test_an_undefined_close_to_empty_condition_never_fires() -> None:
    # 02-02's "the fire decision requires a DEFINED close-to-empty condition". Every
    # other gate test supplies at least one comparable reading, so `_close_to_empty`'s
    # no-known-readings branch was reached but never discriminated: flipping its
    # `return False` to `return True` left the whole suite green.
    #
    # This is the shape a dying UPS actually produces — upsc stops reporting
    # battery.charge and battery.runtime while ups.status still says OB — and firing
    # on it powers off every box on that UPS on a bad read, with the reason string
    # "UPS is not close to empty" nowhere in sight.
    notifier = FakeNotifier()
    blind = replace(snap("OB"), charge=None, runtime_seconds=None)
    deps, calls = make_deps(notifier, blind, countdown_every=0)
    seen: list[tuple[str, dict]] = []
    deps.event_log = lambda ev, _u, _s, _m, data: seen.append((ev, dict(data or {})))
    ups = make_ups(
        targets=(_remote("srv"), _local("pi")),
        shutdown_policy=shutdown_policy(internal_enabled=True),
    )

    dispatch("tick", ups, UpsState(onbatt_since=1), deps)

    assert calls == []
    blocked = {
        str(e[1].get("target")): str(e[1].get("reason", ""))
        for e in seen
        if e[0] == "shutdown_target_blocked"
    }
    assert "not close to empty" in blocked["srv"]  # the gate said so, and said why
    assert "waiting on remote(s)" in blocked["pi"]  # the local is held, not fired


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


class _RaisingNotifier(FakeNotifier):
    """A notifier that dies the way a dead switch mid-outage makes it die."""

    def send(self, note):  # noqa: ANN001, ANN201
        super().send(note)
        raise OSError("network is unreachable")


def test_local_target_fires_even_when_the_shutdown_notifier_raises() -> None:
    # HI-03. `_notify_shutdown_attempt` sat one line ABOVE `_fire_target`'s try, so a
    # raising notifier propagated out of the firing path before the runner ran and
    # before `shutdowns_sent` was appended — the exact T-02-24 starvation the backstop
    # exists to prevent, one statement out of its reach. During an outage the switch is
    # typically the first casualty, so this is the expected case, not an exotic one.
    notifier = _RaisingNotifier()
    deps, calls = make_deps(notifier, snap("OB LB", charge=8, runtime=90), countdown_every=0)
    ups = make_ups(
        "ups1",
        targets=(_remote("srv"), _local("pi")),
        shutdown_policy=shutdown_policy(
            internal_enabled=True, internal_battery_below=10, internal_runtime_below=None
        ),
    )
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)

    assert calls == ["srv", "local"]  # both transports still ran
    assert state.shutdowns_sent == ["srv", "pi"]
    assert notifier.sent  # ...and the attempt was genuinely tried, not skipped


def test_a_raising_unprojectable_report_does_not_strand_the_other_targets() -> None:
    # The `_report_unprojectable` half of HI-03: it is called from inside the
    # `_machine_targets` generator, which is consumed by the `enabled = [...]`
    # comprehension — so a raising notifier there escapes before ANY target has fired.
    notifier = _RaisingNotifier()
    machine = MonitoredMachine(
        name="srv",  # collides with the legacy target's name -> unprojectable
        ups="ups1",
        ssh="srv",
        shutdown_method="ssh",
    )
    deps, calls = make_deps(
        notifier,
        snap("OB LB", charge=8, runtime=90),
        countdown_every=0,
        monitored_machines=(machine,),
    )
    ups = make_ups("ups1", targets=(_remote("srv"),), shutdown_policy=shutdown_policy())
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)

    assert calls == ["srv"]
    assert state.shutdowns_sent == ["srv"]


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
        deps, calls = make_deps(notifier, _low(), countdown_every=0, monitored_machines=machines)
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


def test_unprojectable_report_pages_once_per_cooldown(monkeypatch) -> None:
    # LO-02. `_machine_targets` is re-evaluated on every poll while on battery and
    # `_report_unprojectable` had no rate limit at all, so one unprojectable machine
    # would page every poll_seconds for the whole outage. `_check_load_step` solves the
    # same problem with a cooldown; the event line still goes out every time, because
    # that is the audit trail.
    notifier = FakeNotifier()
    machine = MonitoredMachine(name="srv", ups="ups1", ssh="srv", shutdown_method="ssh")
    clock = {"now": 1000}
    deps, _calls = make_deps(
        notifier,
        snap("OB LB", charge=8, runtime=90),
        countdown_every=0,
        monitored_machines=(machine,),
    )
    deps.now = lambda: clock["now"]
    seen = _record_events(deps)
    ups = make_ups("ups1", targets=(_remote("srv"),), shutdown_policy=shutdown_policy())

    for _poll in range(3):
        list(events_mod._machine_targets(ups, deps.monitored_machines, deps))

    assert len(notifier.sent) == 1, "one page per cooldown, not one per poll"
    assert len([ev for ev, _d in seen if ev == "shutdown_target_blocked"]) == 3

    clock["now"] += deps.unprojectable_cooldown
    list(events_mod._machine_targets(ups, deps.monitored_machines, deps))
    assert len(notifier.sent) == 2  # ...and it does page again once the cooldown lapses


def test_a_held_local_target_logs_why_it_is_waiting() -> None:
    # LO-03. The locals-held path returned before the loop, so a held local produced
    # neither a shutdown_target_blocked event nor any other trace — the only non-firing
    # decision in the module that logged nothing.
    notifier = FakeNotifier()
    deps, calls = make_deps(notifier, snap("OB", charge=80, runtime=600), countdown_every=0)
    seen = _record_events(deps)
    ups = make_ups(
        "ups1",
        targets=(_remote("srv"), _local("pi")),
        shutdown_policy=shutdown_policy(internal_enabled=True),
    )
    state = UpsState(onbatt_since=1, onbatt_notified=True)

    dispatch("tick", ups, state, deps)

    assert calls == []  # nothing fired: the UPS is not close to empty yet
    held = [
        data for ev, data in seen if ev == "shutdown_target_blocked" and data.get("target") == "pi"
    ]
    assert held, "the held local target left no trace"
    assert "srv" in str(held[-1]["reason"])


def _cross_ups_config(tmp_path, *, method: str) -> dict:
    """The exact shape the final verification executed, parameterised by method."""
    return {
        "upses": {
            "cyberpower": {
                "label": "CP",
                "shutdown_targets": [
                    {"name": "spark", "kind": "remote", "enabled": True, "host": "spark"}
                ],
            },
            "cyberpower3": {"label": "CP3", "shutdown_targets": []},
        },
        "monitored_machines": [
            {"name": "spark", "ssh": "spark", "ups": "cyberpower3", "shutdown_method": method}
        ],
        "shutdown": {
            "enabled": True,
            "require_power_outage": True,
            "min_on_battery_seconds": 0,
            "external": {"enabled": True, "battery_below": 15, "runtime_below": 300},
        },
    }


def _fire_every_ups(cfg) -> list[str]:
    """Run one on-battery-and-low tick per configured UPS; return what fired, in order."""
    fired: list[str] = []
    for ups in cfg.upses.values():
        notifier = FakeNotifier()
        deps, calls = make_deps(
            notifier,
            _low(),
            countdown_every=0,
            monitored_machines=cfg.monitored_machines,
        )
        dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)
        fired.extend(calls)
    return fired


def test_cross_ups_native_collision_cannot_fire_two_authorities(tmp_path) -> None:
    """The surviving double-shutdown the final verification reproduced.

    spark declares `native` on cyberpower3 AND has an enabled legacy target on
    cyberpower. Before the fix: `degraded notices: 0`, a cyberpower tick fired
    `ssh:spark`, and spark ALSO self-halts on cyberpower3's FSD — two live
    authorities with zero operator surface. `native` is the one authority config
    cannot disarm, so the ONLY correct outcome is that this daemon pushes nothing.
    """
    from ups_orchestrator.config import Config

    p = tmp_path / "config.json"
    p.write_text(json.dumps(_cross_ups_config(tmp_path, method="native")))
    cfg = Config.load(p, env={})

    # The push is gone: no tick on any UPS reaches a transport.
    assert _fire_every_ups(cfg) == []

    # The native authority is untouched (INV-DECLARED) and is the single survivor.
    (spark,) = cfg.monitored_machines
    assert (spark.disarmed, spark.effective_method) == (False, "native")

    # ...and the operator was told, which is the half that was entirely missing.
    assert [n.subject for n in cfg.degraded if is_disarming(n)], "silent again"
    assert any("spark" in n.message for n in cfg.degraded)


def test_cross_ups_PUSH_collision_still_fires_both_independently(tmp_path) -> None:
    """The behaviour deliberately preserved, pinned so the fix cannot over-reach.

    Two pushes keyed to two different UPSes ARE two power domains: each fires on
    its own outage, and neither is a double-shutdown of one event. Nothing is
    disarmed and both still fire — one per tick, never both on the same tick.
    """
    from ups_orchestrator.config import Config

    p = tmp_path / "config.json"
    p.write_text(json.dumps(_cross_ups_config(tmp_path, method="ssh")))
    cfg = Config.load(p, env={})

    (spark,) = cfg.monitored_machines
    assert (spark.disarmed, spark.effective_method) == (False, "ssh")
    assert [n for n in cfg.degraded if is_disarming(n)] == []

    # One firing per UPS tick: the legacy target on cyberpower, the projected push
    # on cyberpower3. Two events, two authorities, never the same event twice.
    assert _fire_every_ups(cfg) == ["spark", "spark"]


class _TimelineNotifier(FakeNotifier):
    """A notifier that writes into a shared timeline, so ordering is observable.

    The real notifier's cost is what F3 is about: max_attempts=3 x timeout=5.0 plus
    backoff is ~16.5 s against a switch the outage has already killed. Cost is not
    simulated here — ORDER is the invariant, and order is what decides whether the
    transport waits on a POST or the POST waits on the transport.
    """

    def __init__(self, timeline: list[str]) -> None:
        super().__init__()
        self._timeline = timeline

    def send(self, note):
        kind = (
            "attempt" if "attempt" in note.title else "result" if "sent" in note.title else "other"
        )
        self._timeline.append(f"NOTIFY:{kind}")
        return super().send(note)


def _fire_timeline(targets: tuple[ShutdownTarget, ...]) -> list[str]:
    """Run one on-battery-and-low tick; return the interleaved POST/transport order."""
    timeline: list[str] = []
    notifier = _TimelineNotifier(timeline)
    deps, _calls = make_deps(notifier, _low(), countdown_every=0)

    def _ssh(target: ShutdownTarget) -> tuple[int, str, str]:
        timeline.append(f"TRANSPORT:{target.name}")
        return 0, "", ""

    deps = replace(deps, ssh_shutdown=_ssh)
    ups = make_ups("ups1", targets=targets, shutdown_policy=shutdown_policy())
    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)
    return [e for e in timeline if e != "NOTIFY:other"]


def test_f3_transport_is_not_made_to_wait_on_a_notification() -> None:
    """F3. The attempt POST used to precede the runner, so every transport waited.

    Measured before this fix with a 0.30 s stand-in POST: three ssh targets took
    2.40 s and every transport was preceded by a full blocking POST. Scaled to the
    real notifier against a dead switch that is ~16.5 s per target, serialised,
    inside a gate that opens at runtime_below: 300 — and `cyberpower` powers BOTH
    this orchestrator and the Dell PowerEdge, so that time comes straight out of
    the Pi's own remaining runtime.
    """
    order = _fire_timeline((_remote("box0"), _remote("box1"), _remote("box2")))

    assert order == [
        "TRANSPORT:box0",
        "NOTIFY:attempt",
        "NOTIFY:result",
        "TRANSPORT:box1",
        "NOTIFY:attempt",
        "NOTIFY:result",
        "TRANSPORT:box2",
        "NOTIFY:attempt",
        "NOTIFY:result",
    ]
    # The load-bearing property, stated independently of the exact embed set: no
    # notification is ever emitted before the first transport, and each transport
    # is reached having waited on strictly fewer POSTs than there are prior targets
    # x 2. Before the fix the first entry was NOTIFY:attempt.
    assert order[0].startswith("TRANSPORT:")
    assert order.index("TRANSPORT:box2") == 6  # was 7 with the attempt POST in front


def test_f3_reorder_preserves_the_shutdowns_sent_guarantee() -> None:
    """T-02-24 must survive the reorder: a failing remote still lets the local fire.

    The append moved to sit between the runner and both notifications; it must
    still happen on EVERY outcome, including a non-zero rc and an escaping runner.
    """
    for runner, label in (
        (lambda _t: (1, "", "boom"), "rc!=0"),
        (_raise_switch_is_dead, "raised"),
    ):
        notifier = FakeNotifier()
        deps, calls = make_deps(notifier, _low(), countdown_every=0, local_rc=0)
        deps = replace(deps, ssh_shutdown=runner)
        ups = make_ups(
            "ups1",
            targets=(_remote("srv"), _local("pi")),
            shutdown_policy=shutdown_policy(internal_enabled=True),
        )
        state = UpsState(onbatt_since=1, onbatt_notified=True)

        dispatch("tick", ups, state, deps)

        assert state.shutdowns_sent == ["srv", "pi"], label
        assert calls == ["local"], label  # the local proceeded despite the dead remote
        # ...and the operator still gets both embeds for the failed remote.
        titles = [n.title for n in notifier.sent]
        assert any("shutdown attempt for srv" in t for t in titles), label
        assert any("shutdown FAILED for srv" in t for t in titles), label


def _raise_switch_is_dead(_target: ShutdownTarget) -> tuple[int, str, str]:
    raise RuntimeError("the switch is dead")


# --- P2-03's unstated narrowing: the push trigger is NOT NUT's LB flag ---------


def test_lowbatt_never_fires_a_push_even_with_a_machine_enrolled() -> None:
    """docs/Shutdown-Mechanisms.md: NUT's LOWBATT does not reach the shutdown path.

    `test_lowbatt_only_notifies` above proves the handler fires nothing, but it
    runs with no enrolled machine and no target, so it cannot distinguish "does
    not fire" from "had nothing to fire". This one hands `handle_lowbatt` a push
    machine AND an enabled legacy target on a fully-armed policy.
    """
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier,
        snap("OB LB", charge=2, runtime=30),
        countdown_every=0,
        monitored_machines=(_spark(),),
    )
    state = UpsState(onbatt_since=1, onbatt_notified=True)
    ups = make_ups("ups1", targets=(_remote("srv"),), shutdown_policy=shutdown_policy())

    dispatch("lowbatt", ups, state, deps)

    assert calls == []
    assert state.shutdowns_sent == []


def test_the_LB_flag_alone_does_not_open_the_push_gate() -> None:
    """The push gate reads the configured thresholds, never `UpsSnapshot.low_battery`.

    `LB` is set and the battery is under `battery_below`, but runtime is above
    `runtime_below` — and `_close_to_empty` ANDs the two when both are configured.
    NUT would already be halting a native secondary here; the push does not fire.
    """
    notifier = FakeNotifier()
    deps, calls = make_deps(
        notifier,
        snap("OB LB", charge=5, runtime=1200),
        countdown_every=0,
        monitored_machines=(_spark(),),
    )
    ups = make_ups("ups1", shutdown_policy=shutdown_policy())

    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps)

    assert calls == []

    # Drop runtime under the threshold — now BOTH are crossed and it fires. Same
    # snapshot LB flag in both halves, so the flag is provably not the trigger.
    deps2, calls2 = make_deps(
        notifier,
        snap("OB LB", charge=5, runtime=120),
        countdown_every=0,
        monitored_machines=(_spark(),),
    )
    dispatch("tick", ups, UpsState(onbatt_since=1, onbatt_notified=True), deps2)
    assert calls2 == ["spark"]


# ==============================================================================
# The network-independent read-back liveness probe
# ==============================================================================
#
# WHY A PTY AND NOT THE REAL LINE. The conftest tripwire covers PROCESS SPAWNS —
# `subprocess.run`/`Popen` and the `os` system/popen/spawn/exec families. It does NOT
# cover `open`, `os.open` or sockets, so a test that reached a real character device
# would sail straight past it and land in a root-capable auto-login shell on a live
# PowerEdge. The two things that actually prevent that are (a) every probe test running
# against a `pty` pair, and (b) the injected line-configuration seam, which is also why
# no `@pytest.mark.allow_subprocess` appears anywhere below. Both are deliberate.
#
# The far end is scripted rather than mocked because the distinctions under test are
# distinctions between real far-end BEHAVIOURS — a shell that executes, a terminal that
# only echoes, a cable that loops back — and a mock of the probe's own reads would just
# assert the implementation back at itself.

# Checked by `test_no_probe_test_names_the_real_serial_device` against the source of
# everything below this line. Assembled from fragments so the check does not trip on
# its own needles.
_REAL_DEVICE_NEEDLES = ("tty" + "USB", "by-" + "id", "/dev/" + "serial")

_PERSONALITIES = ("executing", "echoing", "loopback", "silent", "chatty")


class _FarEnd:
    """A scripted far end on the master side of a pty.

    ``executing`` — a bash at an auto-login prompt: readline ECHOES the typed line
        first, and only then does the shell run it and emit its output. Echoing as well
        as executing is the point; a far end that only executed would let a much weaker
        probe pass.
    ``echoing`` — the typed line comes back verbatim and nothing else. This is a real
        terminal with no scheduler behind it, and the case a naive nonce probe calls
        alive.
    ``loopback`` — exactly the bytes received, byte for byte: a shorted or looped cable.
    ``silent`` — reads, answers nothing.
    ``chatty`` — executes, but buries the answer under twice the probe's buffer cap of
        console spew and splits the token across two writes.
    """

    def __init__(self, master_fd: int, personality: str, *, ignore_first: int = 0) -> None:
        assert personality in _PERSONALITIES
        self.master_fd = master_fd
        self.personality = personality
        self.ignore_first = ignore_first
        self.received = bytearray()
        self.commands = 0
        # Every line the far end actually RAN, so a test can assert that something a
        # human left half-typed never became one.
        self.executed: list[bytes] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _run(self) -> None:
        pending = bytearray()
        while not self._stop.is_set():
            readable, _w, _x = select.select([self.master_fd], [], [], 0.02)
            if not readable:
                continue
            try:
                chunk = os.read(self.master_fd, 4096)
            except OSError:
                return
            if not chunk:
                return
            self.received += chunk
            if self.personality == "loopback":
                os.write(self.master_fd, chunk)
                continue
            pending += chunk
            while True:
                # VKILL is handled in STREAM ORDER alongside the terminators, not by a
                # scan of the whole buffer: "abc\rdef\x15ghi" must submit "abc", discard
                # "def", and leave "ghi" pending, exactly as a real tty would.
                marks = [
                    i
                    for i in (pending.find(b"\r"), pending.find(b"\n"), pending.find(b"\x15"))
                    if i >= 0
                ]
                if not marks:
                    break
                cut = min(marks)
                if pending[cut : cut + 1] == b"\x15":
                    # Canonical-mode VKILL: the line discipline throws away the pending
                    # input line and the shell never sees it. Modelling this is
                    # load-bearing rather than cosmetic -- the probe sends VKILL so that
                    # a half-typed command on the console is DISCARDED instead of
                    # submitted, and a double that delivered \x15 to the shell as a
                    # command would report that safety property working when it was not.
                    pending = pending[cut + 1 :]
                    continue
                line, pending = bytes(pending[:cut]), pending[cut + 1 :]
                if line.strip():
                    self._answer(line)

    def _answer(self, line: bytes) -> None:
        if self.personality == "silent":
            return
        self.commands += 1
        if self.commands <= self.ignore_first:
            return  # a getty respawning at exactly the wrong moment: no reader at all
        self.executed.append(line)
        os.write(self.master_fd, line + b"\r\n")  # readline echoes before it runs
        if self.personality == "echoing":
            return
        out = _shell_output(line)
        if out is None:
            return
        if self.personality == "chatty":
            # Spew first, then the answer split across two writes with a gap, so the
            # token can only be found by scanning an ACCUMULATED buffer that is capped
            # from the OLD end.
            os.write(self.master_fd, b"kernel: console noise\r\n" * 700)
            half = len(out) // 2
            os.write(self.master_fd, out[:half])
            time.sleep(0.02)
            os.write(self.master_fd, out[half:] + b"\r\n")
            return
        os.write(self.master_fd, out + b"\r\n")


def _shell_output(line: bytes) -> bytes | None:
    """What a shell would PRINT for ``line`` — quote removal is the whole point.

    ``echo UPSPRO""BE-x`` prints ``UPSPROBE-x``: the two halves are joined only by the
    shell, at execution time, which is why the joined form cannot appear in the input.
    """
    if not line.startswith(b"echo "):
        return None
    return line[len(b"echo ") :].replace(b'""', b"").replace(b"''", b"")


class _PtyLine:
    """A pty standing in for the console cable, plus the injected configuration seam."""

    def __init__(self) -> None:
        self.master_fd, self.slave_fd = os.openpty()
        self.device = os.ttyname(self.slave_fd)
        self.configured: list[tuple[str, int]] = []
        self.far_end: _FarEnd | None = None

    def configure_line(self, device: str, baud: int) -> tuple[int, str]:
        self.configured.append((device, baud))
        # TCSANOW, not the `tty.setraw` default of TCSAFLUSH: TCSAFLUSH DISCARDS queued
        # input, which would silently pre-empt the probe's own drain and make the
        # queued-junk test assert nothing.
        tty.setraw(self.slave_fd, termios.TCSANOW)
        return 0, ""

    def start(self, personality: str, *, ignore_first: int = 0) -> _FarEnd:
        self.far_end = _FarEnd(self.master_fd, personality, ignore_first=ignore_first)
        return self.far_end

    def preload(self, payload: bytes) -> int:
        """Queue bytes on the probe's INPUT side, as a console that spewed before we ran."""
        os.set_blocking(self.master_fd, False)
        written = 0
        try:
            while written < len(payload):
                try:
                    written += os.write(self.master_fd, payload[written:])
                except BlockingIOError:
                    break
        finally:
            os.set_blocking(self.master_fd, True)
        return written

    def close(self) -> None:
        if self.far_end is not None:
            self.far_end.stop()
        os.close(self.master_fd)
        os.close(self.slave_fd)


@pytest.fixture
def pty_line():
    line = _PtyLine()
    try:
        yield line
    finally:
        line.close()


def _probe(
    line: _PtyLine,
    *,
    deadline_seconds: float = 1.0,
    settle_seconds: float = 0.01,
    retries: int = 0,
    configure_line=None,
) -> events_mod.ProbeResult:
    """Run the probe against the pty, at test-scale timings."""
    return events_mod.serial_liveness_probe(
        line.device,
        115200,
        deadline_seconds=deadline_seconds,
        settle_seconds=settle_seconds,
        retries=retries,
        configure_line=configure_line or line.configure_line,
    )


def test_executing_far_end_is_seen(pty_line) -> None:
    # The positive control. Without it, every NOT_SEEN assertion below would pass for a
    # probe that reports NOT_SEEN unconditionally.
    # Kills: a probe that never matches (inverted comparison, expected token never built).
    pty_line.start("executing")

    result = _probe(pty_line)

    assert result.outcome is events_mod.ProbeOutcome.SEEN
    assert result.elapsed_seconds >= 0


def test_split_token_defeats_a_far_end_that_only_echoes(pty_line) -> None:
    # The single most important test here. The echoing far end sends the typed line back
    # verbatim — which is what a real bash does BEFORE it runs anything — so the nonce
    # itself is present in the read-back under BOTH branches. Asserting on the nonce
    # would therefore pass either way; the assertion has to be on the OUTCOME under a far
    # end that echoes and does not execute.
    # Kills: building the expected match from the typed line (i.e. reunifying the split
    #        by stripping the quotes), which is the one refactor that destroys the design
    #        while leaving every other test green.
    pty_line.start("echoing")

    result = _probe(pty_line)

    assert result.outcome is events_mod.ProbeOutcome.NOT_SEEN
    # ...and prove the echo really happened, so this is not passing because nothing came
    # back at all.
    assert bytes(pty_line.far_end.received).count(b"echo ") >= 1


def test_written_bytes_never_contain_the_joined_token(pty_line) -> None:
    # The structural companion to the test above: without it, a mutant that wrote the
    # JOINED token would make that test pass spuriously, because the echoing far end
    # would then be echoing a matching token back.
    # Kills: writing the joined token down the line.
    pty_line.start("silent")

    _probe(pty_line)

    wire = bytes(pty_line.far_end.received)
    assert b'UPSPRO""BE-' in wire  # the split form is what is typed
    assert b"UPSPROBE-" not in wire  # the joined form is never on the wire


def test_loopback_cable_does_not_produce_a_match(pty_line) -> None:
    # Kills: the same reunification, from the other direction — a shorted or looped
    #        cable returns exactly what was sent, so any probe whose expected match is
    #        derivable from its own output declares a length of wire alive.
    pty_line.start("loopback")

    result = _probe(pty_line)

    assert result.outcome is events_mod.ProbeOutcome.NOT_SEEN
    assert bytes(pty_line.far_end.received)  # the loopback really did see the bytes


def test_silent_far_end_returns_within_the_deadline(pty_line) -> None:
    # T-03-49, graded CRITICAL. A blocking read on a line nothing answers never returns:
    # handle_tick never returns, the poll loop wedges, every UPS stops being polled, and
    # Restart=always never fires because the process is still alive.
    # Kills: clearing O_NONBLOCK after the open (as the WRITE path deliberately does) —
    #        which makes this test hang rather than fail, and a hang is caught by the
    #        harness timeout, so the mutant dies either way.
    pty_line.start("silent")
    started = time.monotonic()

    result = _probe(pty_line, deadline_seconds=0.5)

    elapsed = time.monotonic() - started
    assert result.outcome is events_mod.ProbeOutcome.NOT_SEEN
    assert elapsed < 1.0, f"probe overran its 0.5s deadline: {elapsed:.3f}s"
    assert result.elapsed_seconds < 1.0


def test_probe_opens_read_write_and_never_clears_the_non_blocking_flag(
    monkeypatch, pty_line
) -> None:
    # The three divergences from the write path, asserted on the flag word — the same
    # technique, and for the same reason, as the write path's own flag test: the failure
    # is only observable on real hardware, and a regression manifests as a HANG rather
    # than as a wrong answer, which no ordinary assertion can catch.
    #
    # This is the test that discriminates on the flag itself. The deadline test below is
    # NOT: with a `select` guarding every read, clearing O_NONBLOCK alone changes no
    # observable behaviour, so it survives that test. What clearing the flag destroys is
    # the SAFETY MARGIN — it is only harmless for as long as the select stays, and the
    # refactor that removes the select is precisely the one that would "restore" the
    # write path's shape. Assert the flag directly rather than rely on a hang.
    #
    # Kills: O_WRONLY (os.read would raise EBADF); dropping O_NOCTTY (the daemon
    #        acquires the console as its controlling terminal and dies on SIGHUP);
    #        clearing O_NONBLOCK after the open, as the write path deliberately does.
    fds: list[int] = []
    flags_seen: list[int] = []
    blocking_at_read_time: list[bool] = []
    real_open, real_select = os.open, select.select

    def spy_open(path, flags, *a):
        fd = real_open(path, flags, *a)
        if path == pty_line.device:
            flags_seen.append(flags)
            fds.append(fd)
        return fd

    def spy_select(rlist, wlist, xlist, timeout=None):
        if fds:
            blocking_at_read_time.append(os.get_blocking(fds[-1]))
        return real_select(rlist, wlist, xlist, timeout)

    monkeypatch.setattr(events_mod.os, "open", spy_open)
    monkeypatch.setattr(events_mod.select, "select", spy_select)
    # No far end at all: nothing reads the master, so nothing answers. A far-end thread
    # would also be calling select and would pollute the recording.

    _probe(pty_line, deadline_seconds=0.2)

    assert flags_seen == [os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK]
    assert blocking_at_read_time  # the probe really did reach a select
    assert not any(blocking_at_read_time), "the probe cleared O_NONBLOCK"


def test_the_probe_retries_once_when_the_first_attempt_is_unanswered(pty_line) -> None:
    # A getty respawning at exactly the wrong moment leaves a sub-second window with no
    # reader on the far end. The far end here ignores the first command outright and
    # answers the second.
    # Kills: dropping the retry; and giving the first attempt the whole budget, which
    #        leaves nothing for the second and reads identically to having no retry.
    far = pty_line.start("executing", ignore_first=1)

    result = _probe(pty_line, retries=1, deadline_seconds=2.0)

    assert result.outcome is events_mod.ProbeOutcome.SEEN
    assert far.commands >= 2
    assert "attempt 2" in result.detail


def test_the_probe_discards_a_half_typed_line_instead_of_submitting_it(pty_line) -> None:
    """The probe must not execute what a human left sitting on the console.

    The far end is a getty in canonical mode. If someone attached with screen, typed
    `shutdown -h now`, and walked away without pressing Enter, that line sits in the
    line-discipline buffer. The probe used to open with a bare "\r" to nudge the shell
    to a fresh prompt -- which SUBMITS that line. Harmless while the probe had no
    production caller; a machine-killer once `monitor verify --deep` could reach it,
    since verify reads as a read-only diagnostic an operator runs casually.

    Sending VKILL first discards the pending line at the line discipline. This asserts
    the dangerous line never becomes a command, while the probe still gets its answer.
    """
    far = pty_line.start("executing")
    # A human's half-typed command, with NO terminator -- exactly the dangerous state.
    os.write(pty_line.master_fd, b"shutdown -h now")

    result = _probe(pty_line, deadline_seconds=2.0)

    assert result.outcome is events_mod.ProbeOutcome.SEEN  # the probe still worked
    submitted = b"".join(far.executed)
    assert b"shutdown" not in submitted, f"the probe SUBMITTED a pending line: {submitted!r}"


def test_the_probe_drains_the_input_queue_before_it_writes(monkeypatch, pty_line) -> None:
    # T-03-48/T-03-50. The tty's input queue is finite: once it is full the kernel
    # DISCARDS what arrives next, and what arrives next is the answer. Draining first is
    # also what stops a replayed buffer from an earlier probe satisfying a later one.
    # Asserted on ORDER rather than on an outcome, because a probe with no drain still
    # empties the queue incidentally through its own reads, so an outcome assertion
    # would not discriminate.
    # Kills: dropping the drain; moving the drain after the write; flushing the OUTPUT
    #        queue (TCOFLUSH) instead of the input queue.
    queues: list[int] = []
    wire_had_bytes_at_flush_time: list[bool] = []
    real_tcflush = termios.tcflush

    def spy(fd: int, queue: int) -> None:
        queues.append(queue)
        readable, _w, _x = select.select([pty_line.master_fd], [], [], 0)
        wire_had_bytes_at_flush_time.append(bool(readable))
        real_tcflush(fd, queue)

    monkeypatch.setattr(events_mod.termios, "tcflush", spy)
    pty_line.start("silent")

    _probe(pty_line, deadline_seconds=0.2)

    assert queues == [termios.TCIFLUSH]
    assert wire_had_bytes_at_flush_time == [False]  # not even the \r nudge yet


def test_queued_junk_does_not_prevent_a_match(pty_line) -> None:
    # Junk both BEFORE the probe (a console that spewed while we were not looking) and
    # DURING it (twice the buffer cap, with the answer split across two writes).
    # Kills: capping the buffer from the wrong end (`del buf[CAP:]` keeps the OLDEST
    #        bytes and throws the answer away); and scanning only the chunk just read
    #        instead of the accumulated window, which loses a token split across reads.
    preloaded = pty_line.preload(b"stale console output\r\n" * 200)
    assert preloaded > 0
    pty_line.start("chatty")

    result = _probe(pty_line, deadline_seconds=2.0)

    assert result.outcome is events_mod.ProbeOutcome.SEEN


def test_regular_file_is_no_transport_and_is_not_truncated(tmp_path) -> None:
    # T-03-51. The config-side /dev/ prefix check does not cover this: a path under
    # /dev/ can still be a regular file, and a hand-built call never passes through the
    # validator at all. The second assertion is what makes this discriminating — a
    # mutant that drops the guard changes the FILE, not just the outcome.
    # Kills: dropping the S_ISCHR guard.
    victim = tmp_path / "not-a-device"
    victim.write_bytes(b"important bytes\n")
    configured: list[tuple[str, int]] = []

    result = events_mod.serial_liveness_probe(
        str(victim),
        115200,
        deadline_seconds=0.2,
        settle_seconds=0.0,
        configure_line=lambda d, b: (configured.append((d, b)), (0, ""))[1],
    )

    assert result.outcome is events_mod.ProbeOutcome.NO_TRANSPORT
    assert victim.read_bytes() == b"important bytes\n"
    assert configured == []  # the guard runs first: not even the line is configured


def test_line_configuration_failure_is_no_transport_and_writes_nothing(pty_line) -> None:
    # Kills: proceeding past a failed line configuration — at which point the baud is
    #        whatever the line happened to be set to, and the bytes go out garbled.
    pty_line.start("executing")

    result = _probe(pty_line, configure_line=lambda _d, _b: (1, "stty: invalid argument"))

    assert result.outcome is events_mod.ProbeOutcome.NO_TRANSPORT
    assert "invalid argument" in result.detail
    assert bytes(pty_line.far_end.received) == b""


def test_probe_leaks_no_file_descriptor(tmp_path, pty_line) -> None:
    # T-03-52. This runs on EVERY poll for the whole grace window, so a descriptor
    # leaked here exhausts the daemon's fd table mid-outage — including one leaked on
    # the SUCCESS path, which is why `finally` and not `except` is the right shape.
    # Kills: moving the close out of the finally; closing only on the failure paths.
    regular = tmp_path / "plain"
    regular.write_bytes(b"x")
    pty_line.start("executing")
    before = len(os.listdir("/proc/self/fd"))

    _probe(pty_line)  # SEEN
    _probe(pty_line, configure_line=lambda _d, _b: (1, "no"))  # NO_TRANSPORT (config)
    events_mod.serial_liveness_probe(  # NO_TRANSPORT (not a character device)
        str(regular), 115200, deadline_seconds=0.1, settle_seconds=0.0
    )
    pty_line.far_end.stop()
    _probe(pty_line, deadline_seconds=0.2)  # NOT_SEEN

    assert len(os.listdir("/proc/self/fd")) == before


def test_probe_never_raises(pty_line) -> None:
    # The runner contract in this module is a RETURNED value. A raise out of here would
    # unwind the poll loop from inside the grace window, which is the T-02-24 class.
    # Every seam the probe touches is failed in turn, and two of them are failed with
    # exceptions that are NOT OSError subclasses, because those are the ones a narrowed
    # catch lets through and both are reachable in production: `stty` hanging raises
    # subprocess.TimeoutExpired (a SubprocessError — the same trap the write transport's
    # own timeout test pins), and draining a character device that is not a tty raises
    # termios.error, which descends straight from Exception. TimeoutError is NOT one of
    # them: it is an OSError subclass, so the write deadline would not discriminate here.
    # Kills: narrowing the catch to OSError; removing the catch altogether.
    pty_line.start("silent")
    boom = OSError(5, "Input/output error")
    real_stat, real_open, real_write, real_read = os.stat, os.open, os.write, os.read

    def _for_device(real):
        def _f(path, *a, **k):
            if path == pty_line.device:
                raise boom
            return real(path, *a, **k)

        return _f

    def _for_our_fd(real):
        def _f(fd, *a, **k):
            if fd not in (0, 1, 2):
                raise boom
            return real(fd, *a, **k)

        return _f

    def _boom_configure(_device: str, _baud: int) -> tuple[int, str]:
        raise subprocess.TimeoutExpired(cmd="stty", timeout=5)

    def _boom_drain(_fd: int, _queue: int) -> None:
        raise termios.error(25, "Inappropriate ioctl for device")

    for seam in ("stat", "configure", "open", "write", "read", "drain"):
        with pytest.MonkeyPatch.context() as ctx:
            configure = None
            if seam == "stat":
                ctx.setattr(events_mod.os, "stat", _for_device(real_stat))
            elif seam == "configure":
                configure = _boom_configure
            elif seam == "open":
                ctx.setattr(events_mod.os, "open", _for_device(real_open))
            elif seam == "write":
                ctx.setattr(events_mod.os, "write", _for_our_fd(real_write))
            elif seam == "read":
                ctx.setattr(events_mod.os, "read", _for_our_fd(real_read))
            elif seam == "drain":
                ctx.setattr(events_mod.termios, "tcflush", _boom_drain)
            result = _probe(pty_line, deadline_seconds=0.2, configure_line=configure)

        assert isinstance(result, events_mod.ProbeResult), seam
        assert result.outcome in tuple(events_mod.ProbeOutcome), seam


def test_each_probe_uses_a_fresh_nonce(pty_line) -> None:
    # T-03-48. A module-level constant nonce would let console bytes replayed from an
    # earlier probe — or a buffer that was never drained — satisfy a later one.
    # Kills: a constant or otherwise reused nonce.
    pty_line.start("silent")

    _probe(pty_line, deadline_seconds=0.2)
    _probe(pty_line, deadline_seconds=0.2)

    nonces = re.findall(rb'UPSPRO""BE-([0-9a-f]+)', bytes(pty_line.far_end.received))
    assert len(nonces) == 2
    assert nonces[0] != nonces[1]


def test_the_write_only_shutdown_transport_is_unchanged(monkeypatch) -> None:
    # A behaviour lock. Every existing caller of the write transport depends on its
    # narrow contract and on its documented honesty about what rc=0 means: bytes were
    # written, and nothing whatever about the far end. Retrofitting read-back into it
    # would silently change what a zero return code means at `_fire_target` and at
    # `shutdown rehearse`.
    # Kills: "helpfully" giving the write path a read-back; opening it O_RDWR.
    reads: list[int] = []
    real_read = os.read
    monkeypatch.setattr(
        events_mod.os, "read", lambda fd, n: (reads.append(fd), real_read(fd, n))[1]
    )
    wiring = _wire_serial(monkeypatch, cmd_written=len(b"poweroff\n"))

    result = _default_serial_shutdown(_serial_target())

    assert result == (0, "", "")  # a 3-tuple, not a ProbeResult
    assert reads == []  # nothing was read back
    (_path, flags) = wiring.opened[0]
    assert flags & os.O_ACCMODE == os.O_WRONLY
    assert not flags & os.O_RDWR


def test_no_probe_test_names_the_real_serial_device() -> None:
    # T-03-53, graded CRITICAL: a test that opened the live console would be typing into
    # a root-capable auto-login shell on a running PowerEdge. The conftest tripwire
    # covers process spawns, NOT `os.open`, so it would not stop one. This is the
    # structural check that does — the plan specified it as a grep over a dedicated
    # probe module; these tests share `test_events.py` with the write-transport tests,
    # which legitimately name a device, so the check is scoped to the probe section.
    source = inspect.getsource(sys.modules[__name__])
    section = source[source.index("# The network-independent read-back liveness probe") :]

    for needle in _REAL_DEVICE_NEEDLES:
        assert needle not in section, f"a probe test names the real console: {needle}"


# --- an alarm that cannot close is a broken alarm ---
#
# On 2026-07-28 all three UPSes on this host paged COMMUNICATION LOST within one second
# and not one was ever closed. The hardware was fine the whole time. Cause: `nut-server`
# was restarted, which restarts `nut-monitor` too; upsmon fires COMMOK only on a
# lost->ok transition IT observed, so the dying process sent COMMBAD and the fresh one
# had no memory that anything was lost. The operator carried three open alarms for two
# days. The poll loop can see recovery directly, so it closes them.


def test_commbad_marks_state_so_the_poll_loop_can_close_it() -> None:
    ups = make_ups("ups1")
    state = UpsState()
    deps, _ = make_deps(FakeNotifier(), snap(""))

    events_mod.handle_commbad(ups, state, deps)

    assert state.commbad_notified is True


def test_tick_closes_an_open_comm_alarm_once_the_ups_reads_again() -> None:
    ups = make_ups("ups1")
    state = UpsState(commbad_notified=True)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))

    events_mod.handle_tick(ups, state, deps)

    titles = [n.title for n in notifier.sent]
    assert any("COMMUNICATION RESTORED" in t for t in titles), titles
    assert state.commbad_notified is False  # and it does not re-send next tick


def test_tick_leaves_the_alarm_standing_while_the_ups_is_still_unreadable() -> None:
    """An empty status is what read_snapshot returns when upsc cannot reach the UPS."""
    ups = make_ups("ups1")
    state = UpsState(commbad_notified=True)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap(""))

    events_mod.handle_tick(ups, state, deps)

    assert not any("COMMUNICATION RESTORED" in n.title for n in notifier.sent)
    assert state.commbad_notified is True


def test_tick_does_not_invent_a_restore_when_no_alarm_was_open() -> None:
    ups = make_ups("ups1")
    state = UpsState(commbad_notified=False)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))

    events_mod.handle_tick(ups, state, deps)

    assert not any("COMMUNICATION RESTORED" in n.title for n in notifier.sent)


def test_commok_clears_the_flag_so_the_tick_does_not_double_send() -> None:
    ups = make_ups("ups1")
    state = UpsState(commbad_notified=True)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"))

    events_mod.handle_commok(ups, state, deps)
    assert state.commbad_notified is False

    notifier.sent.clear()
    events_mod.handle_tick(ups, state, deps)
    assert not any("COMMUNICATION RESTORED" in n.title for n in notifier.sent)


# --- UNREADABLE is not ON UTILITY POWER ---
#
# `snap.on_battery` is False when status is None, so an unreadable UPS used to fall into
# the "not on battery" branch: it sent POWER RESTORED during a live outage and reset
# `onbatt_since` and `shutdowns_sent`. Since min_on_battery_seconds is measured FROM
# onbatt_since, every unreadable poll restarted the 180s countdown — at poll_seconds 10,
# a driver flapping faster than the gate would hold mt and spark below the threshold for
# an entire outage. This host has had days where every upsc read was refused.


def test_an_unreadable_ups_does_not_reset_the_outage_timer() -> None:
    ups = make_ups("ups1")
    state = UpsState(onbatt_since=1000, onbatt_notified=True, shutdowns_sent=["mt"])
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap(""), now=1100)  # unreadable: empty status

    events_mod.handle_tick(ups, state, deps)

    assert state.onbatt_since == 1000, "the 180s countdown was restarted by a failed read"
    assert state.shutdowns_sent == ["mt"], "the fire-once ledger was wiped by a failed read"


def test_an_unreadable_ups_does_not_announce_power_restored() -> None:
    ups = make_ups("ups1")
    state = UpsState(onbatt_since=1000, onbatt_notified=True)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap(""), now=1100)

    events_mod.handle_tick(ups, state, deps)

    titles = [n.title for n in notifier.sent]
    assert not any("POWER RESTORED" in t for t in titles), titles


def test_a_genuine_return_to_utility_power_still_announces() -> None:
    """The guard must not swallow the real thing it sits next to."""
    ups = make_ups("ups1")
    state = UpsState(onbatt_since=1000, onbatt_notified=True)
    notifier = FakeNotifier()
    deps, _ = make_deps(notifier, snap("OL"), now=1100)

    events_mod.handle_tick(ups, state, deps)

    assert any("POWER RESTORED" in n.title for n in notifier.sent)
    assert state.onbatt_since is None


# --- the 180s gate is the ONLY thing between a sag and shutting two machines down ---
#
# With the live policy (battery_below 100, runtime_below null) _close_to_empty is a
# tautology, so the whole firing decision is `on_battery AND age >= 180`. That age comes
# from a wall-clock stamp on a host with no RTC. All three of these were reachable.


def test_a_forward_clock_step_does_not_grant_the_shutdown_gate() -> None:
    """A stale stamp must not turn the first poll of a brief sag into a shutdown."""
    # Recorded a full day+ ago: not a long outage, a stamp that outlived its outage.
    state = UpsState(onbatt_since=1_000_000)
    deps, _ = make_deps(FakeNotifier(), snap("OB"), now=1_000_000 + 90_000)

    assert events_mod._outage_age(state, deps) is None


def test_a_backward_clock_step_is_refused_not_silently_clamped() -> None:
    """max(0, ...) hid this: the countdown restarted with nothing said."""
    state = UpsState(onbatt_since=2000)
    deps, _ = make_deps(FakeNotifier(), snap("OB"), now=1000)  # clock went backwards

    assert events_mod._outage_age(state, deps) is None


def test_a_normal_outage_age_is_unaffected() -> None:
    state = UpsState(onbatt_since=1000)
    deps, _ = make_deps(FakeNotifier(), snap("OB"), now=1200)
    assert events_mod._outage_age(state, deps) == 200


def test_a_suspect_clock_blocks_firing_rather_than_permitting_it() -> None:
    """_target_should_fire reads a None age as 'not recorded yet' and refuses."""
    ups = make_ups("ups1", shutdown_policy=shutdown_policy(min_on_battery_seconds=180))
    state = UpsState(onbatt_since=1_000_000)
    deps, _ = make_deps(FakeNotifier(), snap("OB"), now=1_000_000 + 90_000)
    target = _serial_target(baud=115200)

    ok, why = events_mod._target_should_fire(ups, state, deps, target, snap("OB"))

    assert ok is False
    assert "not recorded yet" in why
