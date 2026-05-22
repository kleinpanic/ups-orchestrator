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


@dataclass(frozen=True)
class ShutdownTarget:
    """A remote machine to gracefully shut down (over SSH) after a grace period
    on battery. Disabled by default. A UPS may have any number of these — e.g.
    every server it powers."""

    name: str
    enabled: bool = False
    host: str = ""
    user: str = ""
    cmd: str = "sudo /sbin/shutdown -h now"
    delay_seconds: int = 300

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ShutdownTarget:
        return cls(
            name=str(data.get("name", data.get("host", "target"))),
            enabled=bool(data.get("enabled", False)),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            cmd=str(data.get("cmd", "sudo /sbin/shutdown -h now")),
            delay_seconds=_as_int(data.get("delay_seconds"), 300),
        )


@dataclass(frozen=True)
class UpsConfig:
    """Per-UPS behaviour, keyed in config by its NUT device name."""

    name: str
    label: str
    shutdown_pi_on_lowbatt: bool = False
    min_runtime_seconds_shutdown_pi: int = 300
    shutdown_targets: tuple[ShutdownTarget, ...] = ()

    @classmethod
    def from_dict(cls, name: str, data: dict[str, object]) -> UpsConfig:
        raw_targets = data.get("shutdown_targets", [])
        targets = (
            tuple(ShutdownTarget.from_dict(t) for t in raw_targets if isinstance(t, dict))
            if isinstance(raw_targets, list)
            else ()
        )
        return cls(
            name=name,
            label=str(data.get("label", name)),
            shutdown_pi_on_lowbatt=bool(data.get("shutdown_pi_on_lowbatt", False)),
            min_runtime_seconds_shutdown_pi=_as_int(
                data.get("min_runtime_seconds_shutdown_pi"), 300
            ),
            shutdown_targets=targets,
        )


@dataclass(frozen=True)
class Config:
    """Top-level configuration for all monitored UPSes."""

    webhook_url: str
    poll_on_battery_seconds: int
    upses: dict[str, UpsConfig]
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

        upses = {
            name: UpsConfig.from_dict(name, data)
            for name, data in upses_raw.items()
            if isinstance(data, dict)
        }

        return cls(
            webhook_url=webhook,
            poll_on_battery_seconds=_as_int(raw.get("poll_on_battery_seconds"), 60),
            upses=upses,
            discord_username=str(raw.get("discord_username", "UPS Orchestrator")),
            discord_avatar_url=str(raw.get("discord_avatar_url", "")),
        )
