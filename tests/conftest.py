"""Shared test fixtures and fakes."""

from __future__ import annotations

from ups_orchestrator.config import R630Config, UpsConfig
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
    name: str = "ups1", *, shutdown_pi: bool = False, r630: R630Config | None = None
) -> UpsConfig:
    return UpsConfig(
        name=name,
        label=f"Test {name}",
        shutdown_pi_on_lowbatt=shutdown_pi,
        r630=r630 or R630Config(),
    )


def make_deps(
    notifier: FakeNotifier,
    snapshot: UpsSnapshot,
    *,
    now: int = 1000,
    ssh_rc: int = 0,
) -> tuple[Deps, list[str]]:
    """Build Deps wired to fakes; returns (deps, shutdown_calls)."""
    calls: list[str] = []

    def _shutdown_pi() -> None:
        calls.append("pi")

    def _ssh(_r630: R630Config) -> tuple[int, str, str]:
        calls.append("ssh")
        return ssh_rc, "", "" if ssh_rc == 0 else "boom"

    deps = Deps(
        notifier=notifier,
        read_snapshot=lambda _name: snapshot,
        shutdown_pi=_shutdown_pi,
        ssh_shutdown=_ssh,
        now=lambda: now,
    )
    return deps, calls


def snap(status: str, *, charge: int = 80, runtime: int = 600) -> UpsSnapshot:
    return UpsSnapshot(
        status=status, charge=charge, runtime_seconds=runtime, load=10, input_voltage=120.0
    )
