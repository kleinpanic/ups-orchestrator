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
from dataclasses import dataclass
from pathlib import Path


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
class ShutdownTarget:
    """A machine to shut down when its UPS runs low on battery.

    ``kind``:
      * ``remote`` — over SSH (``host`` may be an ``ssh_config`` alias; omit
        ``user`` to use just the alias, e.g. ``ssh mt``).
      * ``serial`` — over a serial console (``device`` + ``baud``); the command
        is written to a passwordless/auto-login getty. Network-independent, so it
        still works during an outage when SSH can't reach the box.
      * ``local`` — the host this daemon runs on.

    A UPS may list any number; all disabled by default. Triggers fire when
    **either** threshold is crossed: battery charge at or below ``battery_below``
    (%), or estimated runtime at or below ``runtime_below`` (seconds). A target
    with neither set never auto-fires.

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
    # "remote" = only remote/serial targets fire (this host left to NUT's backstop);
    # "all" = local targets fire too, last. Resolved from the global default at load.
    shutdown_scope: str = "remote"

    @classmethod
    def from_dict(
        cls, name: str, data: dict[str, object], default_scope: str = "remote"
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
        )


@dataclass(frozen=True)
class Config:
    """Top-level configuration for all monitored UPSes."""

    webhook_url: str
    upses: dict[str, UpsConfig]
    poll_seconds: int = 30
    countdown_every_seconds: int = 60
    shutdown_scope: str = "remote"  # global default; per-UPS may override
    discord_username: str = "UPS Orchestrator"
    discord_avatar_url: str = ""

    def ups(self, name: str) -> UpsConfig | None:
        """Look up a UPS by NUT name, returning ``None`` if it is not configured."""
        return self.upses.get(name)

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
        upses = {
            name: UpsConfig.from_dict(name, data, default_scope=global_scope)
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
            discord_username=str(raw.get("discord_username", "UPS Orchestrator")),
            discord_avatar_url=str(raw.get("discord_avatar_url", "")),
        )
