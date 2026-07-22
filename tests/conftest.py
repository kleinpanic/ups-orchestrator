"""Shared test fixtures and fakes."""

from __future__ import annotations

from ups_orchestrator.config import ShutdownGroupPolicy, ShutdownPolicy, ShutdownTarget, UpsConfig
from ups_orchestrator.events import Deps
from ups_orchestrator.notify import DeliveryResult, Notification, Notifier
from ups_orchestrator.nut import UpsSnapshot


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
