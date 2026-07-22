"""Typed configuration loading.

Config lives in a committed ``config.json`` that holds **no secrets**. The Discord
webhook is resolved at runtime from an environment variable (default
``UPS_DISCORD_WEBHOOK``) so the URL never has to live in version control. A plain
``discord_webhook_url`` in the file is still honoured as a last resort for local
setups, but ``config.example.json`` ships it empty.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


def normalize_ups_name(name: str) -> str:
    """Return the local NUT UPS name from values like ``ups@localhost``."""
    return name.strip().split("@", 1)[0]


def _as_int(value: object, default: int) -> int:
    """Coerce an untyped JSON value to int, falling back to ``default``."""
    if isinstance(value, bool):  # bool is an int subclass; treat as absent
        return default
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _as_bool(value: object, default: bool) -> bool:
    """Coerce common JSON/env-style values to bool, falling back to ``default``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        s = value.strip().lower()
        if s in ("1", "true", "yes", "y", "on", "enabled"):
            return True
        if s in ("0", "false", "no", "n", "off", "disabled"):
            return False
    return default


def _opt_int(value: object) -> int | None:
    """Coerce to int, or ``None`` if absent/blank/invalid."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.strip():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _norm_scope(value: object, default: str) -> str:
    """Normalise a shutdown-scope value to ``"remote"`` or ``"all"``.

    ``"remote"`` (and ``remote_only``) → only remote/serial targets fire.
    ``"all"`` (and ``both``) → local targets fire too, last. Unknown/blank
    values inherit ``default``.
    """
    if value is None:
        return default
    s = str(value).strip().lower()
    if s in ("all", "both"):
        return "all"
    if s in ("remote", "remote_only", "remotes"):
        return "remote"
    return default


@dataclass(frozen=True)
class ShutdownGroupPolicy:
    """Central trigger policy for one class of shutdown targets."""

    enabled: bool = False
    battery_below: int | None = None
    runtime_below: int | None = None

    @classmethod
    def from_dict(
        cls, data: object, *, default_battery: int | None, default_runtime: int | None
    ) -> ShutdownGroupPolicy:
        if not isinstance(data, dict):
            return cls(battery_below=default_battery, runtime_below=default_runtime)
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            battery_below=(
                _opt_int(data.get("battery_below")) if "battery_below" in data else default_battery
            ),
            runtime_below=(
                _opt_int(data.get("runtime_below")) if "runtime_below" in data else default_runtime
            ),
        )


@dataclass(frozen=True)
class ShutdownPolicy:
    """Global opt-in policy for orchestrator-initiated device shutdowns.

    This is intentionally a single surface above per-target transports:
    ``external`` covers SSH/serial targets and ``internal`` covers the local
    watcher host. Auto-shutdown is disabled unless ``enabled`` and the relevant
    group are both true.
    """

    enabled: bool = False
    require_power_outage: bool = True
    min_on_battery_seconds: int = 120
    notify: bool = True
    external: ShutdownGroupPolicy = field(
        default_factory=lambda: ShutdownGroupPolicy(
            enabled=False, battery_below=15, runtime_below=300
        )
    )
    internal: ShutdownGroupPolicy = field(
        default_factory=lambda: ShutdownGroupPolicy(
            enabled=False, battery_below=10, runtime_below=120
        )
    )

    @classmethod
    def from_dict(cls, data: object) -> ShutdownPolicy:
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            require_power_outage=_as_bool(data.get("require_power_outage"), True),
            min_on_battery_seconds=max(0, _as_int(data.get("min_on_battery_seconds"), 120)),
            notify=_as_bool(data.get("notify"), True),
            external=ShutdownGroupPolicy.from_dict(
                data.get("external"), default_battery=15, default_runtime=300
            ),
            internal=ShutdownGroupPolicy.from_dict(
                data.get("internal"), default_battery=10, default_runtime=120
            ),
        )


@dataclass(frozen=True)
class LoadStepPolicy:
    """Single-poll output-load drop detection.

    A device abruptly losing power shows up as its UPS's output load falling by
    its whole draw between two polls — the only in-band signature NUT gives for
    a downstream device dying (the UPS itself stays happily ``OL``). A drop of
    ``drop_percent`` points or more between consecutive polls logs a
    ``load_step_drop`` event and (rate-limited by ``cooldown_seconds``) sends a
    notification. It is a hint, not a verdict: a heavy job finishing looks
    identical, so pair it with a reachability check on the device.
    """

    enabled: bool = True
    drop_percent: int = 15
    cooldown_seconds: int = 600

    @classmethod
    def from_dict(cls, data: object) -> LoadStepPolicy:
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            drop_percent=max(1, _as_int(data.get("drop_percent"), 15)),
            cooldown_seconds=max(0, _as_int(data.get("cooldown_seconds"), 600)),
        )


@dataclass(frozen=True)
class ShutdownTarget:
    """A machine to shut down when its UPS runs low on battery.

    ``kind``:
      * ``remote`` — over SSH (``host`` may be an ``ssh_config`` alias; omit
        ``user`` to use just the alias, e.g. ``ssh mt``).
      * ``serial`` — over a serial console (``device`` + ``baud``); the command
        is written to a passwordless/auto-login getty. Network-independent, so it
        still works during an outage when SSH can't reach the box.
      * ``local`` — the host this daemon runs on.

    A UPS may list any number; all disabled by default. ``battery_below`` and
    ``runtime_below`` are parsed for backward compatibility, but automatic
    shutdown decisions are controlled by the top-level ``shutdown`` policy.

    The orchestrator always sequences ``local`` targets **after** every enabled
    remote/serial target on the same UPS, so the watcher host dies last.
    """

    name: str
    kind: str = "remote"  # "remote" | "serial" | "local"
    enabled: bool = False
    host: str = ""
    user: str = ""
    device: str = ""  # serial: e.g. /dev/ttyUSB0
    baud: int = 115200  # serial baud rate
    cmd: str = "sudo /sbin/shutdown -h now"
    battery_below: int | None = None
    runtime_below: int | None = None

    @property
    def is_local(self) -> bool:
        return self.kind.lower() == "local"

    @property
    def is_serial(self) -> bool:
        return self.kind.lower() == "serial"

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ShutdownTarget:
        return cls(
            name=str(data.get("name", data.get("host", "target"))),
            kind=str(data.get("kind", "remote")).lower(),
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            device=str(data.get("device", "")),
            baud=_as_int(data.get("baud"), 115200),
            cmd=str(data.get("cmd", "sudo /sbin/shutdown -h now")),
            battery_below=_opt_int(data.get("battery_below")),
            runtime_below=_opt_int(data.get("runtime_below")),
        )


@dataclass(frozen=True)
class UpsConfig:
    """Per-UPS behaviour, keyed in config by its NUT device name."""

    name: str
    label: str
    shutdown_targets: tuple[ShutdownTarget, ...] = ()
    # Legacy pre-policy scope value, still parsed so old config files load.
    shutdown_scope: str = "remote"
    shutdown_policy: ShutdownPolicy = field(default_factory=ShutdownPolicy)

    @classmethod
    def from_dict(
        cls,
        name: str,
        data: dict[str, object],
        default_scope: str = "remote",
        shutdown_policy: ShutdownPolicy | None = None,
    ) -> UpsConfig:
        raw_targets = data.get("shutdown_targets", [])
        targets = (
            tuple(ShutdownTarget.from_dict(t) for t in raw_targets if isinstance(t, dict))
            if isinstance(raw_targets, list)
            else ()
        )
        return cls(
            name=name,
            label=str(data.get("label", name)),
            shutdown_targets=targets,
            shutdown_scope=_norm_scope(data.get("shutdown_scope"), default_scope),
            shutdown_policy=shutdown_policy or ShutdownPolicy(),
        )


@dataclass(frozen=True)
class Config:
    """Top-level configuration for all monitored UPSes."""

    webhook_url: str
    upses: dict[str, UpsConfig]
    poll_seconds: int = 30
    countdown_every_seconds: int = 60
    shutdown_scope: str = "remote"  # legacy default; retained for config compatibility
    shutdown_policy: ShutdownPolicy = field(default_factory=ShutdownPolicy)
    load_step: LoadStepPolicy = field(default_factory=LoadStepPolicy)
    discord_username: str = "UPS Orchestrator"
    discord_avatar_url: str = ""

    def ups(self, name: str) -> UpsConfig | None:
        """Look up a UPS by NUT name, returning ``None`` if it is not configured."""
        return self.upses.get(normalize_ups_name(name))

    @classmethod
    def load(cls, path: Path, env: Mapping[str, str] | None = None) -> Config:
        """Load and validate configuration from ``path``.

        Raises ``ValueError`` if no UPS sections are defined, since a monitor with
        nothing to monitor is almost certainly a mistake.
        """
        environ: Mapping[str, str] = os.environ if env is None else env
        raw = json.loads(path.read_text())

        webhook_env = str(raw.get("discord_webhook_env", "UPS_DISCORD_WEBHOOK"))
        webhook = (
            environ.get(webhook_env, "").strip() or str(raw.get("discord_webhook_url", "")).strip()
        )

        upses_raw = raw.get("upses", {})
        if not isinstance(upses_raw, dict) or not upses_raw:
            raise ValueError(f"No 'upses' configured in {path}")

        global_scope = _norm_scope(raw.get("shutdown_scope"), "remote")
        shutdown_policy = ShutdownPolicy.from_dict(raw.get("shutdown"))
        upses = {
            name: UpsConfig.from_dict(
                name,
                data,
                default_scope=global_scope,
                shutdown_policy=shutdown_policy,
            )
            for name, data in upses_raw.items()
            if isinstance(data, dict)
        }

        return cls(
            webhook_url=webhook,
            upses=upses,
            # poll_on_battery_seconds is the pre-0.3 name; honoured for back-compat.
            poll_seconds=_as_int(raw.get("poll_seconds", raw.get("poll_on_battery_seconds")), 30),
            countdown_every_seconds=_as_int(raw.get("countdown_every_seconds"), 60),
            shutdown_scope=global_scope,
            shutdown_policy=shutdown_policy,
            load_step=LoadStepPolicy.from_dict(raw.get("load_step")),
            discord_username=str(raw.get("discord_username", "UPS Orchestrator")),
            discord_avatar_url=str(raw.get("discord_avatar_url", "")),
        )
