"""Shared test fixtures and fakes."""

from __future__ import annotations

from ups_orchestrator.config import ShutdownTarget, UpsConfig
from ups_orchestrator.events import Deps
from ups_orchestrator.notify import Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot


class FakeNotifier(Notifier):
    """Captures notifications instead of sending them."""

    def __init__(self) -> None:
        self.sent: list[Notification] = []

    def send(self, note: Notification) -> None:
        self.sent.append(note)


def make_ups(
    name: str = "ups1",
    *,
    targets: tuple[ShutdownTarget, ...] = (),
) -> UpsConfig:
    return UpsConfig(name=name, label=f"Test {name}", shutdown_targets=targets)


def make_deps(
    notifier: FakeNotifier,
    snapshot: UpsSnapshot,
    *,
    now: int = 1000,
    ssh_rc: int = 0,
    local_rc: int = 0,
    countdown_every: int = 60,
) -> tuple[Deps, list[str]]:
    """Build Deps wired to fakes; returns (deps, shutdown_calls).

    ``calls`` records remote target names and the literal ``"local"`` for the
    local host, in the order they fire.
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
        return 0, "", ""

    deps = Deps(
        notifier=notifier,
        read_snapshot=lambda _name: snapshot,
        ssh_shutdown=_ssh,
        local_shutdown=_local,
        serial_shutdown=_serial,
        now=lambda: now,
        countdown_every=countdown_every,
    )
    return deps, calls


def snap(status: str, *, charge: int = 80, runtime: int = 600) -> UpsSnapshot:
    return UpsSnapshot(
        status=status, charge=charge, runtime_seconds=runtime, load=10, input_voltage=120.0
    )
