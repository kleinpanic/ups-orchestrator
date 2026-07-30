"""Event handlers — the orchestrator's brain.

Two decoupled concerns share these handlers:

* **NUT-event webhooks** — ``onbatt``/``online``/``lowbatt``/``commbad``/``commok``
  fire from NUT's ``upssched`` and post per-UPS Discord embeds. NUT's own
  ``upsmon`` ``SHUTDOWNCMD`` remains the backstop that powers off this host.
* **Polling-driven shutdown** — the ``tick`` handler (run repeatedly by the
  ``watch`` loop at a configurable interval) can run configured
  ``shutdown_targets`` only when the top-level shutdown policy explicitly opts
  in, the UPS is on battery long enough, and the UPS is close to empty.
  ``local`` targets are always sequenced **after** every enabled ``remote``
  target on the same UPS, so the watcher host dies last. The on-battery
  countdown post has its own cadence and never gates shutdown decisions.

Side effects (snapshot reads, shutdowns, clock) are injected via :class:`Deps`
so the handlers unit-test without a real UPS or network.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import secrets
import select
import shlex
import stat
import subprocess
import termios
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from ups_orchestrator.config import (
    SERIAL_DEVICE_SHAPES,
    LoadStepPolicy,
    MonitoredMachine,
    ShutdownGroupPolicy,
    ShutdownTarget,
    UpsConfig,
    canonical_ups_key,
    is_serial_device_path,
)
from ups_orchestrator.notify import Level, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import UpsState

LOG = logging.getLogger("ups_orchestrator.events")

EventLogger = Callable[[str, UpsConfig, UpsSnapshot | None, str, dict[str, object] | None], None]


# --- default side effects (overridable in tests) -----------------------------


def ssh_dest(target: ShutdownTarget) -> str:
    """SSH destination: ``user@host`` if a user is set, else just ``host``.

    Leaving ``user`` empty lets ``host`` be an ``ssh_config`` Host alias (e.g.
    ``mt``), so connection details (real hostname, port, key) live in
    ``~/.ssh/config`` rather than the orchestrator config.
    """
    return f"{target.user}@{target.host}" if target.user else target.host


# A transport runner's contract is *return a failure tuple, never raise*. The caller
# appends to ``state.shutdowns_sent`` AFTER the runner returns and holds the local
# targets until every remote has been sent, so a runner that escapes leaves the target
# unmarked and the local hosts unreached — the watcher Pi's own poweroff starves on the
# battery it shares with the machine that hung (T-02-24). Hence the broad catch: it is
# the contract, not laziness.
def _default_ssh_shutdown(target: ShutdownTarget) -> tuple[int, str, str]:
    dest = ssh_dest(target)
    # BL-01 belt-and-braces at the sink. `config.validate_legacy_targets` disarms an
    # option-shaped host/user at load, but a hand-constructed target never passes
    # through the validator. `--` terminates OpenSSH's option parsing, so a leading
    # '-' in dest is read as a destination rather than as `-oProxyCommand=...`.
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "--", dest, target.cmd]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=20, check=False)
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        return 1, "", f"ssh transport to {dest} failed: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _default_local_shutdown(cmd: str) -> tuple[int, str, str]:
    try:
        # shlex.split raises ValueError on an unbalanced quote and subprocess.run
        # raises IndexError on an empty argv, both before any process exists.
        proc = subprocess.run(
            shlex.split(cmd), capture_output=True, text=True, timeout=20, check=False
        )
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        return 1, "", f"local transport ({cmd!r}) failed: {exc}"
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


# A line configurator's contract: ``(device, baud) -> (returncode, stderr)``. Extracted
# so the read-back probe below can be unit-tested against a pty WITHOUT reaching a real
# `stty` — `tests/conftest.py` arms a tripwire on every process-spawning entry point and
# allows only getfacl/setfacl, and the right answer to that is an injected seam (the shape
# ``Deps`` already uses everywhere else), not an `allow_subprocess` exemption.
LineConfigurator = Callable[[str, int], tuple[int, str]]


def _configure_serial_line(device: str, baud: int) -> tuple[int, str]:
    """Configure the LOCAL tty. Returns ``(returncode, stderr)``; never raises usefully.

    HI-04: `raw` touches ignbrk/brkint/…/icanon/opost/isig and NOTHING in c_cflag, so it
    sets neither clocal nor -crtscts. `clocal` is the correct setting for the 3-wire
    TX/RX/GND console this transport targets — it never asserts DCD — and makes the
    carrier question moot for the blocking write and the close() that follow, rather than
    only for the open(). Without -crtscts a cable with no CTS can block the write, and
    close() can block in tty_wait_until_sent for closing_wait (30 s) inside the poll loop.
    `raw` additionally clears `icanon`, which is what lets the probe's reads return bytes
    as they arrive instead of one line at a time.

    What a zero return code proves is LOCAL only: `stty -F <dev> <rate>` returns 0 for
    9600, 19200, 115200 and 0 alike. It catches a MALFORMED rate, never a mismatched far
    end. `serial_liveness_probe` is the only thing in this module that can observe the
    far end.
    """
    proc = subprocess.run(
        ["stty", "-F", device, str(baud), "raw", "-echo", "clocal", "-crtscts"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    return proc.returncode, proc.stderr.strip()


def _default_serial_shutdown(target: ShutdownTarget) -> tuple[int, str, str]:
    """Send ``cmd`` to a serial console (assumes a passwordless/auto-login getty).

    Network-independent: works during an outage when SSH can't reach the box.

    Success means two things and no more: the LOCAL tty was configured at the declared
    baud, and the bytes were written. The far end is never read back, so a far-end speed
    MISMATCH is **not** detectable here — ``stty -F <dev> <rate>`` returns 0 for 9600,
    19200, 115200 and 0 alike, and the payload write completes at any of them. What the
    captured return code catches is a MALFORMED rate or a line that could not be
    configured locally. Bidirectional readback is deferred as OQ-02.
    """
    try:
        # Cheapest and most destructive-to-get-wrong checks first.
        if target.baud is None:
            # 02-06's strict parser yields None for a declared-but-unparseable baud.
            # Reachable here because a hand-constructed target never passes through
            # ``validate_active_transports``. Rendering it would run `stty -F <dev> None`.
            return (
                1,
                "",
                f"serial target {target.name} has no usable baud rate; declare a "
                f"positive integer serial_baud (must match the far end's getty rate). "
                f"Nothing was sent to {target.device}.",
            )
        mode = os.stat(target.device).st_mode
        if not stat.S_ISCHR(mode):
            # Defence in depth over 02-06's config-side /dev/ prefix check, and
            # deliberately not redundant with it: a path under /dev/ can still be a
            # regular file, which the "wb" open below would TRUNCATE and then report
            # success for.
            return (
                1,
                "",
                f"serial device {target.device} is not a character device "
                f"(mode {stat.filemode(mode)}); refusing to write to it. Check the "
                f"serial_device path for a typo.",
            )
        # The stty invocation itself moved to `_configure_serial_line` so the probe can
        # share it through an injected seam. The argv, the timeout and the check=False
        # decision are unchanged — check=False keeps this the single decision point;
        # raising CalledProcessError instead would just escape into the handler below.
        stty_rc, stty_err = _configure_serial_line(target.device, target.baud)
        if stty_rc != 0:
            return (
                1,
                "",
                f"could not configure the local serial line {target.device} at "
                f"{target.baud} baud (stty rc={stty_rc}: "
                f"{stty_err or '(no stderr)'}); the shutdown command was "
                f"NOT sent. This says nothing about the far end's line speed.",
            )
        # T-02-25: open NON-BLOCKING, then clear the flag. `stty raw` does not set
        # clocal and the kernel's default termios leaves CLOCAL clear, so a blocking
        # open on a tty waits for DCD — which a 3-wire TX/RX/GND console cable never
        # asserts. The open would then never return: handle_tick never returns, the
        # poll loop wedges, every UPS stops being polled, and Restart=always never
        # fires because the process is still alive. Clearing the flag immediately
        # restores the blocking write semantics the short-write guard depends on.
        # (A manual `stty -F` smoke test never reproduces the hang: GNU stty already
        # opens O_RDONLY|O_NONBLOCK.)
        #
        # HI-04: O_NOCTTY as well. systemd puts each service in its own session, so
        # this process is a session leader with no controlling terminal — and under
        # POSIX such a process opening a tty WITHOUT O_NOCTTY acquires that tty as its
        # controlling terminal. A carrier transition on the line would then deliver
        # SIGHUP to the session and kill the daemon mid-outage. Unconditionally correct
        # for a daemon, and it costs nothing.
        fd = os.open(target.device, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            flags = fcntl.fcntl(fd, fcntl.F_GETFL)
            fcntl.fcntl(fd, fcntl.F_SETFL, flags & ~os.O_NONBLOCK)
            # LO-01: the fdopen belongs INSIDE this try. It sat below, so an OSError
            # there leaked the descriptor — once per poll, for the whole outage, until
            # the daemon's fd table was exhausted. Once fdopen returns, the file object
            # owns the fd and the `with` closes it.
            port = os.fdopen(fd, "wb", buffering=0)
        except Exception:
            os.close(fd)
            raise
        with port:
            port.write(b"\r")  # nudge the shell to a fresh prompt
            time.sleep(0.5)
            payload = (target.cmd + "\n").encode()
            written = port.write(payload)
        if written != len(payload):
            # Unbuffered write returned short — the far end likely isn't reading
            # (device unplugged / no getty). Report failure, not false success.
            return 1, "", f"short serial write: {written}/{len(payload)} bytes to {target.device}"
        return 0, "", ""
    except Exception as exc:  # noqa: BLE001 - the runner's contract is a tuple, never a raise
        # OSError alone let subprocess.TimeoutExpired escape straight past this.
        return 1, "", f"serial transport to {target.device} failed: {exc}"


# --- serial read-back liveness probe ------------------------------------------
#
# Deliberately ALONGSIDE `_default_serial_shutdown`, never inside it. Every existing
# caller of the write transport depends on its narrow contract and on its documented
# honesty about what a zero return code means (`_fire_target` below, `shutdown rehearse`
# in cli.py). The probing path may make a stronger claim; the plain transport keeps
# saying only what it can prove.


class ProbeOutcome(Enum):
    """What the probe OBSERVED — never what should be done about it.

    The names are observations on purpose. "ALIVE"/"SILENT" would smuggle an inference
    into this module, and the inference is contested: a silent line can mean the far end
    halted (the native shutdown took) or that a pager is sitting on its console (the far
    end is fine and about to be power-cut). Mapping an outcome to an action is a policy
    decision that belongs to the fallback caller, taken explicitly.
    """

    SEEN = "seen"  # the token came back: the far end EXECUTED something
    NOT_SEEN = "not_seen"  # the line worked; the token did not come back in time
    NO_TRANSPORT = "no_transport"  # there was no usable line to probe down at all


@dataclass(frozen=True)
class ProbeResult:
    outcome: ProbeOutcome
    elapsed_seconds: float
    detail: str


# The whole probe, including its retry, must fit inside this — it runs from the poll
# loop, and the bound that matters is on handle_tick, not on one attempt. 3.0 s with one
# retry is the research's budget (~6 ms of wire time at 115200, plus bash readline, plus
# the settle); it is an ASSUMPTION (research A2), not a measurement on this link.
SERIAL_PROBE_DEADLINE_SECONDS = 3.0
# One retry, to cover a getty respawning at exactly the wrong moment.
SERIAL_PROBE_RETRIES = 1
# The same nudge-then-settle the write transport uses to get bash to a fresh prompt.
SERIAL_PROBE_SETTLE_SECONDS = 0.5
# A chatty console must not make the probe allocate unboundedly. Only the most recent
# bytes are kept, so a token that just arrived is always intact inside the window.
_PROBE_BUFFER_CAP = 8192
# The token, in two halves. The typed text joins them with an empty-string literal that
# only the far end's shell removes, so the joined form exists in the far end's OUTPUT and
# never in our INPUT. Keep them separate: deriving the expected match from the typed text
# by stripping the quotes is exactly the refactor that silently destroys the design.
_PROBE_TOKEN_HEAD = "UPSPRO"
_PROBE_TOKEN_TAIL = "BE-"


def _probe_write(fd: int, payload: bytes, deadline_at: float) -> None:
    """Write every byte, or raise. Non-blocking, so short writes are the normal case."""
    view = memoryview(payload)
    while view:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"only {len(payload) - len(view)}/{len(payload)} bytes written")
        _r, writable, _x = select.select([], [fd], [], remaining)
        if not writable:
            continue  # select timed out; the deadline check above ends the loop
        try:
            view = view[os.write(fd, view) :]
        except BlockingIOError:
            continue


def _probe_scan(fd: int, expect: bytes, deadline_at: float) -> bool:
    """Read until ``expect`` appears or the deadline passes."""
    buf = bytearray()
    while True:
        remaining = deadline_at - time.monotonic()
        if remaining <= 0:
            return False
        readable, _w, _x = select.select([fd], [], [], remaining)
        if not readable:
            return False
        try:
            chunk = os.read(fd, 4096)
        except BlockingIOError:
            continue  # select can wake spuriously
        if not chunk:
            return False  # the far end closed the line
        buf += chunk
        del buf[:-_PROBE_BUFFER_CAP]  # keep the most RECENT bytes, never the oldest
        # Rescan the whole retained window after every read: the token routinely arrives
        # split across two reads, and console spew routinely arrives between the two.
        if expect in buf:
            return True


def serial_liveness_probe(
    device: str,
    baud: int,
    *,
    deadline_seconds: float = SERIAL_PROBE_DEADLINE_SECONDS,
    retries: int = SERIAL_PROBE_RETRIES,
    settle_seconds: float = SERIAL_PROBE_SETTLE_SECONDS,
    configure_line: LineConfigurator = _configure_serial_line,
) -> ProbeResult:
    r"""Ask the far end to run something, and watch for the answer on the same line.

    Network-independent by construction: nothing here touches an IP. That is the point —
    the failure this exists for is a dead switch, which kills ICMP, ssh and upsd's own
    client list alike, and kills them in the direction that reads as "the far end is
    gone" when the far end is in fact fine.

    **The split token is the whole design.** The typed text is
    ``echo UPSPRO""BE-<nonce>``; the string matched in the read-back is
    ``UPSPROBE-<nonce>``. The shell concatenates the two halves only when it EXECUTES the
    line, so the matched form is never among the bytes written. A match therefore proves
    execution rather than echo — a bash at a prompt echoes the typed line before running
    it, so a plain-nonce probe matches its own keystrokes — and it is immune to a
    TX-to-RX loopback or a shorted cable, which can only ever return what was sent. A
    prompt regex was rejected for a different reason: ``PS1`` belongs to the operator, and
    coupling the orchestrator to a string it does not own is the trap ``safe_text``
    exists to defuse. DCD/CTS was rejected because this cable is the 3-wire TX/RX/GND
    console documented in ``_configure_serial_line`` that never asserts DCD, which is
    precisely why ``clocal`` is set.

    A match proves four things at once: bytes reached the far end at the right rate, came
    BACK at the right rate, a shell exists at a prompt, and the far-end kernel is still
    scheduling userspace — i.e. the box is not merely powered, it is running.

    **This is the bidirectional acknowledgement OQ-02 named.** ``stty -F <dev> <rate>``
    returns 0 for 9600, 19200, 115200 and 0 alike, so a MISMATCHED far-end baud has been
    undetectable, and the project locked that as an invariant no doc or log line may
    claim otherwise. A mismatched rate garbles the read-back and the token never matches,
    so for the first time the condition is observable here. Retiring that invariant is a
    deliberate amendment owned by a later plan; until it lands, the write transport's
    docstring above still says only what the write transport can prove.

    Returns an OBSERVATION. It decides nothing, fires nothing, and is not reachable from
    any shutdown path.
    """
    started = time.monotonic()

    def _result(outcome: ProbeOutcome, detail: str) -> ProbeResult:
        return ProbeResult(outcome, round(time.monotonic() - started, 3), detail)

    try:
        # Independent of the config-side allowlist, on purpose. This function takes a
        # device as an ARGUMENT and is reachable from callers that never went through
        # Config.load, so it cannot assume that validation ran. The specific hazard is
        # /dev/watchdog: a character device under /dev/ that satisfies the S_ISCHR check
        # below, and which the O_RDWR open + close further down ARMS -- rebooting this
        # host a minute later. This host is the NUT primary; it must not be rebooted by
        # a diagnostic.
        if not is_serial_device_path(device):
            return _result(
                ProbeOutcome.NO_TRANSPORT,
                f"{device} is not a serial console device (expected "
                f"{SERIAL_DEVICE_SHAPES}); nothing was opened",
            )
        # Cheapest and most destructive-to-get-wrong check first, and NOT redundant with
        # the allowlist above: a path under /dev/tty can still be a regular file, which
        # an unguarded write would truncate and then report success for.
        mode = os.stat(device).st_mode
        if not stat.S_ISCHR(mode):
            return _result(
                ProbeOutcome.NO_TRANSPORT,
                f"{device} is not a character device (mode {stat.filemode(mode)}); "
                f"nothing was written to it",
            )
        rc, err = configure_line(device, baud)
        if rc != 0:
            return _result(
                ProbeOutcome.NO_TRANSPORT,
                f"could not configure the local serial line {device} at {baud} baud "
                f"(stty rc={rc}: {err or '(no stderr)'}); nothing was probed",
            )
        # Three flags, three separate reasons. Two of them differ from the write path
        # above ON PURPOSE — if you are here to make them match, read this first.
        #
        # O_RDWR, not O_WRONLY: `os.read` on a write-only descriptor raises EBADF. The
        #   entire point of this function is the read. Mandatory, not stylistic.
        # O_NOCTTY, same as the write path: systemd puts the service in its own session,
        #   so this process is a session leader with no controlling terminal, and under
        #   POSIX such a process opening a tty WITHOUT O_NOCTTY acquires it as its
        #   controlling terminal — after which a carrier transition delivers SIGHUP and
        #   kills the daemon mid-outage. That applies MORE strongly to a long-lived read
        #   than to a short write.
        # O_NONBLOCK, and — unlike the write path — NEVER CLEARED: the write path clears
        #   it to restore the blocking-write semantics its short-write guard depends on.
        #   Clearing it here would be the T-02-25 hang class this project graded
        #   CRITICAL: a blocking read on a line nothing answers never returns, handle_tick
        #   never returns, the poll loop wedges, every UPS stops being polled, and
        #   Restart=always never fires because the process is still alive. The probe also
        #   *wants* non-blocking, so `select` can enforce a deadline at all.
        fd = os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
        try:
            attempts = max(1, retries + 1)
            for attempt in range(attempts):
                left = deadline_seconds - (time.monotonic() - started)
                if left <= 0:
                    break
                # Split what is left evenly over the attempts still to come, so a first
                # attempt against a silent line cannot eat the whole budget and starve
                # the retry — the retry exists for a getty respawning at exactly the
                # wrong moment, which is a sub-second window.
                deadline_at = time.monotonic() + left / (attempts - attempt)
                # Queued console spew can push the token out of the scan window, and the
                # tty's input queue is finite — once it is full the kernel DISCARDS what
                # arrives next, which would include the answer.
                termios.tcflush(fd, termios.TCIFLUSH)
                nonce = secrets.token_hex(6)
                # Two separate expressions, never one derived from the other.
                typed = f'echo {_PROBE_TOKEN_HEAD}""{_PROBE_TOKEN_TAIL}{nonce}\r'.encode()
                expect = f"{_PROBE_TOKEN_HEAD}{_PROBE_TOKEN_TAIL}{nonce}".encode()
                # Ctrl-U (VKILL) BEFORE the newline, never a bare "\r".
                #
                # The far end's console is a getty in CANONICAL mode, so its line
                # discipline may already hold a partially typed, unsubmitted command --
                # someone attached with screen/minicom, started typing, and walked away.
                # A bare "\r" SUBMITS that line. This function is reachable from
                # `monitor verify --deep`, a diagnostic an operator runs casually, so a
                # forgotten half-typed `shutdown -h now` on mt's console would be
                # executed BY THE VERIFY. VKILL discards the pending line instead, and
                # costs nothing when the buffer is already empty.
                #
                # Our own side is `raw`, which is what makes this work: the byte goes out
                # the wire unmodified and is interpreted by the FAR end's line discipline,
                # not swallowed by ours.
                _probe_write(fd, b"\x15\r", deadline_at)
                time.sleep(max(0.0, min(settle_seconds, deadline_at - time.monotonic())))
                _probe_write(fd, typed, deadline_at)
                if _probe_scan(fd, expect, deadline_at):
                    return _result(
                        ProbeOutcome.SEEN,
                        f"the far end executed the probe on attempt {attempt + 1} of {attempts}",
                    )
        finally:
            # In a `finally`, not an `except`: this probe runs on EVERY poll for the
            # whole grace window, so a descriptor leaked on the SUCCESS path exhausts the
            # daemon's fd table just as surely as one leaked on a failure path.
            os.close(fd)
        return _result(
            ProbeOutcome.NOT_SEEN,
            f"nothing on {device} executed the probe within {deadline_seconds:g}s "
            f"({attempts} attempt(s))",
        )
    except Exception as exc:  # noqa: BLE001 - a probe returns an observation, never raises
        # The runner contract in this module is a returned value. A raise here would
        # unwind the poll loop from inside a grace window.
        return _result(ProbeOutcome.NO_TRANSPORT, f"probing {device} failed: {exc}")


def _noop_event_log(
    _event: str,
    _ups: UpsConfig,
    _snap: UpsSnapshot | None,
    _message: str,
    _data: dict[str, object] | None,
) -> None:
    return None


@dataclass
class Deps:
    """Injectable side effects + the one poll knob the handlers need."""

    notifier: Notifier
    read_snapshot: Callable[[str], UpsSnapshot] = read_snapshot
    ssh_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_ssh_shutdown
    local_shutdown: Callable[[str], tuple[int, str, str]] = _default_local_shutdown
    serial_shutdown: Callable[[ShutdownTarget], tuple[int, str, str]] = _default_serial_shutdown
    now: Callable[[], int] = field(default_factory=lambda: lambda: int(time.time()))
    countdown_every: int = 60  # seconds between on-battery countdown posts; 0 = off
    # A transfer must persist this many seconds before the poll loop pages ON
    # BATTERY, so grid blips and battery self-tests (both brief) don't alarm.
    onbatt_notify_grace: int = 20
    event_log: EventLogger = _noop_event_log
    load_step: LoadStepPolicy = field(default_factory=LoadStepPolicy)
    sample_path: Path | None = None  # recorder JSONL, for draw-history sparklines
    # Enrolled machines, projected onto ephemeral shutdown targets by
    # ``_machine_targets``. Empty by default so a handler built without config
    # (tests, one-off dispatch) pushes to nothing.
    monitored_machines: tuple[MonitoredMachine, ...] = ()
    # When set, ``_fire_target`` returns at the TOP: no event-log line, no attempt
    # notification, no runner and — critically — no ``shutdowns_sent`` append, so a
    # preview can never poison the dedupe key that decides whether a REAL outage
    # later shuts a box down. ``remote-shutdown --dry-run`` sets it. It is a
    # belt-and-braces guarantee rather than the preview's mechanism: the preview
    # reports the gate without reaching the firing path at all, and this makes any
    # future path that does reach it under a dry run inert (T-02-13).
    dry_run: bool = False
    # LO-02: ``_machine_targets`` is re-evaluated on EVERY poll while on battery, and
    # ``_report_unprojectable`` had no rate limit at all, so one unprojectable machine
    # would page every ``poll_seconds`` for the whole outage. ``_check_load_step``
    # already solves this with a cooldown; this is the same pattern. Keyed
    # ``<ups>/<machine>`` -> last notified, mutable because ``_build_deps`` constructs
    # Deps ONCE for the life of the watch loop.
    unprojectable_cooldown: int = 600
    unprojectable_notified: dict[str, int] = field(default_factory=dict)


# --- formatting helpers -------------------------------------------------------


def fmt_duration(seconds: int | None) -> str:
    """Render a span of seconds like ``2h 5m 3s`` (omitting zero leading units)."""
    if seconds is None or seconds < 0:
        return "unknown"
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = [f"{h}h" if h else "", f"{m}m" if (m or h) else "", f"{s}s"]
    return " ".join(p for p in parts if p)


_STATUS_LABELS = {
    "OL": "Online",
    "OB": "On Battery",
    "LB": "Low Battery",
    "CHRG": "Charging",
    "DISCHRG": "Discharging",
    "RB": "Replace Battery",
    "BYPASS": "Bypass",
    "OFF": "Off",
}


def _pretty_status(status: str | None) -> str:
    if not status:
        return "Unknown"
    labelled = [_STATUS_LABELS.get(f, f) for f in status.split()]
    return f"{' · '.join(labelled)}  (`{status}`)"


def charge_bar(pct: int, width: int = 10) -> str:
    """Render a battery charge percentage as a unicode gauge, e.g. ``▰▰▰▰▰▰▰▱▱▱``."""
    pct = max(0, min(100, pct))
    filled = round(pct / 100 * width)
    return "▰" * filled + "▱" * (width - filled)


def _snapshot_fields(snap: UpsSnapshot) -> list[tuple[str, str]]:
    fields: list[tuple[str, str]] = [("Status", _pretty_status(snap.status))]
    if snap.charge is not None:
        fields.append(("Battery", f"{charge_bar(snap.charge)} **{snap.charge}%**"))
    if snap.runtime_seconds is not None:
        fields.append(("Expected time before 0%", f"~{fmt_duration(snap.runtime_seconds)}"))
    if snap.load is not None:
        if snap.estimated_load_watts is not None and snap.realpower_nominal is not None:
            fields.append(
                (
                    "Load",
                    (
                        f"{snap.load}% {snap.load_level} "
                        f"(~{snap.estimated_load_watts}/{snap.realpower_nominal} W, "
                        f"{snap.load_margin_percent}% margin)"
                    ),
                )
            )
        else:
            fields.append(("Load", f"{snap.load}% {snap.load_level}"))
    if snap.input_voltage is not None:
        fields.append(("Input voltage", f"{snap.input_voltage:.1f} V"))
    if snap.output_voltage is not None:
        fields.append(("Output voltage", f"{snap.output_voltage:.1f} V"))
    if snap.load_is_high:
        fields.append(("Load warning", "Output load is high; rebalance or move devices."))
    return fields


def _log_event(
    deps: Deps,
    event: str,
    ups: UpsConfig,
    snap: UpsSnapshot | None,
    message: str,
    data: dict[str, object] | None = None,
) -> None:
    try:
        deps.event_log(event, ups, snap, message, data)
    except Exception:  # noqa: BLE001 - logging must never break UPS handling
        LOG.exception("event log failed for %s/%s", ups.name, event)


def _notify(deps: Deps, note: Notification) -> None:
    """Send a notification on the SHUTDOWN path without ever raising out of it.

    The mirror of ``_log_event``, and load-bearing for the same reason. The three
    notify sites below sit on the path that powers machines off, and
    ``state.shutdowns_sent`` is appended only after the transport returns — so a
    notifier that raises (a dead switch mid-outage is the expected case) leaves the
    target unmarked and the local hosts unreached, which is the T-02-24 starvation the
    ``_fire_target`` backstop exists to prevent. ``_report_unprojectable`` raises out of
    the ``_machine_targets`` generator instead, i.e. before anything has fired at all.
    """
    try:
        deps.notifier.send(note)
    except Exception:  # noqa: BLE001 - a notification must never break a shutdown
        LOG.exception("shutdown notification failed")


def _record_status_transition(
    ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot
) -> None:
    if snap.status == state.last_status:
        return
    _log_event(
        deps,
        "status_transition",
        ups,
        snap,
        "UPS status changed",
        {"previous_status": state.last_status, "new_status": snap.status},
    )
    state.last_status = snap.status


# --- NUT-event handlers (Discord notifications) -------------------------------


def handle_onbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    now = deps.now()
    state.onbatt_since = now
    state.shutdowns_sent = []
    state.last_tick_notified = now  # delay first countdown by one cadence
    state.onbatt_notified = True  # this path pages now, so the poll loop won't re-page
    state.last_status = snap.status
    _log_event(deps, "onbatt", ups, snap, "Utility power lost; UPS is on battery.")
    deps.notifier.send(
        Notification(
            title=f"🔋 {ups.label} — ON BATTERY",
            body=(
                "Utility power is out and this UPS is carrying the load. "
                "Shutdown automation remains policy-gated."
            ),
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )


def handle_online(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    outage = None if state.onbatt_since is None else max(0, deps.now() - state.onbatt_since)
    paged = state.onbatt_notified  # did this outage ever page ON BATTERY?
    state.onbatt_since = None
    state.shutdowns_sent = []
    state.last_tick_notified = None
    state.onbatt_notified = False
    state.last_status = snap.status
    fields = _snapshot_fields(snap)
    if outage is not None:
        fields.insert(0, ("Outage duration", fmt_duration(outage)))
    _log_event(
        deps,
        "online",
        ups,
        snap,
        "Utility power restored.",
        {"outage_seconds": outage, "paged": paged},
    )
    # Only announce restoration if we announced the outage — a sub-grace blip or a
    # self-test transfer stays silent on both ends.
    if paged:
        deps.notifier.send(
            Notification(
                title=f"✅ {ups.label} — POWER RESTORED",
                body="Back on utility power. Shutdown state has been reset.",
                level=Level.SUCCESS,
                fields=fields,
            )
        )


def handle_lowbatt(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    _log_event(deps, "lowbatt", ups, snap, "NUT reported low battery.")
    deps.notifier.send(
        Notification(
            title=f"⚠️ {ups.label} — LOW BATTERY",
            # States the fact and NOT a prediction about this host's fate. The old text
            # promised "NUT will shut this host down (backstop)", which is false on this
            # deployment and false in the worst direction: the primary runs MINSUPPLIES 0
            # so upsmon's forceshutdown() is unreachable, and shutdown.internal.enabled is
            # false so no local target can fire. This host is the one that must SURVIVE to
            # bring the fleet back, and an alert claiming it is about to die would send an
            # operator chasing the wrong thing during the outage it matters most.
            body=(
                "Battery critical. Whether any machine is powered off is decided by the "
                "shutdown policy — run 'ups-orchestrator remote-shutdown --dry-run' to "
                "see the per-target verdict."
            ),
            level=Level.CRITICAL,
            fields=_snapshot_fields(snap),
        )
    )


def handle_commbad(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    _log_event(deps, "commbad", ups, None, "Lost contact with UPS.")
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION LOST",
            body="Lost contact with the UPS (USB/driver issue or UPS powered off).",
            level=Level.WARNING,
        )
    )


def handle_commok(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    snap = deps.read_snapshot(ups.name)
    state.last_status = snap.status
    _log_event(deps, "commok", ups, snap, "Re-established contact with UPS.")
    deps.notifier.send(
        Notification(
            title=f"🔌 {ups.label} — COMMUNICATION RESTORED",
            body="Re-established contact with the UPS.",
            level=Level.SUCCESS,
            fields=_snapshot_fields(snap),
        )
    )


# --- polling-driven shutdown --------------------------------------------------


def _target_group(ups: UpsConfig, target: ShutdownTarget) -> ShutdownGroupPolicy:
    return ups.shutdown_policy.internal if target.is_local else ups.shutdown_policy.external


def _target_location(target: ShutdownTarget) -> str:
    if target.is_local:
        return "this host (local)"
    if target.is_serial:
        return f"serial {target.device}"
    return f"{ssh_dest(target)} (ssh)"


def _outage_age(state: UpsState, deps: Deps) -> int | None:
    if state.onbatt_since is None:
        return None
    return max(0, deps.now() - state.onbatt_since)


def _threshold_status(
    label: str, value: int | None, threshold: int | None, render: Callable[[int], str]
) -> tuple[bool | None, str]:
    if threshold is None:
        return None, f"{label} threshold disabled"
    if value is None:
        return None, f"{label} unknown"
    due = value <= threshold
    return due, f"{label} {render(value)} <= {render(threshold)}"


def _close_to_empty(group: ShutdownGroupPolicy, snap: UpsSnapshot) -> tuple[bool, str]:
    battery_due, battery_reason = _threshold_status(
        "battery", snap.charge, group.battery_below, lambda pct: f"{pct}%"
    )
    runtime_due, runtime_reason = _threshold_status(
        "runtime", snap.runtime_seconds, group.runtime_below, fmt_duration
    )
    known = [result for result in (battery_due, runtime_due) if result is not None]
    reasons = [battery_reason, runtime_reason]
    if not known:
        return False, "; ".join(reasons)
    if battery_due is not None and runtime_due is not None:
        return battery_due and runtime_due, "; ".join(reasons)
    return bool(known[0]), "; ".join(reasons)


def _target_should_fire(
    ups: UpsConfig, state: UpsState, deps: Deps, target: ShutdownTarget, snap: UpsSnapshot
) -> tuple[bool, str]:
    """Return whether the target may fire and why."""
    policy = ups.shutdown_policy
    if not policy.enabled:
        return False, "shutdown policy disabled"

    group = _target_group(ups, target)
    group_name = "internal" if target.is_local else "external"
    if not group.enabled:
        return False, f"{group_name} shutdown group disabled"

    if policy.require_power_outage:
        if not snap.on_battery:
            return False, "UPS is not on battery"
        age = _outage_age(state, deps)
        if age is None:
            return False, "on-battery start was not recorded yet"
        if age < policy.min_on_battery_seconds:
            return (
                False,
                "on-battery time "
                f"{fmt_duration(age)} < {fmt_duration(policy.min_on_battery_seconds)}",
            )

    close, reason = _close_to_empty(group, snap)
    if not close:
        return False, f"UPS is not close to empty ({reason})"
    return True, f"{group_name} shutdown allowed ({reason})"


def _notify_shutdown_attempt(
    ups: UpsConfig,
    deps: Deps,
    target: ShutdownTarget,
    snap: UpsSnapshot,
    where: str,
    reason: str,
) -> None:
    if not ups.shutdown_policy.notify:
        return
    fields = _snapshot_fields(snap)
    fields.insert(0, ("Target", f"{target.name} via {where}"))
    fields.insert(1, ("Trigger", reason))
    _notify(
        deps,
        Notification(
            title=f"🛑 {ups.label} — shutdown attempt for {target.name}",
            body="The orchestrator is issuing a configured shutdown command.",
            level=Level.CRITICAL,
            fields=fields,
        ),
    )


def _notify_shutdown_result(
    ups: UpsConfig, deps: Deps, target: ShutdownTarget, rc: int, err: str, where: str
) -> None:
    if not ups.shutdown_policy.notify:
        return
    if rc == 0:
        _notify(
            deps,
            Notification(
                title=f"🛑 {ups.label} — shutdown sent to {target.name}",
                body=f"Graceful shutdown issued to {where}.",
                level=Level.CRITICAL,
            ),
        )
    else:
        _notify(
            deps,
            Notification(
                title=f"❗ {ups.label} — shutdown FAILED for {target.name}",
                body=f"rc={rc}; stderr={err or '(none)'}",
                level=Level.CRITICAL,
            ),
        )


def _fire_target(
    ups: UpsConfig,
    state: UpsState,
    deps: Deps,
    target: ShutdownTarget,
    snap: UpsSnapshot,
    reason: str,
) -> None:
    where = _target_location(target)
    # T-02-13. FIRST statement with a side effect in sight, deliberately: every line
    # below this point either tells someone a shutdown happened or makes the system
    # behave as though one did. Returning here rather than skipping the runner alone
    # is what keeps a dry run from poisoning `shutdowns_sent`.
    if deps.dry_run:
        LOG.info("[dry-run] would fire %s via %s: %s (%s)", target.name, where, target.cmd, reason)
        return
    _log_event(
        deps,
        "shutdown_attempt",
        ups,
        snap,
        "Issuing configured shutdown target command.",
        {"target": target.name, "where": where, "reason": reason},
    )
    # Backstop for the runner contract, and NOT redundant with the defaults' own
    # handlers: ``Deps`` carries injected runners (tests, any future transport) that
    # those handlers do not cover. Only the call site can guarantee the invariant that
    # matters — ``shutdowns_sent`` is always appended, so the local targets below are
    # always reached even when every remote blows up on a dead switch (T-02-24).
    #
    # HI-03: the attempt notification used to sit one line ABOVE this try, which put a
    # blocking Discord POST — up to ~16.5 s of retries against a switch the outage just
    # killed — outside the backstop's reach. It is inside now AND non-raising via
    # ``_notify``, so no path can reach the runner without the append that follows it.
    #
    # F3: it also used to sit one line ABOVE the runner, INSIDE this try, which kept the
    # backstop's guarantee but still spent the whole POST before the transport ran.
    # Measured with a 0.30 s stand-in POST, every transport waited on a full POST;
    # against a switch the outage has already killed that is ~16.5 s of dead time per
    # target, serialised, inside a gate that opens at runtime_below: 300. It is not
    # academic here: `cyberpower` powers BOTH this orchestrator and the Dell PowerEdge,
    # so time spent telling Discord about the push to mt is time subtracted from the
    # Pi's own remaining runtime, and locals fire only after every remote has been sent.
    #
    # The transport now runs FIRST. Nothing about the operator surface changes — both
    # notifications are still sent, still in attempt-then-result order, still carrying
    # the snapshot and trigger the result embed does not — they just no longer sit
    # between the decision and the wire. The event log is unaffected: the
    # `shutdown_attempt` line above is a local append and is written before the runner
    # exactly as it was.
    try:
        if target.is_local:
            rc, _out, err = deps.local_shutdown(target.cmd)
        elif target.is_serial:
            rc, _out, err = deps.serial_shutdown(target)
        else:
            rc, _out, err = deps.ssh_shutdown(target)
    except Exception as exc:  # noqa: BLE001 - an escaping runner must not strand the rest
        rc, err = 1, f"shutdown transport for {target.name} ({where}) raised: {exc}"
    # Unchanged and load-bearing: the append happens on EVERY outcome, including an rc!=0
    # and an escaping runner, so a dead remote can never strand the local host (T-02-24).
    state.shutdowns_sent.append(target.name)
    _log_event(
        deps,
        "shutdown_result",
        ups,
        snap,
        "Configured shutdown target command completed.",
        {"target": target.name, "where": where, "returncode": rc, "stderr": err},
    )
    _notify_shutdown_attempt(ups, deps, target, snap, where, reason)
    _notify_shutdown_result(ups, deps, target, rc, err, where)


def _report_unprojectable(
    ups: UpsConfig, machine: MonitoredMachine, deps: Deps | None, reason: str
) -> None:
    """Report a machine this UPS considered and then did NOT project.

    NEW-3: the dropped machine never enters ``remotes``, so it would otherwise get
    neither the ``shutdown_target_blocked`` event nor the notification every other
    non-firing decision gets — leaving mid-outage syslog, the channel least likely to
    be read, as the only trace of a machine that will not shut down. One reporting
    path for every "projected nothing" decision, not one per reason.
    """
    LOG.error("Machine %r on UPS %s was not projected: %s", machine.name, ups.name, reason)
    if deps is None:  # 02-02's two-argument call sites and tests
        return
    _log_event(
        deps,
        "shutdown_target_blocked",
        ups,
        None,
        "Enrolled machine could not be projected onto a shutdown target.",
        {"target": machine.name, "reason": reason},
    )
    # LO-02: the event line is written every time (it is the audit trail); the PAGE is
    # rate-limited, exactly as `_check_load_step` does it. This path is re-reached on
    # every poll for the whole outage.
    key = f"{ups.name}/{machine.name}"
    last = deps.unprojectable_notified.get(key)
    now = deps.now()
    if last is not None and now - last < deps.unprojectable_cooldown:
        return
    deps.unprojectable_notified[key] = now
    _notify(
        deps,
        Notification(
            title=f"❗ {ups.label} — {machine.name} will NOT be shut down",
            body=f"{machine.name} was not projected onto a shutdown target: {reason}",
            level=Level.CRITICAL,
        ),
    )


def _machine_targets(
    ups: UpsConfig, machines: Sequence[MonitoredMachine], deps: Deps | None = None
) -> Iterator[ShutdownTarget]:
    """Project this UPS's push-managed machines onto ephemeral shutdown targets.

    A machine's ``shutdown_method`` selects the *transport*; the existing
    ``ShutdownPolicy`` gate still decides *whether and when*. The projected targets
    are handed to the unchanged firing path, so no new shutdown or transport logic
    exists anywhere.

    ``native`` machines are deliberately never projected: their ``upsmon`` secondary
    powers itself off on the primary's FSD, so a push as well would shut the box
    down twice (P2-01/P2-06). ``none`` machines opted out of shutdown entirely.

    ``serial_baud`` is carried verbatim. ``_default_serial_shutdown`` runs
    ``stty -F <device> <baud>``, so a substituted baud writes garbage down the line
    and still returns rc=0 — a silent no-shutdown (P2-08).

    The association is resolved through ``canonical_ups_key`` — the SAME
    canonicalisation 02-06's detectors use — so a capitalisation cannot mean two
    different things in two modules. A blank ``ups`` matches no UPS at all, including
    one whose own name canonicalises to blank; ``Config.load`` disarms such a machine
    with a notice rather than letting it look protected.

    Projection reads EFFECTIVE state (``effective_method`` here,
    ``effective_enabled`` for the legacy targets in ``seen``), so a machine or target
    that ``Config.load`` disarmed is already excluded while its declaration on disk is
    untouched. ``Config.load`` no longer rejects a machine that collides with an
    enabled legacy target — it disarms one of them instead.

    The projected target is named after the machine, which is the
    ``state.shutdowns_sent`` dedupe key. A name already claimed on this UPS would
    make the later target a no-op with no trace, so a collision is dropped and
    reported through ``_report_unprojectable``.
    """
    ups_key = canonical_ups_key(ups.name)
    seen = {t.name.strip().lower() for t in ups.shutdown_targets if t.effective_enabled}
    for m in machines:
        if not m.ups.strip() or canonical_ups_key(m.ups) != ups_key:
            continue
        method = m.effective_method.strip().lower()
        if method == "ssh":
            target = ShutdownTarget(
                name=m.name, kind="remote", enabled=True, host=m.ssh, cmd=m.shutdown_cmd
            )
        elif method == "serial":
            if m.serial_baud is None:
                _report_unprojectable(
                    ups,
                    m,
                    deps,
                    "its declared serial_baud could not be parsed; declare a positive "
                    "integer serial_baud (must match the far end's getty rate)",
                )
                continue
            target = ShutdownTarget(
                name=m.name,
                kind="serial",
                enabled=True,
                device=m.serial_device,
                baud=m.serial_baud,
                cmd=m.shutdown_cmd,
            )
        else:
            continue
        key = m.name.strip().lower()
        if key in seen:
            _report_unprojectable(
                ups,
                m,
                deps,
                "it has a duplicate shutdown target name on this UPS; rename it or that "
                "machine will never be shut down",
            )
            continue
        seen.add(key)
        yield target


def _run_shutdown_targets(ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot) -> None:
    """Fire due targets — remotes first, locals only once all remotes are sent."""
    projected = _machine_targets(ups, deps.monitored_machines, deps)
    enabled = [t for t in (*ups.shutdown_targets, *projected) if t.effective_enabled]
    # Serial is network-independent; ssh dies with the switch. Fire serial first so a
    # collapsing network can't strand a shutdown. The sort is stable, so declared
    # order still holds within each transport.
    remotes = sorted((t for t in enabled if not t.is_local), key=lambda t: not t.is_serial)
    locals_ = [t for t in enabled if t.is_local]

    for t in remotes:
        should_fire, reason = _target_should_fire(ups, state, deps, t, snap)
        if t.name not in state.shutdowns_sent and should_fire:
            _fire_target(ups, state, deps, t, snap, reason)
        elif t.name not in state.shutdowns_sent:
            _log_event(
                deps,
                "shutdown_target_blocked",
                ups,
                snap,
                "Configured remote/serial target did not meet shutdown gate.",
                {"target": t.name, "reason": reason},
            )

    # Local hosts die last: hold until every enabled remote has been triggered.
    active_remotes = [t for t in remotes if _target_group(ups, t).enabled]
    pending = [t.name for t in active_remotes if t.name not in state.shutdowns_sent]
    if pending:
        # LO-03: this returned silently, so a held local target produced no
        # `shutdown_target_blocked` event and no other trace — the ONE non-firing
        # decision in this module that logged nothing. An operator reading the event
        # log saw the watcher host simply not appear.
        for t in locals_:
            if t.name not in state.shutdowns_sent:
                _log_event(
                    deps,
                    "shutdown_target_blocked",
                    ups,
                    snap,
                    "Local target held until every enabled remote has been sent.",
                    {"target": t.name, "reason": f"waiting on remote(s): {', '.join(pending)}"},
                )
        return
    for t in locals_:
        should_fire, reason = _target_should_fire(ups, state, deps, t, snap)
        if t.name not in state.shutdowns_sent and should_fire:
            _fire_target(ups, state, deps, t, snap, reason)
        elif t.name not in state.shutdowns_sent:
            _log_event(
                deps,
                "shutdown_target_blocked",
                ups,
                snap,
                "Configured local target did not meet shutdown gate.",
                {"target": t.name, "reason": reason},
            )


def _draw_sparkline(sample_path: Path, ups_name: str, *, minutes: int = 10) -> str:
    """Render recent outlet watts from the recorder log as a Unicode sparkline."""
    try:
        with sample_path.open("rb") as fh:
            fh.seek(0, 2)
            fh.seek(max(0, fh.tell() - 400_000))
            tail = fh.read().decode("utf-8", "replace").splitlines()[1:]
    except OSError:
        return ""
    watts: list[tuple[float, int]] = []
    for line in tail:
        try:
            d = json.loads(line)
            t = d["unix_time"]
            w = d["upses"][ups_name]["estimated_load_watts"]
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(t, (int, float)) and isinstance(w, (int, float)):
            watts.append((float(t), int(w)))
    if len(watts) < 2:
        return ""
    cutoff = watts[-1][0] - minutes * 60
    pts = [w for t, w in watts if t >= cutoff]
    if len(pts) < 2:
        return ""
    cols = 24
    step = max(1, len(pts) // cols)
    buckets = [max(pts[i : i + step]) for i in range(0, len(pts), step)][:cols]
    lo, hi = min(buckets), max(buckets)
    blocks = "▁▂▃▄▅▆▇█"
    if hi == lo:
        bar = blocks[0] * len(buckets)
    else:
        bar = "".join(blocks[(w - lo) * 7 // (hi - lo)] for w in buckets)
    return f"`{bar}` {lo}–{hi} W, last {minutes} min"


def _check_load_step(ups: UpsConfig, state: UpsState, deps: Deps, snap: UpsSnapshot) -> None:
    """Flag an output-load collapse (a downstream device dying).

    The drop is measured against the highest load in the last ``window_polls``
    polls — not just the previous poll — so a collapse that straddles a poll
    (the UPS reporting an intermediate value mid-decay) still trips. Tracking
    always runs so enabling the policy later starts with a baseline. The event
    is always logged when the threshold trips; the notification is rate-limited
    by the policy cooldown.
    """
    load = snap.load
    if load is None:
        return
    policy = ups.load_step if ups.load_step is not None else deps.load_step
    window = list(state.recent_loads)
    state.recent_loads = (window + [load])[-max(1, policy.window_polls) :]
    if not policy.enabled or not window:
        return
    peak = max(window)
    drop = peak - load
    if drop < policy.drop_percent:
        return
    # Restart the window at the collapsed level so the stale peak can't
    # re-trigger on every poll until it ages out.
    state.recent_loads = [load]
    watts = (drop * snap.realpower_nominal) // 100 if snap.realpower_nominal else None
    _log_event(
        deps,
        "load_step_drop",
        ups,
        snap,
        f"Output load fell {drop} points from its recent peak ({peak}% -> {load}%).",
        {
            "peak_load": peak,
            "new_load": load,
            "drop_points": drop,
            "estimated_watts_delta": watts,
            "window": window,
        },
    )
    now = deps.now()
    if (
        state.last_load_step_notified is not None
        and now - state.last_load_step_notified < policy.cooldown_seconds
    ):
        return
    state.last_load_step_notified = now
    watts_note = f" (≈{watts} W)" if watts is not None else ""
    sparkline = _draw_sparkline(deps.sample_path, ups.name) if deps.sample_path is not None else ""
    body = (
        f"Output load fell to {load}% from a recent high of {peak}%{watts_note}. "
        "A device on this UPS may have lost power — or just finished heavy "
        "work. Worth a reachability check."
    )
    if sparkline:
        body += f"\n\n{sparkline}"
    deps.notifier.send(
        Notification(
            title=f"📉 {ups.label} — load dropped {drop} points",
            body=body,
            level=Level.WARNING,
            fields=_snapshot_fields(snap),
        )
    )


def handle_tick(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """One poll iteration (driven by the ``watch`` loop).

    Tracks load steps every call; otherwise a no-op unless the UPS is on
    battery. Evaluates shutdown targets every call; posts a runtime countdown
    only every ``countdown_every`` seconds.
    """
    snap = deps.read_snapshot(ups.name)
    _record_status_transition(ups, state, deps, snap)
    _check_load_step(ups, state, deps, snap)
    if not snap.on_battery:
        if state.onbatt_since is not None:
            _log_event(
                deps,
                "online_detected_by_poll",
                ups,
                snap,
                "Poll loop detected utility power restored before/without online callback.",
            )
            handle_online(ups, state, deps)
        return

    now = deps.now()
    if state.onbatt_since is None:
        state.onbatt_since = now
        state.shutdowns_sent = []
        state.last_tick_notified = now
        _log_event(
            deps,
            "onbatt_detected_by_poll",
            ups,
            snap,
            "Poll loop detected on-battery state before/without onbatt callback.",
        )

    # Defer the page until the transfer has persisted past the grace window, so a
    # brief blip or a battery self-test (which also transfers to battery) that
    # clears within the window never alarms. Once sent, don't re-page this outage.
    if not state.onbatt_notified and (now - state.onbatt_since) >= deps.onbatt_notify_grace:
        state.onbatt_notified = True
        deps.notifier.send(
            Notification(
                title=f"🔋 {ups.label} — ON BATTERY",
                body=(
                    "Poll loop confirms the UPS on battery beyond the notify grace. "
                    "This covers cases where the NUT event callback is missed."
                ),
                level=Level.WARNING,
                fields=_snapshot_fields(snap),
            )
        )

    _run_shutdown_targets(ups, state, deps, snap)

    if (
        state.onbatt_notified
        and deps.countdown_every > 0
        and (
            state.last_tick_notified is None
            or (now - state.last_tick_notified) >= deps.countdown_every
        )
    ):
        state.last_tick_notified = now
        _log_event(
            deps,
            "onbatt_countdown",
            ups,
            snap,
            "UPS remains on battery; sending countdown notification.",
        )
        deps.notifier.send(
            Notification(
                title=f"⏳ {ups.label} — still on battery",
                body=f"Estimated runtime remaining: ~{fmt_duration(snap.runtime_seconds)}.",
                level=Level.WARNING,
                fields=_snapshot_fields(snap),
            )
        )


def handle_remote_shutdown(ups: UpsConfig, state: UpsState, deps: Deps) -> None:
    """Explicit trigger: evaluate configured targets now (remotes first, local last)."""
    snap = deps.read_snapshot(ups.name)
    _log_event(deps, "remote_shutdown", ups, snap, "Explicit remote shutdown trigger received.")
    if not ups.shutdown_policy.enabled:
        _log_event(
            deps,
            "remote_shutdown_skipped",
            ups,
            snap,
            "Triggered, but orchestrator-managed shutdowns are disabled.",
        )
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — shutdown skipped",
                body="Triggered, but orchestrator-managed shutdowns are disabled.",
                level=Level.INFO,
            )
        )
        return
    if ups.shutdown_policy.require_power_outage and not snap.on_battery:
        _log_event(
            deps,
            "remote_shutdown_skipped",
            ups,
            snap,
            "Triggered, but UPS is not on battery.",
        )
        deps.notifier.send(
            Notification(
                title=f"ℹ️ {ups.label} — shutdown skipped",
                body="Triggered, but the UPS is no longer on battery.",
                level=Level.INFO,
            )
        )
        return
    _run_shutdown_targets(ups, state, deps, snap)


_HANDLERS: dict[str, Callable[[UpsConfig, UpsState, Deps], None]] = {
    "onbatt": handle_onbatt,
    "online": handle_online,
    "lowbatt": handle_lowbatt,
    "commbad": handle_commbad,
    "commok": handle_commok,
    "tick": handle_tick,
    "remote_shutdown": handle_remote_shutdown,
}


def dispatch(event: str, ups: UpsConfig, state: UpsState, deps: Deps) -> bool:
    """Run the handler for ``event``. Returns False if the event is unknown."""
    handler = _HANDLERS.get(event.lower())
    if handler is None:
        LOG.warning("Unknown event %r for UPS %s", event, ups.name)
        return False
    handler(ups, state, deps)
    return True
