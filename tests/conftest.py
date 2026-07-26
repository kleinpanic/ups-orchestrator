"""Shared test fixtures and fakes."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from ups_orchestrator.config import (
    MonitoredMachine,
    ShutdownGroupPolicy,
    ShutdownPolicy,
    ShutdownTarget,
    UpsConfig,
)
from ups_orchestrator.events import Deps
from ups_orchestrator.notify import DeliveryResult, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot

# ---- the process tripwire (LO-C3) -------------------------------------------
#
# The previous tripwire was a plain helper in `test_cli.py` that four tests called
# by hand: it was opt-IN rather than armed, it was absent from `test_monitor.py` —
# the file that drives every enrollment path, i.e. every ssh/nft/systemctl seam —
# and it covered `subprocess.run` alone, so `subprocess.Popen` and the `system`,
# `popen` and `exec*` families on `os` walked straight past it. A test that forgot
# to monkeypatch a transport seam therefore reached a REAL process. It is armed for
# the whole suite now, at every process-spawning entry point this code can reach.
#
# ONE documented exception: `state._copy_acl` shells out to the platform's own
# getfacl/setfacl on every persist (T-02-SC declined a pylibacl dependency), and
# the ACL-preservation tests assert on what those two binaries actually did. They
# are local metadata tools — not a transport, not a host, not a device — so they
# are allowed through BY NAME and everything else is refused. A test that genuinely
# needs a different binary opts out with `@pytest.mark.allow_subprocess`.
_ALLOWED_BINARIES = frozenset({"getfacl", "setfacl"})

# Every stdlib entry point that can put a new process on this box. `subprocess.run`
# is built on `Popen`, so patching `run` alone leaves `Popen` — and `call`,
# `check_call` and `check_output`, which all go through it — wide open.
_SPAWN_TARGETS: tuple[tuple[Any, str], ...] = (
    (subprocess, "run"),
    (subprocess, "Popen"),
    (os, "system"),
    (os, "popen"),
    (os, "posix_spawn"),
    (os, "posix_spawnp"),
    *((os, _n) for _n in sorted(dir(os)) if _n.startswith("exec")),
)


def _allowed(args: tuple[Any, ...]) -> bool:
    """Whether this call names one of the allow-listed local metadata binaries."""
    argv = args[0] if args else None
    if isinstance(argv, (list, tuple)) and argv:
        argv = argv[0]
    if not isinstance(argv, (str, bytes, os.PathLike)):
        return False
    return os.path.basename(os.fsdecode(argv)) in _ALLOWED_BINARIES


@pytest.fixture(autouse=True)
def no_real_processes(request: pytest.FixtureRequest, monkeypatch: Any) -> Iterator[list[str]]:
    """Refuse every real process spawn for the duration of a test.

    Raising is not enough on its own: several call sites here catch broad
    exceptions deliberately (a shutdown runner must not strand the rest of the
    sweep), so an AssertionError raised inside one would be swallowed and the
    escape would go unnoticed. Every blocked call is therefore RECORDED as well,
    and the recording is asserted empty after the test body returns.

    Yields that recording, so the tripwire's own tests can request the fixture by
    name, provoke a block deliberately, and clear it.
    """
    if request.node.get_closest_marker("allow_subprocess"):
        yield []
        return

    escaped: list[str] = []

    def _blocker(label: str, real: Callable[..., Any]) -> Callable[..., Any]:
        def _blocked(*args: Any, **kwargs: Any) -> Any:
            if _allowed(args):
                return real(*args, **kwargs)
            escaped.append(f"{label}{args!r}")
            raise AssertionError(
                f"a real process escaped the test fakes: {label}{args!r}. "
                "Monkeypatch the transport seam, or mark the test "
                "@pytest.mark.allow_subprocess if it genuinely needs a process."
            )

        return _blocked

    for module, name in _SPAWN_TARGETS:
        real = getattr(module, name, None)
        if real is None:  # not present on every platform/build
            continue
        monkeypatch.setattr(module, name, _blocker(f"{module.__name__}.{name}", real))

    yield escaped

    assert not escaped, "real process spawns escaped the fakes: " + "; ".join(escaped)


class FakeNotifier(Notifier):
    """Captures notifications instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, note: Notification) -> DeliveryResult:
        self.sent.append(note)
        return DeliveryResult(configured=True, ok=True, attempts=1, status_code=204)


def make_ups(
    name: str = "ups1",
    *,
    targets: tuple[ShutdownTarget, ...] = (),
    scope: str = "remote",
    shutdown_policy: ShutdownPolicy | None = None,
) -> UpsConfig:
    return UpsConfig(
        name=name,
        label=f"Test {name}",
        shutdown_targets=targets,
        shutdown_scope=scope,
        shutdown_policy=shutdown_policy or ShutdownPolicy(),
    )


def shutdown_policy(
    *,
    enabled: bool = True,
    external_enabled: bool = True,
    internal_enabled: bool = False,
    min_on_battery_seconds: int = 0,
    external_battery_below: int | None = 15,
    external_runtime_below: int | None = 300,
    internal_battery_below: int | None = 10,
    internal_runtime_below: int | None = 120,
) -> ShutdownPolicy:
    return ShutdownPolicy(
        enabled=enabled,
        min_on_battery_seconds=min_on_battery_seconds,
        external=ShutdownGroupPolicy(
            enabled=external_enabled,
            battery_below=external_battery_below,
            runtime_below=external_runtime_below,
        ),
        internal=ShutdownGroupPolicy(
            enabled=internal_enabled,
            battery_below=internal_battery_below,
            runtime_below=internal_runtime_below,
        ),
    )


def make_deps(
    notifier: FakeNotifier,
    snapshot: UpsSnapshot,
    *,
    now: int = 1000,
    ssh_rc: int = 0,
    local_rc: int = 0,
    serial_rc: int = 0,
    countdown_every: int = 60,
    monitored_machines: tuple[MonitoredMachine, ...] = (),
    serial_capture: list[tuple[str, int, str]] | None = None,
) -> tuple[Deps, list[str]]:
    """Build Deps wired to fakes; returns (deps, shutdown_calls).

    ``calls`` records remote target names and the literal ``"local"`` for the
    local host, in the order they fire.

    ``serial_capture``, when supplied, additionally records the full
    ``(device, baud, cmd)`` the serial runner was handed. ``calls`` only holds the
    target name, which would let a wrong baud pass a test unnoticed — and a wrong
    baud is a silent no-shutdown (P2-08).

    No runner here touches a real host: every shutdown path is a closure, so a test
    can drive an on-battery-and-low snapshot without any risk of a real power-off.
    """
    calls: list[str] = []

    def _ssh(target: ShutdownTarget) -> tuple[int, str, str]:
        calls.append(target.name)
        return ssh_rc, "", "" if ssh_rc == 0 else "boom"

    def _local(_cmd: str) -> tuple[int, str, str]:
        calls.append("local")
        return local_rc, "", "" if local_rc == 0 else "boom"

    def _serial(target: ShutdownTarget) -> tuple[int, str, str]:
        calls.append(target.name)
        if serial_capture is not None:
            serial_capture.append((target.device, target.baud, target.cmd))
        return serial_rc, "", "" if serial_rc == 0 else "boom"

    deps = Deps(
        notifier=notifier,
        read_snapshot=lambda _name: snapshot,
        ssh_shutdown=_ssh,
        local_shutdown=_local,
        serial_shutdown=_serial,
        now=lambda: now,
        countdown_every=countdown_every,
        monitored_machines=monitored_machines,
    )
    return deps, calls


def snap(
    status: str, *, charge: int = 80, runtime: int = 600, load: int | None = 10
) -> UpsSnapshot:
    return UpsSnapshot(
        status=status,
        charge=charge,
        runtime_seconds=runtime,
        load=load,
        input_voltage=120.0,
        output_voltage=120.0,
        realpower_nominal=900,
    )
