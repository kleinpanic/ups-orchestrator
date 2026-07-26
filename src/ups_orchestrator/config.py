"""Typed configuration loading.

Config lives in a committed ``config.json`` that holds **no secrets**. The Discord
webhook is resolved at runtime from an environment variable (default
``UPS_DISCORD_WEBHOOK``) so the URL never has to live in version control. A plain
``discord_webhook_url`` in the file is still honoured as a last resort for local
setups, but ``config.example.json`` ships it empty.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# The single per-machine shutdown method (Option A). A machine carries exactly
# one EFFECTIVE method: an active method (native/serial/ssh) is mutually exclusive
# with a legacy shutdown_target on the same UPS. Unknown/blank coerces to "none"
# (input-validation V5) so a typo can never spuriously activate a transport.
SHUTDOWN_METHODS = frozenset({"none", "native", "serial", "ssh"})


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
    its whole draw within a poll or two — the only in-band signature NUT gives
    for a downstream device dying (the UPS itself stays happily ``OL``). A drop
    of ``drop_percent`` points or more below the peak of the last
    ``window_polls`` polls logs a ``load_step_drop`` event and (rate-limited by
    ``cooldown_seconds``) sends a notification. The window — rather than a
    plain previous-poll comparison — keeps a collapse that straddles a poll
    boundary from splitting into two sub-threshold steps. It is a hint, not a
    verdict: a heavy job finishing looks identical, so pair it with a
    reachability check on the device.
    """

    enabled: bool = True
    drop_percent: int = 15
    cooldown_seconds: int = 600
    window_polls: int = 4

    @classmethod
    def from_dict(cls, data: object) -> LoadStepPolicy:
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), True),
            drop_percent=max(1, _as_int(data.get("drop_percent"), 15)),
            cooldown_seconds=max(0, _as_int(data.get("cooldown_seconds"), 600)),
            window_polls=max(1, _as_int(data.get("window_polls"), 4)),
        )


@dataclass(frozen=True)
class NutServer:
    """Primary-side ``upsd`` exposure settings — holds no secrets.

    ``listen`` defaults to localhost only; the LAN address is appended to the
    tuple at enrollment time by the CLI, so a fresh config never silently
    exposes ``upsd`` to the network. The secondary NUT password is sourced from
    the environment at use time, never stored here (SC6).
    """

    listen: tuple[str, ...] = ("127.0.0.1", "::1")
    port: int = 3493
    secondary_user: str = "upsmon_secondary"

    @classmethod
    def from_dict(cls, data: object) -> NutServer:
        if not isinstance(data, dict):
            return cls()
        raw_listen = data.get("listen")
        listen: tuple[str, ...] = cls.listen
        if isinstance(raw_listen, list):
            listen = tuple(e for e in raw_listen if isinstance(e, str))
        return cls(
            listen=listen,
            port=_as_int(data.get("port"), 3493),
            secondary_user=str(data.get("secondary_user", "upsmon_secondary")),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "listen": list(self.listen),
            "port": self.port,
            "secondary_user": self.secondary_user,
        }


@dataclass(frozen=True)
class BackupShutdown:
    """Reframed SSH/serial backup shutdown for a monitored machine — default off."""

    enabled: bool = False
    kind: str = "remote"  # "remote" | "serial"

    @classmethod
    def from_dict(cls, data: object) -> BackupShutdown:
        if not isinstance(data, dict):
            return cls()
        return cls(
            enabled=_as_bool(data.get("enabled"), False),
            kind=str(data.get("kind", "remote")),
        )

    def to_dict(self) -> dict[str, object]:
        return {"enabled": self.enabled, "kind": self.kind}


def derive_shutdown_method(
    raw: dict[str, object], ssh: str, ups: str, backup: BackupShutdown
) -> str:
    """Derive the effective shutdown method for a record that predates ``shutdown_method``.

    This is a BACK-COMPAT path only — a newly written record carries an explicit
    ``shutdown_method`` and never reaches here. The has-ups => ``native`` branch is
    evaluated FIRST so a Phase-1 native secondary (spark: ups set, backup
    ``{enabled:false, kind:remote}``) derives to ``native``, never ``ssh``. The
    ``backup`` projection fires ONLY when ``backup.enabled`` — an absent/disabled
    backup parses as ``enabled=False, kind="remote"`` and must not turn a native
    secondary into ssh.
    """
    if ups.strip():
        return "native"
    if backup.enabled:
        kind = backup.kind.strip().lower()
        if kind == "serial":
            return "serial"
        return "ssh"  # kind == "remote" (or unknown) maps to the ssh transport
    return "none"


@dataclass(frozen=True)
class MonitoredMachine:
    """A NUT secondary enrolled via ``monitor add``.

    Carries no password: the secondary's NUT credential is sourced from the
    environment at use time (SC6). ``to_dict`` emits only the known fields so
    plan 04 can append a fresh entry into the raw config dict without leaking
    any secret.
    """

    name: str
    ssh: str = ""  # ssh_config alias
    ups: str = ""  # NUT UPS name (e.g. "cyberpower")
    powervalue: int = 1  # 1 = powered by this UPS (counts to MINSUPPLIES)
    os: str = "auto"  # "auto" | "arch" | "ubuntu" | "debian"
    shutdown_cmd: str = "/sbin/shutdown -h now"
    ip: str = ""  # resolved source IP for the nft saddr set
    backup: BackupShutdown = field(default_factory=BackupShutdown)
    # The single effective shutdown method (Option A). Default "none"; a legacy
    # record with no explicit value derives one via ``derive_shutdown_method``.
    shutdown_method: str = "none"  # "none" | "native" | "serial" | "ssh"
    serial_device: str = ""  # method == "serial": e.g. /dev/serial/by-id/...
    serial_baud: int = 9600  # operator-declared; NEVER a silent 115200 (P2-08)
    # The original raw entry, so operator-authored keys (e.g. a per-machine
    # ``_comment``) survive an add/remove round-trip instead of being dropped by
    # a known-fields-only to_dict. Never carries a secret — the config holds none.
    raw: dict[str, object] = field(default_factory=dict, compare=False)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MonitoredMachine:
        ssh = str(data.get("ssh", ""))
        ups = str(data.get("ups", ""))
        name = str(data.get("name", ssh or "machine"))
        backup = BackupShutdown.from_dict(data.get("backup"))

        # Serial transport fields. Accept flat top-level keys (the dataclass shape)
        # and a nested ``serial: {device, baud}`` block (the authored form in
        # RESEARCH §1); flat keys win. serial_baud has NO silent 115200 default.
        serial_blk = data.get("serial")
        serial_blk = serial_blk if isinstance(serial_blk, dict) else {}
        serial_device = str(data.get("serial_device", serial_blk.get("device", "")))
        serial_baud = _as_int(
            data.get("serial_baud", serial_blk.get("baud")),
            9600,
        )

        # Effective method: an explicit valid shutdown_method always wins; else a
        # legacy record derives one. Unknown/blank explicit values coerce to "none"
        # (V5, logged once). Derivation is logged once so an operator sees any
        # mapping they did not write.
        if "shutdown_method" in data:
            raw_method = str(data.get("shutdown_method", "")).strip().lower()
            if raw_method in SHUTDOWN_METHODS:
                method = raw_method
            else:
                logger.warning(
                    "monitored machine %r has unknown shutdown_method %r; "
                    "coercing to 'none'",
                    name,
                    data.get("shutdown_method"),
                )
                method = "none"
        else:
            method = derive_shutdown_method(raw=data, ssh=ssh, ups=ups, backup=backup)
            if method != "none":
                logger.warning(
                    "monitored machine %r has no explicit shutdown_method; "
                    "derived %r from its legacy shape",
                    name,
                    method,
                )

        # A legacy backup {enabled:true, kind:serial} that derived to "serial" has
        # no serial_device/serial_baud to project — refuse it with a migration
        # error instead of silently producing an unfireable serial transport.
        if (
            "shutdown_method" not in data
            and method == "serial"
            and not serial_device
        ):
            raise ValueError(
                f"monitored machine {name!r} has a legacy backup "
                f"{{enabled:true, kind:serial}} but no serial device to project; "
                f"set an explicit shutdown_method:'serial' with serial_device + "
                f"serial_baud to migrate."
            )

        return cls(
            name=name,
            ssh=ssh,
            ups=ups,
            powervalue=_as_int(data.get("powervalue"), 1),
            os=str(data.get("os", "auto")),
            shutdown_cmd=str(data.get("shutdown_cmd", "/sbin/shutdown -h now")),
            ip=str(data.get("ip", "")),
            backup=backup,
            shutdown_method=method,
            serial_device=serial_device,
            serial_baud=serial_baud,
            raw=dict(data),
        )

    def to_dict(self) -> dict[str, object]:
        # Merge known fields into the preserved raw entry so unknown operator keys
        # (a _comment, a custom tag) are not lost on the next persist.
        merged: dict[str, object] = dict(self.raw)
        merged.update(
            {
                "name": self.name,
                "ssh": self.ssh,
                "ups": self.ups,
                "powervalue": self.powervalue,
                "os": self.os,
                "shutdown_cmd": self.shutdown_cmd,
                "ip": self.ip,
                "backup": self.backup.to_dict(),
                "shutdown_method": self.shutdown_method,
                "serial_device": self.serial_device,
                "serial_baud": self.serial_baud,
            }
        )
        return merged


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
    baud: int = 9600  # serial baud rate (matches the live line; never 115200 — P2-08)
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
            baud=_as_int(data.get("baud"), 9600),
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
    # Per-UPS load-step override; None falls back to the global load_step policy.
    # Useful to raise drop_percent on a UPS with bursty load so routine job churn
    # doesn't page while other UPSes keep the sensitive default.
    load_step: LoadStepPolicy | None = None

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
            load_step=LoadStepPolicy.from_dict(data["load_step"]) if "load_step" in data else None,
        )


def validate_active_transports(
    monitored_machines: tuple[MonitoredMachine, ...],
) -> tuple[str, ...]:
    """Return per-machine errors for effective methods with invalid transport params.

    ``_as_int`` silently substitutes defaults (config.py:27), so a bad baud never
    surfaces as a coercion failure — validation must be explicit. A serial method
    with an empty ``serial_device`` or a non-positive ``serial_baud``, or an ssh
    method with an empty ssh alias, is a load-time error (finding 3): otherwise
    ``_default_serial_shutdown`` would open a bogus device or ssh would target no host.
    """
    errors: list[str] = []
    for m in monitored_machines:
        method = m.shutdown_method.strip().lower()
        if method == "serial":
            if not m.serial_device.strip():
                errors.append(f"{m.name}: serial method with empty serial_device")
            if m.serial_baud <= 0:
                errors.append(f"{m.name}: serial method with non-positive serial_baud")
        elif method == "ssh":
            if not m.ssh.strip():
                errors.append(f"{m.name}: ssh method with empty ssh alias")
    return tuple(errors)


def dual_regime_conflicts(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[str, ...]:
    """Return names of machines governed by BOTH shutdown regimes.

    A conflict is a monitored machine that also appears as an *enabled*
    ``shutdown_target`` on the UPS it references. Since every ``monitored_machines``
    entry now carries an EFFECTIVE ``shutdown_method`` (explicit or derived), this
    is re-keyed on that method rather than on "is a native secondary": every
    value in the allow-set conflicts with a legacy target, so the check is a plain
    overlap test. ``native`` + legacy still double-shuts (the secondary fires below
    LB while the target uses the external-group thresholds); ``serial``/``ssh`` +
    legacy fires the same box twice over two transports; and ``none`` + legacy
    fires a machine the operator declared off. All four are hard errors at
    ``Config.load``. ``monitor add`` uses the same detector and refuses without
    ``--force``. Matching is case-insensitive on the machine name.
    """
    conflicts: list[str] = []
    for m in monitored_machines:
        ups = upses.get(normalize_ups_name(m.ups))
        if ups is None:
            continue
        name_lower = m.name.strip().lower()
        for t in ups.shutdown_targets:
            if t.enabled and t.name.strip().lower() == name_lower:
                conflicts.append(m.name)
                break
    return tuple(conflicts)


def legacy_only_targets(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[str, ...]:
    """Return ``ups/target`` labels for enabled targets with no monitored machine.

    These are the pure-legacy remnant: there is no ``monitored_machines`` entry to
    key an effective shutdown method on, so nothing can conflict and the entry must
    keep loading (P2-07). It is warn-only, not an error. ``local`` targets are the
    watcher host itself — they have no monitored_machines entry by construction and
    are not a migration candidate, so they are excluded.
    """
    known = {m.name.strip().lower() for m in monitored_machines}
    remnant: list[str] = []
    for ups_name, ups in upses.items():
        for t in ups.shutdown_targets:
            if t.enabled and not t.is_local and t.name.strip().lower() not in known:
                remnant.append(f"{ups_name}/{t.name}")
    return tuple(remnant)


@dataclass(frozen=True)
class Config:
    """Top-level configuration for all monitored UPSes."""

    webhook_url: str
    upses: dict[str, UpsConfig]
    poll_seconds: int = 30
    countdown_every_seconds: int = 60
    onbatt_notify_grace_seconds: int = 20
    shutdown_scope: str = "remote"  # legacy default; retained for config compatibility
    shutdown_policy: ShutdownPolicy = field(default_factory=ShutdownPolicy)
    load_step: LoadStepPolicy = field(default_factory=LoadStepPolicy)
    discord_username: str = "UPS Orchestrator"
    discord_avatar_url: str = ""
    nut_server: NutServer = field(default_factory=NutServer)
    monitored_machines: tuple[MonitoredMachine, ...] = ()

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

        machines_raw = raw.get("monitored_machines", [])
        monitored_machines = (
            tuple(MonitoredMachine.from_dict(m) for m in machines_raw if isinstance(m, dict))
            if isinstance(machines_raw, list)
            else ()
        )

        # STRICT per-machine mutual exclusion (P2-06): a machine carries exactly one
        # effective method, so any overlap with an enabled legacy shutdown_target on
        # the same UPS is a hard error — it would shut the box down twice (or shut
        # down a machine declared "none"). Fail closed at load, not at outage time.
        conflicts = dual_regime_conflicts(monitored_machines, upses)
        if conflicts:
            methods = {m.name: m.shutdown_method for m in monitored_machines}
            raise ValueError(
                "Machine(s) governed by BOTH shutdown regimes: "
                + "; ".join(f"{n} (shutdown_method={methods[n]})" for n in conflicts)
                + ". Each machine takes exactly one method — remove the enabled "
                "shutdown_target on its UPS, or set the machine's shutdown_method."
            )

        remnant = legacy_only_targets(monitored_machines, upses)
        if remnant:
            logger.warning(
                "Legacy shutdown_target(s) %s have no monitored_machines entry, so no "
                "per-machine shutdown_method governs them; they still fire under the "
                "legacy regime. Migrate them to a monitored_machines entry with an "
                "explicit shutdown_method.",
                ", ".join(remnant),
            )

        transport_errors = validate_active_transports(monitored_machines)
        if transport_errors:
            raise ValueError(
                "Invalid active shutdown transport(s): " + "; ".join(transport_errors)
            )

        return cls(
            webhook_url=webhook,
            upses=upses,
            # poll_on_battery_seconds is the pre-0.3 name; honoured for back-compat.
            poll_seconds=_as_int(raw.get("poll_seconds", raw.get("poll_on_battery_seconds")), 30),
            countdown_every_seconds=_as_int(raw.get("countdown_every_seconds"), 60),
            onbatt_notify_grace_seconds=max(0, _as_int(raw.get("onbatt_notify_grace_seconds"), 20)),
            shutdown_scope=global_scope,
            shutdown_policy=shutdown_policy,
            load_step=LoadStepPolicy.from_dict(raw.get("load_step")),
            discord_username=str(raw.get("discord_username", "UPS Orchestrator")),
            discord_avatar_url=str(raw.get("discord_avatar_url", "")),
            nut_server=NutServer.from_dict(raw.get("nut_server")),
            monitored_machines=monitored_machines,
        )
