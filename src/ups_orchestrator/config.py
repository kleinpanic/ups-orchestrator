"""Typed configuration loading.

Config lives in a committed ``config.json`` that holds **no secrets**. The Discord
webhook is resolved at runtime from an environment variable (default
``UPS_DISCORD_WEBHOOK``) so the URL never has to live in version control. A plain
``discord_webhook_url`` in the file is still honoured as a last resort for local
setups, but ``config.example.json`` ships it empty.
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
import re
import shlex
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

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


def _strict_baud(value: object) -> int | None:
    """Parse a DECLARED baud strictly, or return ``None``.

    ``_as_int`` substitutes a default for anything it cannot read, so ``"fast"``
    became 9600 and then sailed past a ``baud <= 0`` check as a perfectly plausible
    rate (HI-04 on ``MonitoredMachine.serial_baud``, LO-12 on ``ShutdownTarget.baud``
    — one class on two lines, so one parser serves both). Only an int or an
    integer-valued string parses; a JSON boolean, a float and a non-integer string
    all yield ``None`` so the caller can quote what the operator actually wrote.
    Surrounding whitespace is tolerated — whitespace was never the defect.
    """
    if isinstance(value, bool):  # bool is an int subclass; a boolean is not a baud
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


# Wrappers that hand the caller root without the command itself needing to be root.
_ESCALATORS = frozenset({"sudo", "doas", "su", "pkexec", "run0"})
# Binaries that halt the box and refuse to run as anyone but root.
_ROOT_HALT_BINARIES = frozenset({"shutdown", "poweroff", "halt", "reboot", "telinit", "init"})
# `systemctl <verb>` forms that reach the same place.
_SYSTEMCTL_HALT_VERBS = frozenset({"poweroff", "halt", "reboot"})


def requires_root_escalation(cmd: str) -> bool:
    """Does ``cmd`` invoke a root-only halt binary with no escalation wrapper?

    NEW-2: ``shutdown_cmd`` is overloaded across two privilege contexts. The default
    ``/sbin/shutdown -h now`` is CORRECT for ``native`` — ``upsmon`` runs SHUTDOWNCMD
    as root — and WRONG for a push, where it runs as the ssh user or the far-end
    auto-login user. Over ssh a permission failure is a non-zero rc; over serial it is
    SILENT (the bytes are delivered, the write completes, rc is 0, and success is
    reported for a box that stayed up).

    The test is on the VALUE, never on key presence: ``to_dict`` writes
    ``shutdown_cmd`` unconditionally and the CLI defaults it method-independently, so
    every persisted record carries the key at exactly the wrong-for-push value and a
    key-presence detector would warn about none of them. Basenames rather than full
    paths, because ``/sbin/shutdown``, ``/usr/sbin/shutdown`` and a bare ``shutdown``
    on PATH are one defect wearing three spellings; case-folded because the field is
    operator-authored free text. Anything unrecognised is False, so a genuinely custom
    command (a setuid wrapper, an operator script) is never nagged about. This never
    raises: a predicate that threw out of the load path would turn an advisory into an
    outage.
    """
    try:
        tokens = shlex.split(cmd)
    except ValueError:  # unbalanced quote
        tokens = cmd.split()
    if not tokens:
        return False
    head = PurePosixPath(tokens[0]).name.casefold()
    if head in _ESCALATORS:
        return False  # the operator already handled the privilege gap
    if head in _ROOT_HALT_BINARIES:
        return True
    if head == "systemctl":
        return any(PurePosixPath(t).name.casefold() in _SYSTEMCTL_HALT_VERBS for t in tokens[1:])
    return False


@dataclass(frozen=True)
class ConfigNotice:
    """One load-time degrade report.

    ``severity`` is the whole seam between "an authority changed state" and "look at
    this". It is carried in the VALUE rather than implied by a notice existing at all,
    so an advisory is structurally incapable of disarming a machine: ``disarmed``
    folds on ``severity == "error"`` and ignores everything else.

    ``subject`` is a machine name or an ``<ups>/<target>`` label; ``message`` states
    what is wrong AND the concrete remedy. No secret has ever entered this module and
    none may now — messages interpolate only machine names, UPS names, method strings,
    device paths, kinds and baud values.
    """

    severity: str  # "error" | "advisory"
    subject: str
    message: str

    def __str__(self) -> str:
        return f"[{self.severity}] {self.subject}: {self.message}"


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


def derive_shutdown_method(ssh: str, ups: str, backup: BackupShutdown) -> str:
    """Derive the effective shutdown method for a record that predates ``shutdown_method``.

    This is a BACK-COMPAT path only — a newly written record carries an explicit
    ``shutdown_method`` and never reaches here. The has-ups => ``native`` branch is
    evaluated FIRST so a Phase-1 native secondary (spark: ups set, backup
    ``{enabled:false, kind:remote}``) derives to ``native``, never ``ssh``. The
    ``backup`` projection fires ONLY when ``backup.enabled`` — an absent/disabled
    backup parses as ``enabled=False, kind="remote"`` and must not turn a native
    secondary into ssh.

    The branch ORDER is load-bearing and is pinned by
    ``test_derive_native_ordering_beats_enabled_backup``: with a MATCHING non-empty
    ``ups`` a mis-derived push method is projected and does fire, pushing onto a box
    whose own upsmon is already self-halting on FSD. Note the corollary that makes the
    no-``ups`` half inert: a push method is derived ONLY when ``ups`` is empty, and
    ``events._machine_targets`` projects only a machine whose UPS matches the UPS being
    handled — so the entire derived-push class is structurally unfireable, which is why
    ``unprojectable_push_machines`` disarms it rather than warning about it.

    LO-11: this consults only its arguments. It used to take a ``raw`` parameter it
    never referenced, which read as though the record were consulted.
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
    # Operator-declared; NEVER a silent 115200 and never a silent 9600 either (P2-08).
    # An absent declaration is None and disarms a serial machine at load rather than
    # guessing at the live line's rate.
    serial_baud: int | None = None
    # The original raw entry, so operator-authored keys (e.g. a per-machine
    # ``_comment``) survive an add/remove round-trip instead of being dropped by
    # a known-fields-only to_dict. Never carries a secret — the config holds none.
    raw: dict[str, object] = field(default_factory=dict, compare=False)
    # INV-DEGRADE: the ONLY state a load-time degrade may write. Non-persisted and
    # compare=False — a diagnostic overlay, never part of the declaration.
    load_notices: tuple[ConfigNotice, ...] = field(default=(), compare=False)

    @property
    def disarmed(self) -> bool:
        """Has a load-time ERROR taken this machine's push authority away?

        Not a stored field, so two guarantees are structural rather than remembered:

        1. A declared-``native`` machine is NEVER disarmed. Its authority is the REMOTE
           box's own ``upsmon`` reacting to this primary's FSD, configured in that
           box's ``/etc``; nothing a config parser does changes it, and reporting it
           disarmed would tell an operator a still-protected box is unprotected. The
           only real disarm is ``monitor remove <name>``.
        2. An ADVISORY can never disarm anything, because the fold ignores every
           severity but ``"error"``. There is no code path by which attaching one
           changes ``effective_method``.

        It reads the DECLARED ``shutdown_method`` (INV-DECLARED), which is what makes
        the native carve-out unforgeable: nothing in this module rewrites that field.
        """
        return (
            any(n.severity == "error" for n in self.load_notices)
            and self.shutdown_method != "native"
        )

    @property
    def effective_method(self) -> str:
        """The method that FIRES. Detection, gates and persistence read the declaration."""
        return "none" if self.disarmed else self.shutdown_method

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
        serial_baud = _strict_baud(data.get("serial_baud", serial_blk.get("baud")))

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
            method = derive_shutdown_method(ssh=ssh, ups=ups, backup=backup)
            if method != "none":
                logger.warning(
                    "monitored machine %r has no explicit shutdown_method; "
                    "derived %r from its legacy shape",
                    name,
                    method,
                )

        # RA-01: a legacy backup {enabled:true, kind:serial} with no serial_device used
        # to raise here. It no longer does. MonitoredMachine.backup has ZERO runtime
        # consumers, so hard-failing the whole daemon over an inert field is the exact
        # MED-08 outage — and on the outage path specifically, _cmd_event returns 0 when
        # _load_config returns None, so a config that trips a load-time raise turns every
        # real NUT power event into a silent no-op that reports success (IW-06). The
        # shape now parses, and Config.load disarms it with the migration remedy named.
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
        # LO-14: never emit a stale nested ``serial`` block alongside the flat fields.
        # Two contradictory sources of truth means an operator editing the nested form
        # is silently ignored. The nested form stays ACCEPTED on input.
        merged.pop("serial", None)
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
            }
        )
        # INV-DEGRADE at the persistence boundary, as a MECHANISM rather than a special
        # case: a value that could not be parsed is never written back. The key is
        # simply not emitted, so the merge of ``raw`` leaves the operator's own "fast"
        # in place verbatim and the notice can quote what they actually wrote. Without
        # this, ``_monitor_persist`` would write the parser's sentinel over the
        # declaration and the next load would quote a value the operator never typed.
        if self.serial_baud is not None:
            merged["serial_baud"] = self.serial_baud
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
    # Serial baud rate (matches the live line; never 115200 — P2-08). The 9600
    # DATACLASS default is retained for P2-07 back-compat: an ABSENT key still yields
    # 9600. Only a DECLARED-but-unparseable value yields None (LO-12).
    baud: int | None = 9600
    cmd: str = "sudo /sbin/shutdown -h now"
    battery_below: int | None = None
    runtime_below: int | None = None
    # INV-DEGRADE mirror of MonitoredMachine.load_notices. Non-persisted, compare=False.
    load_notices: tuple[ConfigNotice, ...] = field(default=(), compare=False)

    @property
    def is_local(self) -> bool:
        return self.kind.lower() == "local"

    @property
    def is_serial(self) -> bool:
        return self.kind.lower() == "serial"

    @property
    def effective_enabled(self) -> bool:
        """Whether this target FIRES. ``enabled`` stays the declaration (INV-DECLARED).

        Folds on severity identically to ``MonitoredMachine.disarmed``: an advisory
        leaves the target armed. Unlike ``native``, a legacy target's authority is
        in-process, so an ERROR here genuinely disarms it.
        """
        return self.enabled and not any(n.severity == "error" for n in self.load_notices)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> ShutdownTarget:
        return cls(
            name=str(data.get("name", data.get("host", "target"))),
            kind=str(data.get("kind", "remote")).lower(),
            # MED-07: bare bool("false") is True, which made the disabled-target
            # exemption conditional on the operator having written a real JSON boolean.
            enabled=_as_bool(data.get("enabled"), False),
            host=str(data.get("host", "")),
            user=str(data.get("user", "")),
            device=str(data.get("device", "")),
            baud=_strict_baud(data["baud"]) if "baud" in data else 9600,
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


# INV-DEGRADE has exactly FOUR write points, and they are these. Every notice site goes
# through one of them, so the invariant has four places to audit rather than a rule to
# remember at every site. They are named for their SEVERITY because the round-3 failure
# mode was not that an executor would choose badly — it was that only one door existed.
# All four APPEND via dataclasses.replace and never overwrite: a push machine can
# legitimately earn both the NEW-2 advisory and a T-02-10 ssh-alias error in one load.


def _disarm_machine(machine: MonitoredMachine, message: str) -> MonitoredMachine:
    """Append an ERROR notice: this machine stops being pushed — unless it is native."""
    return dataclasses.replace(
        machine,
        load_notices=(
            *machine.load_notices,
            ConfigNotice(severity="error", subject=machine.name, message=message),
        ),
    )


def _advise_machine(machine: MonitoredMachine, message: str) -> MonitoredMachine:
    """Append an ADVISORY notice: nothing about this machine's armed state changes."""
    return dataclasses.replace(
        machine,
        load_notices=(
            *machine.load_notices,
            ConfigNotice(severity="advisory", subject=machine.name, message=message),
        ),
    )


def _disarm_target(ups_key: str, target: ShutdownTarget, message: str) -> ShutdownTarget:
    """Append an ERROR notice: this legacy target stops firing."""
    return dataclasses.replace(
        target,
        load_notices=(
            *target.load_notices,
            ConfigNotice(severity="error", subject=f"{ups_key}/{target.name}", message=message),
        ),
    )


def _advise_target(ups_key: str, target: ShutdownTarget, message: str) -> ShutdownTarget:
    """Append an ADVISORY notice: this legacy target still fires."""
    return dataclasses.replace(
        target,
        load_notices=(
            *target.load_notices,
            ConfigNotice(severity="advisory", subject=f"{ups_key}/{target.name}", message=message),
        ),
    )


# T-02-10. A plain host or ssh_config alias: no leading "-" (ssh would read it as an
# option), no whitespace, no shell metacharacters. `user@host` and `host:port` shapes are
# allowed because they are legitimate operator spellings.
_SSH_ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@:-]*$")


def _transport_notices(m: MonitoredMachine) -> tuple[tuple[str, str], ...]:
    """``(severity, message)`` pairs for one machine's DECLARED transport parameters."""
    found: list[tuple[str, str]] = []
    method = m.shutdown_method.strip().lower()
    if method == "serial":
        device = m.serial_device.strip()
        if not device:
            if "shutdown_method" in m.raw:
                found.append(
                    (
                        "error",
                        "declares shutdown_method='serial' with an empty serial_device, so "
                        "there is nothing to write the shutdown command to. Set "
                        "serial_device to the console device under /dev/.",
                    )
                )
            else:
                # Distinguished by the ABSENCE of an explicit key: this is the legacy
                # backup {enabled:true, kind:serial} shape, which needs a migration, not
                # a device typo fix.
                found.append(
                    (
                        "error",
                        "has a legacy backup {enabled:true, kind:serial} that derived "
                        "shutdown_method='serial', but no serial device to project. "
                        "Migrate it: set an explicit shutdown_method 'serial' with "
                        "serial_device and serial_baud.",
                    )
                )
        elif not device.startswith("/dev/") or device == "/dev/":
            # MED-10 (config half). The serial writer opens the device with mode "wb",
            # which TRUNCATES a regular file — so a typo'd path destroys that file and
            # still reports success. The transport-side guard is 02-07.
            found.append(
                (
                    "error",
                    f"declares serial_device {device!r}, which is not an absolute path "
                    f"under /dev/. The serial writer opens it with mode 'wb', which "
                    f"TRUNCATES a regular file, so a typo would destroy that file and "
                    f"still report success. Use the console device path under /dev/.",
                )
            )
        if m.serial_baud is None:
            declared = m.raw.get("serial_baud", m.raw.get("serial", {}))
            if isinstance(declared, dict):
                declared = declared.get("baud")
            if declared is None:
                found.append(
                    (
                        "error",
                        "declares shutdown_method='serial' with no serial_baud. The baud "
                        "is never assumed: a mismatch writes garbage down the line and "
                        "still returns rc 0, a silent no-shutdown. Declare serial_baud "
                        "(the live console here is 9600).",
                    )
                )
            else:
                # HI-04: quote the operator's ORIGINAL value, never a sentinel the parser
                # invented — which is only possible because to_dict never wrote one back.
                found.append(
                    (
                        "error",
                        f"declares serial_baud {declared!r}, which is not an integer. "
                        f"Declare an integer serial_baud (the live console here is 9600).",
                    )
                )
        elif m.serial_baud <= 0:
            found.append(
                (
                    "error",
                    f"declares serial_baud {m.serial_baud}; POSIX B0 means hang up the "
                    f"line, so stty would disconnect the console it was about to use. "
                    f"Declare the live console's baud (9600 here).",
                )
            )
    elif method == "ssh":
        alias = m.ssh.strip()
        if not alias:
            found.append(
                (
                    "error",
                    "declares shutdown_method='ssh' with an empty ssh alias, so there is "
                    "no host to connect to. Set 'ssh' to the hostname or ssh_config "
                    "alias of the box to shut down.",
                )
            )
        elif not _SSH_ALIAS_RE.match(alias):
            # T-02-10. This value becomes an argv element in an UNATTENDED ssh at outage
            # time (before 02-02 it only ever reached a foreground operator command), and
            # an option-shaped value is interpreted by ssh as an option — a probe produced
            # an argv carrying an injected ProxyCommand while validation returned clean.
            found.append(
                (
                    "error",
                    f"declares ssh alias {m.ssh!r}, which is not a plain host or "
                    f"ssh_config alias. That value becomes an argv element in an "
                    f"unattended ssh at outage time, where a leading '-' is read as an "
                    f"option and shell metacharacters are carried verbatim. Use a plain "
                    f"hostname or ssh_config alias.",
                )
            )
    elif method == "native" and not m.ups.strip():
        # HI-05 corrected: REPORTED, never disarmed. Config cannot disarm a native
        # authority — see MonitoredMachine.disarmed. Routing it through the advisory
        # helper makes the disposition explicit rather than relying on the native
        # carve-out to neutralise a disarm.
        found.append(
            (
                "advisory",
                f"declares shutdown_method='native' with a blank ups, so nothing here "
                f"associates it with a UPS. Config cannot disarm a native authority: the "
                f"remote box's own upsmon halts it on this primary's FSD. Run "
                f"'monitor verify {m.name}' to learn whether that secondary is actually "
                f"armed, and 'monitor remove {m.name}' to disarm it.",
            )
        )
    return tuple(found)


def validate_active_transports(
    monitored_machines: tuple[MonitoredMachine, ...],
) -> tuple[tuple[str, str, str], ...]:
    """Return ``(machine_name, severity, message)`` for invalid transport parameters.

    A pure reporter that raises nothing (RA-01): a machine's transport being
    unconfigurable is not a reason to stop watching the UPS. It reads the DECLARED
    method (INV-DECLARED), so the same findings recur on a reload of an already-degraded
    config and the degrade is idempotent.
    """
    return tuple(
        (m.name, severity, message)
        for m in monitored_machines
        for severity, message in _transport_notices(m)
    )


# Every kind ``events._fire_target`` dispatches deliberately. Anything else is not
# rejected there — it falls through to the SSH runner (events.py:470).
_LEGACY_TARGET_KINDS = frozenset({"remote", "serial", "local"})


def validate_legacy_targets(
    upses: Mapping[str, UpsConfig],
) -> tuple[tuple[str, str, str], ...]:
    """Return ``(ups_key, target_name, message)`` for enabled targets that cannot fire.

    A pure reporter — it raises nothing and reads the DECLARED ``enabled`` flag
    (INV-DECLARED), so it keeps reporting the same shapes on a reload of an
    already-degraded config. Four shapes, each unfireable or misdispatching:

    * a serial target whose baud is non-positive or unparseable. POSIX ``B0`` means
      *hang up the line*, so ``stty -F <device> 0`` actively disconnects the console it
      was about to write to;
    * a serial target with a blank device;
    * a ``remote`` target with a blank host — ``from_dict`` accepts it and ``ssh`` can
      never connect to it;
    * a ``remote`` target whose ``host`` or ``user`` is not a plain host/ssh_config
      alias (BL-01);
    * a ``kind`` that is none of remote/serial/local, because dispatch treats anything
      not local and not serial as SSH, so an unknown kind becomes a silent ssh attempt
      against whatever ``host`` happens to hold.
    """
    problems: list[tuple[str, str, str]] = []
    for ups_key, ups in upses.items():
        for t in ups.shutdown_targets:
            if not t.enabled:
                continue
            kind = t.kind.strip().lower()
            if kind not in _LEGACY_TARGET_KINDS:
                problems.append(
                    (
                        ups_key,
                        t.name,
                        f"unknown target kind {t.kind!r}; dispatch treats anything that is "
                        f"not 'local' or 'serial' as SSH, so this would become a silent ssh "
                        f"attempt. Set kind to one of remote/serial/local.",
                    )
                )
                continue
            if kind == "serial":
                if not t.device.strip():
                    problems.append(
                        (
                            ups_key,
                            t.name,
                            "serial target with a blank device; set device to the "
                            "console device under /dev/.",
                        )
                    )
                if t.baud is None:
                    problems.append(
                        (
                            ups_key,
                            t.name,
                            "serial target with an unparseable baud; declare an integer "
                            "baud matching the live console (9600 here).",
                        )
                    )
                elif t.baud <= 0:
                    problems.append(
                        (
                            ups_key,
                            t.name,
                            f"serial target with a non-positive baud {t.baud}; POSIX B0 "
                            f"hangs up the line, so stty would disconnect the console it "
                            f"was about to use. Declare the live console's baud (9600 here).",
                        )
                    )
            elif kind == "remote":
                if not t.host.strip():
                    problems.append(
                        (
                            ups_key,
                            t.name,
                            "remote target with a blank host; ssh can never connect. Set host "
                            "to the hostname or ssh_config alias of the box to shut down.",
                        )
                    )
                # BL-01. The SAME sink T-02-10 hardened for MonitoredMachine.ssh: both
                # fields land in ``events.ssh_dest`` -> ``["ssh", ..., dest, cmd]``, so a
                # leading '-' in EITHER is read by ssh as an option and an injected
                # ProxyCommand runs unattended at outage time. Only the machine half was
                # checked; the legacy half reaches the identical argv.
                for field_name, value in (("host", t.host), ("user", t.user)):
                    v = value.strip()
                    if v and not _SSH_ALIAS_RE.match(v):
                        problems.append(
                            (
                                ups_key,
                                t.name,
                                f"remote target declares {field_name} {value!r}, which is not "
                                f"a plain host or ssh_config alias. It becomes an argv element "
                                f"in an unattended ssh at outage time, where a leading '-' is "
                                f"read as an option and shell metacharacters are carried "
                                f"verbatim. Use a plain hostname or ssh_config alias.",
                            )
                        )
    return tuple(problems)


def canonical_ups_key(name: str) -> str:
    """The ONE canonicalisation of a UPS name: strip ``@host``, strip, casefold.

    HI-03/IB-02. Used by the dual-regime detector, by the push-association check and —
    in 02-07 — by ``events._machine_targets``, so a capitalisation cannot mean two
    different things in two modules. Correction of record: ``_machine_targets`` does
    NOT case-fold today; it compares ``normalize_ups_name`` outputs directly, which is
    why a case-mismatched ``ups`` silently disarms the push path while the record loads
    clean and reports an active method.
    """
    return normalize_ups_name(name).strip().casefold()


def canonical_ups_index(
    upses: Mapping[str, UpsConfig],
) -> tuple[dict[str, UpsConfig], tuple[tuple[str, str], ...]]:
    """Build ``{canonical_key: UpsConfig}`` and report authored keys that collide.

    A collision means two authored ``upses`` keys canonicalise to one, which makes
    projection and lookup ambiguous. That is monitoring-topology corruption rather than
    a shutdown-authority error, so ``Config.load`` RAISES on it; this function only
    reports, and raises nothing.
    """
    index: dict[str, UpsConfig] = {}
    collisions: list[tuple[str, str]] = []
    for authored, ups in upses.items():
        key = canonical_ups_key(authored)
        existing = index.get(key)
        if existing is not None:
            collisions.append((existing.name, authored))
            continue
        index[key] = ups
    return index, tuple(collisions)


def dual_regime_pairs(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[tuple[str, str, str], ...]:
    """Return ``(machine_name, ups_key, target_name)`` for every dual-regime conflict.

    A conflict is a monitored machine that also appears as an *enabled*
    ``shutdown_target``. Every ``monitored_machines`` entry carries a
    ``shutdown_method``, and every value in the allow-set conflicts with a legacy
    target, so this is a plain overlap test: ``native`` + legacy double-shuts (the
    secondary fires below LB while the target uses the external-group thresholds);
    ``serial``/``ssh`` + legacy fires the same box twice over two transports; ``none`` +
    legacy fires a machine the operator declared off.

    It returns the TRIPLE so a caller can disarm the exact target rather than only learn
    a machine name, and it reads DECLARED ``shutdown_method`` and DECLARED
    ``ShutdownTarget.enabled`` (INV-DECLARED) — which is what keeps ``cli.py``'s
    ``--force`` gate firing against an already-degraded config, and what makes the
    degrade idempotent across reloads.

    BL-01: a machine whose ``ups`` is blank, or non-empty but resolving to no configured
    UPS, is scanned against EVERY configured UPS's enabled targets rather than skipped.
    Fail-closed — a typo must widen the scan, never silence it. Matching is
    case-insensitive on the machine name and canonicalised on the UPS name (HI-03).
    It raises nothing.
    """
    index, _collisions = canonical_ups_index(upses)
    pairs: list[tuple[str, str, str]] = []
    for m in monitored_machines:
        key = canonical_ups_key(m.ups)
        match = index.get(key) if key else None
        scope: tuple[UpsConfig, ...] = (match,) if match is not None else tuple(upses.values())
        name_key = m.name.strip().casefold()
        for ups in scope:
            for t in ups.shutdown_targets:
                if t.enabled and t.name.strip().casefold() == name_key:
                    pairs.append((m.name, ups.name, t.name))
                    break
    return tuple(pairs)


def unknown_ups_references(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[tuple[str, str], ...]:
    """Return ``(machine_name, ups_name)`` for a non-empty ``ups`` that resolves to nothing.

    A blank ``ups`` is a different defect (see ``unprojectable_push_machines``) and is
    not reported here. Raises nothing.
    """
    index, _collisions = canonical_ups_index(upses)
    return tuple(
        (m.name, m.ups)
        for m in monitored_machines
        if m.ups.strip() and canonical_ups_key(m.ups) not in index
    )


def unprojectable_push_machines(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[str, ...]:
    """Return machines declaring an ACTIVE PUSH method that can never be projected.

    The push-association rule, and the correction to BL-01's premise.
    ``events._machine_targets`` projects a machine only when its UPS matches the UPS
    being handled, so a ``serial``/``ssh`` machine whose ``ups`` is blank or
    unresolvable is projected on no event at all: it reports as protected and can never
    fire. Measured, not reasoned — the shipped projector was probed.

    The interaction that makes this load-bearing rather than academic:
    ``derive_shutdown_method`` yields a push method ONLY when ``ups`` is blank, so the
    ENTIRE legacy-derived push class is structurally unfireable while the loader logs
    ``derived 'ssh'`` and ``monitor list`` shows it. Disarming it is not a regression —
    ``MonitoredMachine.backup`` has zero runtime consumers, so the push it implied has
    always been imaginary. Raises nothing.
    """
    index, _collisions = canonical_ups_index(upses)
    return tuple(
        m.name
        for m in monitored_machines
        if m.shutdown_method.strip().lower() in ("serial", "ssh")
        and canonical_ups_key(m.ups) not in index
    )


def dual_regime_conflicts(
    monitored_machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[str, ...]:
    """Return names of machines governed by BOTH shutdown regimes.

    A de-duplicated projection of ``dual_regime_pairs``' machine-name element, in
    first-seen order, so ``monitor add``'s ``--force`` gate and its error text stay
    behaviourally identical. See ``dual_regime_pairs`` for what counts as a conflict.

    Matching is case-insensitive on the machine name AND canonicalised on the UPS name
    (the earlier docstring advertised the former and was silent about the latter, which
    is exactly the gap HI-03 exploited). A machine whose ``ups`` is blank or
    unresolvable is scanned against every UPS rather than skipped (BL-01).
    """
    seen: dict[str, None] = {}
    for machine_name, _ups_key, _target_name in dual_regime_pairs(monitored_machines, upses):
        seen.setdefault(machine_name, None)
    return tuple(seen)


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


_PUSH_METHODS = frozenset({"serial", "ssh"})


def _apply_degrades(
    machines: tuple[MonitoredMachine, ...],
    upses: dict[str, UpsConfig],
) -> tuple[tuple[MonitoredMachine, ...], dict[str, UpsConfig], tuple[ConfigNotice, ...]]:
    """Attach every load-time notice in a fixed order and return the degraded config.

    Nothing here raises, nothing is dropped, and no persisted field is written
    (INV-DEGRADE) — the only state produced is ``load_notices`` on copies plus the
    returned notice tuple. Detection reads the DECLARATION throughout (INV-DECLARED), so
    the result is recomputed identically on every load: fixing the config re-arms, and
    nothing latches or drifts.

    Order is fixed and asserted: duplicates, dual-regime conflicts (the structural
    double-shutdown is the most actionable), push-association and unknown-UPS, the
    stale-enrollment fingerprint, transports, legacy targets, then the two advisories.
    """
    working = list(machines)
    target_lists = {key: list(ups.shutdown_targets) for key, ups in upses.items()}
    notices: list[ConfigNotice] = []

    def _record(notice: ConfigNotice) -> None:
        notices.append(notice)
        if notice.severity == "error":
            logger.error("config degrade: %s", notice)
        else:
            logger.warning("config advisory: %s", notice)

    def disarm_at(i: int, message: str) -> None:
        working[i] = _disarm_machine(working[i], message)
        _record(working[i].load_notices[-1])

    def advise_at(i: int, message: str) -> None:
        working[i] = _advise_machine(working[i], message)
        _record(working[i].load_notices[-1])

    def indices(name: str) -> list[int]:
        return [i for i, m in enumerate(working) if m.name == name]

    def disarm_target(ups_key: str, target_name: str, message: str) -> None:
        for j, t in enumerate(target_lists[ups_key]):
            if t.name == target_name and not any(n.severity == "error" for n in t.load_notices):
                target_lists[ups_key][j] = _disarm_target(ups_key, t, message)
                _record(target_lists[ups_key][j].load_notices[-1])
                return

    # 1. LO-13 duplicate names. Every colliding record is KEPT — a drop is written by
    # _monitor_persist as a DELETION, since it rewrites the whole array from
    # cfg.monitored_machines, so an unrelated `monitor remove <other>` would silently
    # destroy an operator-authored record. All of them are disarmed instead: firing "one
    # of the two mt records" is a guess. Lossless on disk, fail-closed on shutdown.
    seen: dict[str, int] = {}
    for m in working:
        key = m.name.strip().casefold()
        seen[key] = seen.get(key, 0) + 1
    for i, m in enumerate(list(working)):
        if seen[m.name.strip().casefold()] > 1:
            disarm_at(
                i,
                f"duplicate monitored_machines name: {seen[m.name.strip().casefold()]} records "
                f"share this name (case-insensitively), so which one a shutdown would fire is "
                f"a guess. Every record is kept — none is dropped — and all of them are "
                f"disarmed. Give each machine a unique name.",
            )

    # 2. Dual-regime conflicts, SPLIT BY DECLARED AUTHORITY TYPE (RA-01 round 2).
    for machine_name, ups_key, target_name in dual_regime_pairs(tuple(working), upses):
        hits = indices(machine_name)
        method = working[hits[0]].shutdown_method
        if method == "native":
            # Config cannot disarm a native authority, and round 1's claim that it could
            # produced the mirror of this phase's target failure: an operator told a
            # still-protected box is unprotected, plus a cosmetic "none" that would have
            # let 02-03's guard permit a native->push switch over a live remote upsmon.
            message = (
                f"governed by BOTH shutdown regimes: declared shutdown_method='native' and "
                f"also an enabled shutdown_target {target_name!r} on UPS {ups_key!r}. The "
                f"legacy target has been disabled. This machine remains ARMED and is now the "
                f"single surviving authority — its own upsmon halts it on this primary's FSD "
                f"and lives in that box's /etc, so no config change here can disarm it. Run "
                f"'monitor verify {machine_name}' to check that secondary, and "
                f"'monitor remove {machine_name}' to actually disarm it."
            )
        else:
            message = (
                f"governed by BOTH shutdown regimes: declared shutdown_method={method!r} and "
                f"also an enabled shutdown_target {target_name!r} on UPS {ups_key!r}, which "
                f"would shut this box down twice. Both are disarmed. Remove the enabled "
                f"shutdown_target, or set this machine's shutdown_method to 'none'."
            )
        for i in hits:
            disarm_at(i, message)
        disarm_target(
            ups_key,
            target_name,
            f"disabled: it collides with monitored machine {machine_name!r}, which declares "
            f"shutdown_method={method!r}. A machine takes exactly one shutdown authority.",
        )

    # 3. Push-association (the correction to BL-01's premise) and unknown UPS names.
    unprojectable = set(unprojectable_push_machines(tuple(working), upses))
    for name in unprojectable:
        for i in indices(name):
            declared_ups = working[i].ups.strip()
            if declared_ups:
                where = f"its ups {declared_ups!r} matches no configured UPS"
            else:
                where = "its ups is blank"
            disarm_at(
                i,
                f"declares shutdown_method={working[i].shutdown_method!r} but {where}. Pushes "
                f"are projected per UPS on that UPS's low-battery event, so this machine is "
                f"projected on no event at all and can never fire. Disarmed so it does not "
                f"report as protected. Set 'ups' to the UPS that powers this box.",
            )
    for name, ups_name in unknown_ups_references(tuple(working), upses):
        if name in unprojectable:
            continue  # already disarmed, with a more specific reason
        for i in indices(name):
            advise_at(
                i,
                f"references UPS {ups_name!r}, which is not configured. Its declared "
                f"shutdown_method is {working[i].shutdown_method!r}, so nothing is disarmed "
                f"here; fix the name or add the UPS.",
            )

    # 4. IW-05, the hand-edit hole no CLI guard can close. `ip` is the nft saddr address
    # resolved by _resolve_remote_ip and is written ONLY by the native enrollment path, so
    # a push record carrying one is a probable former native secondary whose upsmon was
    # never torn down: two live authorities. Fail closed against the suspected one.
    for i, m in enumerate(list(working)):
        if m.shutdown_method.strip().lower() in _PUSH_METHODS and m.ip.strip():
            disarm_at(
                i,
                f"declares shutdown_method={m.shutdown_method!r} but carries a non-empty ip "
                f"{m.ip!r}. That address is written only by the native enrollment path, so "
                f"this is probably a former native secondary whose shutdown_method was "
                f"hand-edited — leaving the remote upsmon armed AND enabling a push, two live "
                f"authorities. Disarmed (fail closed). Run 'monitor verify {m.name}' to check "
                f"whether that secondary still answers, and 'monitor remove {m.name}' to "
                f"actually disarm it.",
            )

    # 5. Transports. The severity is chosen at THIS call site, by name, per notice.
    for name, severity, message in validate_active_transports(tuple(working)):
        for i in indices(name):
            if severity == "error":
                disarm_at(i, message)
            else:
                advise_at(i, message)

    # 6. Legacy targets that cannot fire or would misdispatch.
    for ups_key, target_name, message in validate_legacy_targets(upses):
        disarm_target(ups_key, target_name, f"disabled: {message}")

    # 7. The two ADVISORIES. Neither changes any machine's armed state — that is
    # guaranteed by the severity fold, not by these call sites being careful.
    for i, m in enumerate(list(working)):
        method = m.shutdown_method.strip().lower()
        if method in _PUSH_METHODS and requires_root_escalation(m.shutdown_cmd):
            # NEW-2, keyed on the VALUE: every record ever persisted carries an explicit
            # shutdown_cmd at exactly the wrong-for-push value, so key presence carries
            # no signal whatsoever.
            advise_at(
                i,
                f"declares shutdown_method={m.shutdown_method!r} with shutdown_cmd "
                f"{m.shutdown_cmd!r}, which invokes a root-only halt binary with no "
                f"escalation. upsmon runs SHUTDOWNCMD as root, so that is right for "
                f"'native' and wrong for a push, where it runs as the ssh user or the "
                f"far-end auto-login user. Over ssh that fails with a non-zero rc; over "
                f"serial it fails SILENTLY — the bytes are delivered, the write returns rc "
                f"0, and success is reported for a box that stayed up. Advisory only: this "
                f"machine is still armed. Declare an escalated shutdown_cmd (e.g. prefixed "
                f"with 'sudo') unless the far end logs in as root.",
            )
        elif method == "none" and m.ups.strip() and "shutdown_method" in m.raw:
            # BL-02, routed to the degrade surface rather than to a journal line, because
            # RA-01's own argument is that a log line is an insufficient operator surface.
            advise_at(
                i,
                f"declares shutdown_method='none' while naming UPS {m.ups!r}. 'none' does "
                f"NOT disarm an already-enrolled native secondary: the remote upsmon still "
                f"powers this box off on the primary's FSD. Run 'monitor verify {m.name}' to "
                f"check, and 'monitor remove {m.name}' to actually disarm it.",
            )

    degraded_upses = {
        key: dataclasses.replace(ups, shutdown_targets=tuple(target_lists[key]))
        for key, ups in upses.items()
    }
    return tuple(working), degraded_upses, tuple(notices)


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
    # Every load-time degrade, machine- and target-level, in detection order. This is a
    # machine-readable OPERATOR SURFACE — rendered by monitor list and monitor verify and
    # at watch startup (02-03) and by status and the web UI (02-08) — not a log-only
    # artefact, because RA-01's whole argument is that a journal line is insufficient.
    degraded: tuple[ConfigNotice, ...] = ()

    def ups(self, name: str) -> UpsConfig | None:
        """Look up a UPS by NUT name, returning ``None`` if it is not configured."""
        return self.upses.get(normalize_ups_name(name))

    @classmethod
    def load(cls, path: Path | str, env: Mapping[str, str] | None = None) -> Config:
        """Load and validate configuration from ``path``.

        ONE rule decides what raises: *a config error that makes the file unparseable, or
        that corrupts the MONITORING TOPOLOGY, is fatal; a config error that makes a
        SHUTDOWN AUTHORITY unsafe or unfireable disarms that authority — if it is
        disarmable — and always loads.*

        The availability half is not theoretical. ``_cmd_event`` returns 0 when
        ``_load_config`` returns ``None``, and ``upssched-cmd.sh`` invokes it for
        ``onbatt``, ``lowbatt`` and ``remote_shutdown`` — so a config that trips a
        load-time raise turns every real NUT power event into a silent no-op that reports
        success, at the exact moment the daemon exists for (IW-06).

        The seven fatal classes: unreadable file, malformed JSON, a non-object JSON root
        (LO-15), ``upses`` absent or empty, a non-object entry inside ``upses``, an
        effective ``upses`` mapping that is empty after filtering, and colliding canonical
        UPS keys. The last three are monitoring-topology corruption — ``watch`` would poll
        zero or ambiguous UPSes while looking healthy.

        Everything else degrades: see ``_apply_degrades`` and ``Config.degraded``.
        """
        environ: Mapping[str, str] = os.environ if env is None else env
        # LO-16: accept a str as well as a Path. The live probe reproduced
        # `AttributeError: 'str' object has no attribute 'read_text'` against the real
        # config file — the same escaped-handler class as LO-15, since cli._load_config
        # catches only (OSError, ValueError).
        cfg_path = Path(path)
        raw = json.loads(cfg_path.read_text())
        if not isinstance(raw, dict):
            raise ValueError(
                f"Config root in {cfg_path} is not a JSON object (got {type(raw).__name__})"
            )

        webhook_env = str(raw.get("discord_webhook_env", "UPS_DISCORD_WEBHOOK"))
        webhook = (
            environ.get(webhook_env, "").strip() or str(raw.get("discord_webhook_url", "")).strip()
        )

        upses_raw = raw.get("upses", {})
        if not isinstance(upses_raw, dict) or not upses_raw:
            raise ValueError(f"No 'upses' configured in {cfg_path}")

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
        # A non-empty raw `upses` whose values are all non-objects passed the check above
        # and then filtered to {}; `watch` polled zero UPSes and looked healthy. Loud
        # refusal beats a silently useless daemon.
        if not upses:
            raise ValueError(f"No usable 'upses' in {cfg_path}: every entry is a non-object")
        non_objects = [name for name, data in upses_raw.items() if not isinstance(data, dict)]
        if non_objects:
            # Previously filtered out silently. A monitored UPS vanishing from the
            # topology is not a shutdown-authority error — it is a monitor that quietly
            # stops watching a UPS.
            raise ValueError(
                f"Non-object 'upses' entr{'y' if len(non_objects) == 1 else 'ies'} in "
                f"{cfg_path}: {', '.join(repr(n) for n in non_objects)}"
            )
        _index, collisions = canonical_ups_index(upses)
        if collisions:
            raise ValueError(
                f"UPS names in {cfg_path} collide when canonicalised, making projection and "
                f"lookup ambiguous: "
                + "; ".join(f"{first!r} and {second!r}" for first, second in collisions)
            )

        machines_raw = raw.get("monitored_machines", [])
        monitored_machines = (
            tuple(MonitoredMachine.from_dict(m) for m in machines_raw if isinstance(m, dict))
            if isinstance(machines_raw, list)
            else ()
        )

        # RA-01: per-machine mutual exclusion is still ENFORCED (P2-06) — but by disarming
        # every disarmable authority and reporting it, not by refusing to load. See
        # _apply_degrades for the split by authority type and the full notice set.
        monitored_machines, upses, degraded = _apply_degrades(monitored_machines, upses)

        remnant = legacy_only_targets(monitored_machines, upses)
        if remnant:
            logger.warning(
                "Legacy shutdown_target(s) %s have no monitored_machines entry, so no "
                "per-machine shutdown_method governs them; they still fire under the "
                "legacy regime. Migrate them to a monitored_machines entry with an "
                "explicit shutdown_method.",
                ", ".join(remnant),
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
            degraded=degraded,
        )
