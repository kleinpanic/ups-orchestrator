from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from ups_orchestrator.config import (
    BackupShutdown,
    Config,
    ConfigNotice,
    MonitoredMachine,
    ShutdownTarget,
    UpsConfig,
    any_disarming,
    canonical_ups_index,
    canonical_ups_key,
    derive_shutdown_method,
    dual_regime_conflicts,
    dual_regime_pairs,
    is_disarming,
    is_disarming_severity,
    is_serial_device_path,
    legacy_only_targets,
    requires_root_escalation,
    unknown_ups_references,
    unprojectable_push_machines,
    validate_active_transports,
    validate_legacy_targets,
)

REPO_ROOT = Path(__file__).resolve().parents[1]


def _write(tmp_path: Path, data: dict[str, object]) -> Path:
    p = tmp_path / "config.json"
    p.write_text(json.dumps(data))
    return p


def test_webhook_from_env_overrides_file(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "discord_webhook_env": "MY_HOOK",
            "discord_webhook_url": "https://file-value",
            "upses": {"u1": {"label": "U1"}},
        },
    )
    cfg = Config.load(p, env={"MY_HOOK": "https://env-value"})
    assert cfg.webhook_url == "https://env-value"


def test_webhook_falls_back_to_file(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {"discord_webhook_url": "https://file-value", "upses": {"u1": {"label": "U1"}}},
    )
    cfg = Config.load(p, env={})
    assert cfg.webhook_url == "https://file-value"


def test_no_upses_raises(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {}})
    with pytest.raises(ValueError):
        Config.load(p, env={})


def test_per_ups_targets_parse(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "Rack",
                    "shutdown_targets": [
                        {
                            "name": "srv",
                            "kind": "remote",
                            "enabled": True,
                            "host": "h",
                            "user": "u",
                            "battery_below": 50,
                        },
                        {"name": "pi", "kind": "local", "enabled": True, "runtime_below": 120},
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    u1 = cfg.ups("u1")
    assert u1 is not None
    assert len(u1.shutdown_targets) == 2
    srv, pi = u1.shutdown_targets
    assert srv.name == "srv" and srv.is_local is False and srv.battery_below == 50
    assert srv.runtime_below is None
    assert pi.is_local is True and pi.runtime_below == 120 and pi.battery_below is None
    assert cfg.ups("missing") is None


def test_ups_lookup_accepts_nut_upsname_with_host(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    cfg = Config.load(p, env={})
    assert cfg.ups("u1@localhost") == cfg.ups("u1")
    assert cfg.ups("  u1@127.0.0.1  ") == cfg.ups("u1")


def test_poll_defaults_and_backcompat(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    assert Config.load(p, env={}).poll_seconds == 30
    # pre-0.3 name still honoured
    p2 = _write(tmp_path, {"poll_on_battery_seconds": 15, "upses": {"u1": {"label": "U1"}}})
    cfg2 = Config.load(p2, env={})
    assert cfg2.poll_seconds == 15
    assert cfg2.countdown_every_seconds == 60


def test_shutdown_scope_default_and_override(tmp_path: Path) -> None:
    # default is "remote" when unset
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    cfg = Config.load(p, env={})
    assert cfg.shutdown_scope == "remote"
    assert cfg.ups("u1").shutdown_scope == "remote"  # type: ignore[union-attr]

    # global default applies; per-UPS overrides it; "both" normalises to "all"
    p2 = _write(
        tmp_path,
        {
            "shutdown_scope": "all",
            "upses": {
                "inherit": {"label": "I"},
                "override": {"label": "O", "shutdown_scope": "remote"},
                "alias": {"label": "A", "shutdown_scope": "both"},
            },
        },
    )
    cfg2 = Config.load(p2, env={})
    assert cfg2.shutdown_scope == "all"
    assert cfg2.ups("inherit").shutdown_scope == "all"  # type: ignore[union-attr]
    assert cfg2.ups("override").shutdown_scope == "remote"  # type: ignore[union-attr]
    assert cfg2.ups("alias").shutdown_scope == "all"  # type: ignore[union-attr]


def test_shutdown_policy_defaults_disabled(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    cfg = Config.load(p, env={})
    assert cfg.shutdown_policy.enabled is False
    assert cfg.shutdown_policy.external.enabled is False
    assert cfg.shutdown_policy.internal.enabled is False
    assert cfg.shutdown_policy.external.battery_below == 15
    assert cfg.shutdown_policy.external.runtime_below == 300
    assert cfg.ups("u1").shutdown_policy == cfg.shutdown_policy  # type: ignore[union-attr]


def test_shutdown_policy_parses_central_surface(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "shutdown": {
                "enabled": True,
                "require_power_outage": True,
                "min_on_battery_seconds": 300,
                "external": {
                    "enabled": True,
                    "battery_below": 12,
                    "runtime_below": 180,
                },
                "internal": {
                    "enabled": False,
                    "battery_below": 8,
                    "runtime_below": 60,
                },
            },
            "upses": {"u1": {"label": "U1"}},
        },
    )
    cfg = Config.load(p, env={})
    assert cfg.shutdown_policy.enabled is True
    assert cfg.shutdown_policy.min_on_battery_seconds == 300
    assert cfg.shutdown_policy.external.enabled is True
    assert cfg.shutdown_policy.external.battery_below == 12
    assert cfg.shutdown_policy.external.runtime_below == 180
    assert cfg.shutdown_policy.internal.enabled is False
    assert cfg.shutdown_policy.internal.battery_below == 8
    assert cfg.shutdown_policy.internal.runtime_below == 60


def test_nut_server_defaults_when_absent(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    cfg = Config.load(p, env={})
    assert cfg.nut_server.listen == ("127.0.0.1", "::1")
    assert cfg.nut_server.port == 3493
    assert cfg.nut_server.secondary_user == "upsmon_secondary"


def test_nut_server_parses(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "nut_server": {
                "listen": ["127.0.0.1", "192.168.1.125"],
                "port": 3494,
                "secondary_user": "sec",
            },
            "upses": {"u1": {"label": "U1"}},
        },
    )
    cfg = Config.load(p, env={})
    assert cfg.nut_server.listen == ("127.0.0.1", "192.168.1.125")
    assert cfg.nut_server.port == 3494
    assert cfg.nut_server.secondary_user == "sec"


def test_monitored_machines_parse(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "monitored_machines": [
                {"name": "mt", "ssh": "mt", "ups": "cyberpower", "powervalue": 2},
            ],
            "upses": {"u1": {"label": "U1"}},
        },
    )
    cfg = Config.load(p, env={})
    assert len(cfg.monitored_machines) == 1
    m = cfg.monitored_machines[0]
    assert m.name == "mt" and m.ssh == "mt" and m.ups == "cyberpower"
    assert m.powervalue == 2
    assert m.backup.enabled is False  # default-off backup


@pytest.mark.parametrize("shape", [{}, None, "spark", 7])
def test_non_list_monitored_machines_refuses_to_load(tmp_path: Path, shape: object) -> None:
    # The silent-unprotection hole. Authored as an OBJECT keyed by name, or as `null`,
    # this used to coerce to `()`: zero machines, zero degraded notices, zero log lines,
    # and a status banner reading healthy while every push machine was unprotected. It
    # is the identical defect class already closed for `upses`.
    p = _write(tmp_path, {"monitored_machines": shape, "upses": {"u1": {"label": "U1"}}})
    with pytest.raises(ValueError, match="monitored_machines"):
        Config.load(p, env={})


def test_non_dict_monitored_machine_entry_refuses_to_load(tmp_path: Path) -> None:
    # A single bad entry used to be dropped just as silently, unprotecting exactly the
    # machine the operator fat-fingered. The error names the index so it is findable.
    p = _write(
        tmp_path,
        {
            "monitored_machines": [
                {"name": "mt", "ssh": "mt", "ups": "u1"},
                "spark",
            ],
            "upses": {"u1": {"label": "U1"}},
        },
    )
    with pytest.raises(ValueError, match="index 1"):
        Config.load(p, env={})


def test_absent_monitored_machines_is_still_fine(tmp_path: Path) -> None:
    # Back-compat (P2-07): an ABSENT key is not an authored mistake. Every pre-Phase-2
    # config on disk omits it, and none of them may start refusing to load.
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    assert Config.load(p, env={}).monitored_machines == ()
    # An explicitly empty array is equally fine.
    sub = tmp_path / "sub"
    sub.mkdir()
    p2 = _write(sub, {"monitored_machines": [], "upses": {"u1": {"label": "U1"}}})
    assert Config.load(p2, env={}).monitored_machines == ()


def test_monitored_machine_to_dict_preserves_unknown_keys() -> None:
    # WR-02: an operator-authored per-machine key (e.g. _comment) must survive a
    # parse → to_dict round-trip instead of being dropped by known-fields-only.
    from ups_orchestrator.config import MonitoredMachine

    m = MonitoredMachine.from_dict(
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "_comment": "kitchen pi", "tag": 7}
    )
    out = m.to_dict()
    assert out["_comment"] == "kitchen pi"
    assert out["tag"] == 7
    # Known fields are still normalized/emitted.
    assert out["name"] == "mt" and out["ups"] == "cyberpower"
    assert out["backup"] == {"enabled": False, "kind": "remote"}


def test_monitored_machines_default_empty(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    cfg = Config.load(p, env={})
    assert cfg.monitored_machines == ()


def test_dual_regime_conflict_degrades_and_loads(tmp_path: Path) -> None:
    # RA-01 (round 2), replacing the round-1 hard ValueError. "mt" has a ups and no
    # explicit method, so it derives "native"; an enabled shutdown_target with the same
    # name on that UPS is the classic double-shutdown. Mutual exclusion is still
    # ENFORCED — but by disarming the disarmable authority, not by refusing to load.
    # Config.load raising here made every real NUT power event a silent successful
    # no-op, because _cmd_event returns 0 when _load_config returns None (IW-06).
    p = _write(
        tmp_path,
        {
            "monitored_machines": [{"name": "mt", "ssh": "mt", "ups": "u1"}],
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                }
            },
        },
    )
    cfg = Config.load(p, env={})
    (machine,) = cfg.monitored_machines
    # Declared native: the surviving authority is the remote box's own upsmon, which
    # lives in that box's /etc. Config cannot disarm it and must not claim to.
    assert machine.shutdown_method == "native"
    assert machine.disarmed is False
    assert machine.effective_method == "native"
    # The legacy target IS in-process, so disabling it is real.
    (target,) = cfg.ups("u1").shutdown_targets  # type: ignore[union-attr]
    assert target.enabled is True  # the DECLARATION is never rewritten
    assert target.effective_enabled is False
    assert cfg.degraded
    assert any("monitor remove mt" in n.message for n in cfg.degraded)


def test_dual_regime_no_conflict_when_target_disabled(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "monitored_machines": [{"name": "mt", "ssh": "mt", "ups": "u1"}],
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [{"name": "mt", "enabled": False}],
                }
            },
        },
    )
    cfg = Config.load(p, env={})
    assert dual_regime_conflicts(cfg.monitored_machines, cfg.upses) == ()


# --- Phase 2 Task 1: shutdown_method + serial fields on MonitoredMachine ---


def test_shutdown_method_parses_allow_set() -> None:
    for method in ("none", "native", "serial", "ssh"):
        m = MonitoredMachine.from_dict({"name": "x", "shutdown_method": method})
        assert m.shutdown_method == method


def test_shutdown_method_unknown_coerces_to_none(caplog) -> None:
    with caplog.at_level("WARNING"):
        m = MonitoredMachine.from_dict({"name": "x", "shutdown_method": "telepathy"})
    assert m.shutdown_method == "none"
    assert any("telepathy" in rec.message for rec in caplog.records)


def test_serial_baud_absent_is_none_never_assumed() -> None:
    # P2-08 restated (02-06 deliberate change 3): an ABSENT serial_baud on a
    # machine is None, not a silently assumed 9600. The operator declares the
    # baud; assuming one is the same silent-coercion class as HI-04. Config.load
    # disarms such a machine rather than guessing at the live line's rate.
    m = MonitoredMachine.from_dict(
        {
            "name": "mt",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
        }
    )
    assert m.serial_baud is None
    assert m.serial_device == "/dev/ttyUSB0"


def test_serial_fields_parse_explicit() -> None:
    m = MonitoredMachine.from_dict(
        {
            "name": "mt",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/serial/by-id/x",
            "serial_baud": 19200,
        }
    )
    assert m.serial_device == "/dev/serial/by-id/x"
    assert m.serial_baud == 19200


def test_derive_native_when_ups_present() -> None:
    # has-ups => native. NOTE: this fixture does NOT pin the branch ORDER — it
    # sets backup.enabled=False, so the backup branch would return "none" anyway.
    # The ordering is pinned by test_derive_native_ordering_beats_enabled_backup.
    method = derive_shutdown_method(
        ssh="spark", ups="cyberpower", backup=BackupShutdown(enabled=False, kind="remote")
    )
    assert method == "native"


def test_derive_none_when_nothing() -> None:
    method = derive_shutdown_method(
        ssh="", ups="", backup=BackupShutdown(enabled=False, kind="remote")
    )
    assert method == "none"


def test_derive_backup_remote_to_ssh_only_when_enabled() -> None:
    # No ups, backup enabled+remote => ssh
    assert (
        derive_shutdown_method(
            ssh="host", ups="", backup=BackupShutdown(enabled=True, kind="remote")
        )
        == "ssh"
    )
    # No ups, backup enabled+serial => serial requires migration (no serial fields to project)
    # handled by from_dict; the raw derivation for serial maps to "serial".
    assert (
        derive_shutdown_method(ssh="", ups="", backup=BackupShutdown(enabled=True, kind="serial"))
        == "serial"
    )
    # backup disabled => never derives an active method
    assert (
        derive_shutdown_method(
            ssh="host", ups="", backup=BackupShutdown(enabled=False, kind="remote")
        )
        == "none"
    )


def test_spark_derive_native_not_ssh() -> None:
    # spark's real Phase-1 shape: ups set, backup {enabled:false,kind:remote}, no shutdown_method.
    m = MonitoredMachine.from_dict(
        {
            "name": "spark",
            "ssh": "spark",
            "ups": "cyberpower",
            "backup": {"enabled": False, "kind": "remote"},
        }
    )
    assert m.shutdown_method == "native"


def test_from_dict_explicit_method_wins_over_derivation() -> None:
    m = MonitoredMachine.from_dict(
        {"name": "x", "ups": "cyberpower", "shutdown_method": "ssh", "ssh": "x"}
    )
    assert m.shutdown_method == "ssh"


def test_legacy_serial_backup_without_serial_fields_degrades(tmp_path: Path) -> None:
    # A legacy backup {enabled:true, kind:serial} has no serial_device/baud to project.
    # 02-01 made that a hard ValueError in from_dict; RA-01 removes it. Hard-failing the
    # whole daemon over an INERT field is the exact MED-08 outage — MonitoredMachine.backup
    # has zero runtime consumers, so this shape never fired anything in Phase 1 either.
    # It now parses, and Config.load disarms it with the migration remedy still named.
    m = MonitoredMachine.from_dict({"name": "mt", "backup": {"enabled": True, "kind": "serial"}})
    assert m.shutdown_method == "serial"

    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [{"name": "mt", "backup": {"enabled": True, "kind": "serial"}}],
        },
    )
    cfg = Config.load(p, env={})
    (machine,) = cfg.monitored_machines
    assert machine.disarmed is True
    assert machine.effective_method == "none"
    assert any("migrate" in n.message.lower() for n in machine.load_notices)


def test_round_trip_preserves_new_fields() -> None:
    m = MonitoredMachine.from_dict(
        {
            "name": "mt",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
            "serial_baud": 9600,
        }
    )
    out = m.to_dict()
    assert out["shutdown_method"] == "serial"
    assert out["serial_device"] == "/dev/ttyUSB0"
    assert out["serial_baud"] == 9600
    # backup still emitted (D-05 defers dropping it)
    assert out["backup"] == {"enabled": False, "kind": "remote"}


def _one_machine(tmp_path: Path, machine: dict[str, object]) -> Config:
    """Load a config whose only content is ``machine`` on a configured UPS."""
    return Config.load(
        _write(
            tmp_path,
            {"upses": {"cyberpower": {"label": "CP"}}, "monitored_machines": [machine]},
        ),
        env={},
    )


def _sole(cfg: Config) -> MonitoredMachine:
    (machine,) = cfg.monitored_machines
    return machine


def test_transport_serial_empty_device_degrades(tmp_path: Path) -> None:
    # A machine's transport being unconfigurable is not a reason to stop watching the
    # UPS. Disarm that machine; keep monitoring, alerting and notifying.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_baud": 9600,
            },
        )
    )
    assert m.disarmed is True
    assert m.effective_method == "none"
    assert any("serial_device" in n.message for n in m.load_notices)


def test_transport_serial_nonpositive_baud_degrades(tmp_path: Path) -> None:
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_device": "/dev/ttyUSB0",
                "serial_baud": 0,
            },
        )
    )
    assert m.disarmed is True
    assert any("baud" in n.message for n in m.load_notices)


def test_transport_serial_absent_baud_degrades_and_is_never_assumed(tmp_path: Path) -> None:
    # Deliberate behaviour change 3 (P2-08): an ABSENT serial_baud on a serial machine
    # DISARMS rather than silently assuming 9600. Assuming one is the same
    # silent-coercion class as HI-04, and a far-end mismatch writes garbage down the
    # line while still returning rc 0 — a silent no-shutdown.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_device": "/dev/ttyUSB0",
            },
        )
    )
    assert m.disarmed is True
    assert any("serial_baud" in n.message for n in m.load_notices)


def test_transport_serial_unparseable_baud_quotes_the_operators_own_value(
    tmp_path: Path,
) -> None:
    # HI-04 end to end: the notice quotes what the operator WROTE, not a sentinel the
    # parser invented, and the persist path leaves their value alone (INV-DEGRADE).
    cfg = _one_machine(
        tmp_path,
        {
            "name": "mt",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
            "serial_baud": "fast",
        },
    )
    m = _sole(cfg)
    assert m.disarmed is True
    assert any("'fast'" in n.message for n in m.load_notices)
    assert m.to_dict()["serial_baud"] == "fast"


def test_transport_serial_device_must_live_under_dev(tmp_path: Path) -> None:
    # MED-10 (config half): _default_serial_shutdown opens the device with mode "wb",
    # which TRUNCATES a regular file — so a typo'd path destroys that file and reports
    # success. The transport-side guard is 02-07.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_device": "/etc/ups-orchestrator/config.json",
                "serial_baud": 9600,
            },
        )
    )
    assert m.disarmed is True
    assert any("/dev/" in n.message for n in m.load_notices)


def test_transport_ssh_empty_alias_degrades(tmp_path: Path) -> None:
    m = _sole(
        _one_machine(
            tmp_path,
            {"name": "mt", "ups": "cyberpower", "shutdown_method": "ssh", "ssh": ""},
        )
    )
    assert m.disarmed is True
    assert any("ssh" in n.message for n in m.load_notices)


def test_legacy_baud_default_now_9600() -> None:
    # ShutdownTarget.baud landmine closed: default is 9600, not 115200, in BOTH the
    # parser (a legacy target that omits "baud") and the declared dataclass field.
    # `name` has no default, so the declared field default is asserted directly
    # rather than by constructing a bare ShutdownTarget().
    t = ShutdownTarget.from_dict({"name": "s", "kind": "serial", "device": "/dev/ttyUSB0"})
    assert t.baud == 9600
    declared = {f.name: f.default for f in dataclasses.fields(ShutdownTarget)}
    assert declared["baud"] == 9600


# --- Phase 2 Task 2: STRICT mutual exclusion re-keyed on the effective method ---


def _mutual_exclusion_config(tmp_path: Path, machine: dict[str, object]) -> Path:
    """A config where ``machine`` collides with an enabled legacy target on its UPS."""
    return _write(
        tmp_path,
        {
            "monitored_machines": [machine],
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {"name": "mt", "kind": "remote", "enabled": True, "host": "mt"}
                    ],
                }
            },
        },
    )


def _only_target(cfg: Config, ups_key: str = "cyberpower") -> ShutdownTarget:
    ups = cfg.ups(ups_key)
    assert ups is not None
    (target,) = ups.shutdown_targets
    return target


def test_mutual_exclusion_native_leaves_the_remote_authority_armed(tmp_path: Path) -> None:
    # RA-01 split by authority type. Round 1 applied the push disposition here and
    # rendered a live native secondary as disarmed in monitor list — WHILE the box still
    # halts on FSD. That is the mirror image of the failure this phase exists to
    # prevent: an operator told a machine is unprotected when it is not. It also fed
    # 02-03's transition guard a cosmetic "none", which would have permitted a
    # native->push switch with the remote upsmon still live.
    cfg = Config.load(
        _mutual_exclusion_config(
            tmp_path,
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "native"},
        ),
        env={},
    )
    m = _sole(cfg)
    assert m.shutdown_method == "native"
    assert m.disarmed is False
    assert m.effective_method == "native"
    # The legacy target is in-process, so THAT disarm is real.
    assert _only_target(cfg).effective_enabled is False
    # Exactly one authority survives, and the notice says which, and how to disarm it.
    (notice,) = [n for n in m.load_notices if n.severity == "error"]
    assert "monitor remove mt" in notice.message
    assert "monitor verify mt" in notice.message


def test_mutual_exclusion_none_degrades(tmp_path: Path) -> None:
    # none + enabled legacy target: the machine is declared OFF yet the legacy regime
    # would still shut it down. Fail closed rather than honour the stale target.
    cfg = Config.load(
        _mutual_exclusion_config(
            tmp_path,
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "none"},
        ),
        env={},
    )
    assert _sole(cfg).effective_method == "none"
    assert _only_target(cfg).effective_enabled is False


def test_mutual_exclusion_serial_disarms_machine_and_target(tmp_path: Path) -> None:
    cfg = Config.load(
        _mutual_exclusion_config(
            tmp_path,
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_device": "/dev/ttyUSB0",
                "serial_baud": 9600,
            },
        ),
        env={},
    )
    m = _sole(cfg)
    assert m.shutdown_method == "serial"  # the declaration is inviolable
    assert m.disarmed is True
    assert m.effective_method == "none"
    assert _only_target(cfg).effective_enabled is False
    assert {n.subject for n in cfg.degraded} == {"mt", "cyberpower/mt"}


def test_mutual_exclusion_ssh_disarms_machine_and_target(tmp_path: Path) -> None:
    cfg = Config.load(
        _mutual_exclusion_config(
            tmp_path,
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
        ),
        env={},
    )
    m = _sole(cfg)
    assert m.disarmed is True
    assert m.effective_method == "none"
    assert _only_target(cfg).effective_enabled is False


def test_mutual_exclusion_ignores_a_PUSH_target_on_a_different_ups(tmp_path: Path) -> None:
    # STILL PINNED. A serial/ssh push is projected only onto the UPS the machine names,
    # so a same-named target on another UPS fires on a different outage — same target
    # name, different power domain, not a conflict.
    p = _write(
        tmp_path,
        {
            "monitored_machines": [
                {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}
            ],
            "upses": {
                "cyberpower": {"label": "CP"},
                "other": {
                    "label": "Other",
                    "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
                },
            },
        },
    )
    cfg = Config.load(p, env={})
    assert dual_regime_conflicts(cfg.monitored_machines, cfg.upses) == ()


def test_mutual_exclusion_CATCHES_a_native_machine_against_any_ups(tmp_path: Path) -> None:
    # The carve-out above does NOT extend to `native`. A native authority is the remote
    # box's own upsmon firing on this primary's FSD — it is keyed to no UPS in this file
    # — so "different power domain" does not separate it from a legacy push anywhere.
    # Before this fix the load produced ZERO notices and two live authorities.
    p = _write(
        tmp_path,
        {
            "monitored_machines": [
                {"name": "spark", "ssh": "spark", "ups": "cyberpower3", "shutdown_method": "native"}
            ],
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [{"name": "spark", "enabled": True, "host": "spark"}],
                },
                "cyberpower3": {"label": "CP3"},
            },
        },
    )
    cfg = Config.load(p, env={})
    assert dual_regime_conflicts(cfg.monitored_machines, cfg.upses) == ("spark",)

    # INV-DECLARED: a native machine is never disarmed by config; the LEGACY side is
    # what gets disabled, leaving exactly one authority.
    (machine,) = cfg.monitored_machines
    assert machine.disarmed is False
    assert machine.effective_method == "native"
    (target,) = cfg.upses["cyberpower"].shutdown_targets
    assert target.effective_enabled is False

    # And the operator is told, including an answer to "but that is a different UPS".
    assert cfg.degraded, "a cross-UPS native collision must not be silent"
    text = " ".join(n.message for n in cfg.degraded)
    assert "DIFFERENT UPS" in text
    assert "'cyberpower3'" in text


def test_pure_legacy_target_warns_only_and_still_loads(caplog, tmp_path: Path) -> None:
    # P2-07: a shutdown_target with NO monitored_machines entry has no effective
    # method to key on, so it stays warn-only and keeps loading.
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [{"name": "fileserver", "enabled": True, "host": "fs.lan"}],
                }
            }
        },
    )
    with caplog.at_level("WARNING"):
        cfg = Config.load(p, env={})
    assert cfg.monitored_machines == ()
    assert legacy_only_targets(cfg.monitored_machines, cfg.upses) == ("u1/fileserver",)
    assert any("fileserver" in rec.getMessage() for rec in caplog.records)


def test_legacy_only_targets_excludes_local_and_disabled(tmp_path: Path) -> None:
    # A local target is the watcher host, never a monitored_machines entry, and a
    # disabled target fires nothing — neither is a migration remnant.
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [
                        {"name": "this-host", "kind": "local", "enabled": True},
                        {"name": "off", "kind": "remote", "enabled": False, "host": "h"},
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    assert legacy_only_targets(cfg.monitored_machines, cfg.upses) == ()


# --- Phase 2 Task 3: regressions — spark derivation, shipped baud, legacy load ---


def test_legacy_serial_load_defaults_9600(tmp_path: Path) -> None:
    # A pre-Phase-2 config: serial shutdown_target with no "baud" key and no
    # monitored_machines entry. It must still load, at 9600 — not the 115200
    # landmine that silently sends garbage down a 9600 console (P2-08).
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [
                        {
                            "name": "bigserver-serial",
                            "kind": "serial",
                            "enabled": True,
                            "device": "/dev/ttyUSB0",
                        }
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    u1 = cfg.ups("u1")
    assert u1 is not None
    (target,) = u1.shutdown_targets
    assert target.is_serial and target.baud == 9600


def test_config_example_baud_9600_no_115200() -> None:
    # install.sh copies config.example.json verbatim to /etc, so the shipped default
    # is the landmine that reaches real operators. Negative-grep the whole file.
    example = REPO_ROOT / "config.example.json"
    text = example.read_text()
    assert "115200" not in text
    data = json.loads(text)
    serial_targets = [
        t
        for ups in data["upses"].values()
        for t in ups.get("shutdown_targets", [])
        if t.get("kind") == "serial"
    ]
    assert serial_targets
    assert all(t["baud"] == 9600 for t in serial_targets)


def test_config_example_scopes_native_mutual_exclusion_across_every_ups() -> None:
    # IF-07. The shipped comment said the collision was scoped to "the same UPS".
    # The cross-UPS widening made that false for a declared `native` machine — it is
    # scanned against EVERY configured UPS, and `_monitor_add`'s own refusal text
    # says "on ANY configured UPS". install.sh copies this file verbatim into
    # production, so the stale sentence is what a real operator reads while deciding
    # whether two shutdown authorities can coexist.
    example = REPO_ROOT / "config.example.json"
    comment = json.loads(example.read_text())["upses"]["ups1"]["shutdown_targets__comment"]
    assert "on the same UPS" not in comment
    assert "on ANY configured UPS" in comment


def test_malformed_json_config_loads_as_none(monkeypatch, tmp_path: Path) -> None:
    from ups_orchestrator import cli

    bad = tmp_path / "config.json"
    bad.write_text("{ not valid json ")
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(bad))
    assert cli._load_config() is None  # graceful, not a crash


# --- Plan 02-06 Task 1: the declaration/effect model (INV-DEGRADE, INV-SEVERITY) ---


def test_config_notice_renders_severity_subject_and_message() -> None:
    n = ConfigNotice(severity="error", subject="mt", message="no serial device")
    assert str(n) == "[error] mt: no serial device"


# NEW-1 — the has-ups => native ORDERING. Every pre-existing derivation test sets
# backup.enabled=False whenever ups is set, so the ordering was never pinned and two
# independent branch-inverting mutants passed all 41 tests. The discriminating shape
# below sets ups AND backup{enabled:true} SIMULTANEOUSLY: swap the two branches and
# these assertions fail. It matters because this shape carries a MATCHING ups, so a
# mis-derived push method IS projected and DOES fire — an ssh/serial push onto a box
# whose own upsmon is already self-halting on FSD. It is the only surviving route to a
# genuine double-shutdown once every other 02-06 fix lands.


def test_derive_native_ordering_beats_enabled_backup() -> None:
    assert (
        derive_shutdown_method(
            ssh="spark", ups="cyberpower", backup=BackupShutdown(enabled=True, kind="remote")
        )
        == "native"
    )
    assert (
        derive_shutdown_method(
            ssh="spark", ups="cyberpower", backup=BackupShutdown(enabled=True, kind="serial")
        )
        == "native"
    )


def test_derive_native_ordering_beats_enabled_backup_through_from_dict() -> None:
    # The same ordering through the real parse path an operator's record takes.
    for kind in ("remote", "serial"):
        m = MonitoredMachine.from_dict(
            {
                "name": "spark",
                "ssh": "spark",
                "ups": "cyberpower",
                "backup": {"enabled": True, "kind": kind},
            }
        )
        assert m.shutdown_method == "native", kind


# --- one strict baud parser, both paths (HI-04 + LO-12) ---


def test_machine_serial_baud_strict_parse() -> None:
    def baud(value: object) -> int | None:
        return MonitoredMachine.from_dict({"name": "mt", "serial_baud": value}).serial_baud

    assert baud(19200) == 19200
    assert baud("19200") == 19200
    assert baud("  9600  ") == 9600  # whitespace was never the defect
    assert baud(True) is None  # JSON boolean, not a baud
    assert baud(115.2) is None  # a float is not an integer-valued declaration
    assert baud("115.2k") is None
    assert baud("fast") is None
    assert MonitoredMachine.from_dict({"name": "mt"}).serial_baud is None


def test_machine_serial_baud_accepts_nested_block() -> None:
    m = MonitoredMachine.from_dict(
        {"name": "mt", "serial": {"device": "/dev/ttyUSB0", "baud": 9600}}
    )
    assert m.serial_device == "/dev/ttyUSB0"
    assert m.serial_baud == 9600


def test_target_baud_strict_parse() -> None:
    def baud(value: object) -> int | None:
        return ShutdownTarget.from_dict({"name": "s", "kind": "serial", "baud": value}).baud

    assert baud(19200) == 19200
    assert baud("19200") == 19200
    assert baud("  9600  ") == 9600
    assert baud(True) is None
    assert baud(115.2) is None
    assert baud("fast") is None
    # LO-12: an ABSENT key keeps the 9600 back-compat default (P2-07); only a
    # DECLARED-but-unparseable value yields None. Previously "fast" became 9600 and
    # sailed past the baud <= 0 check.
    assert ShutdownTarget.from_dict({"name": "s", "kind": "serial"}).baud == 9600


def test_to_dict_leaves_unparseable_baud_untouched() -> None:
    # INV-DEGRADE at the to_dict boundary: a value that could not be parsed is never
    # written back, so the operator's own "fast" survives the raw merge verbatim and
    # the notice can quote what they actually wrote — forever, across any number of
    # monitor add/remove persists.
    m = MonitoredMachine.from_dict({"name": "mt", "serial_baud": "fast"})
    assert m.serial_baud is None
    assert m.to_dict()["serial_baud"] == "fast"


def test_to_dict_emits_a_parsed_baud() -> None:
    m = MonitoredMachine.from_dict({"name": "mt", "serial_baud": "19200"})
    assert m.to_dict()["serial_baud"] == 19200


def test_to_dict_omits_serial_baud_when_undeclared() -> None:
    assert "serial_baud" not in MonitoredMachine.from_dict({"name": "mt"}).to_dict()


def test_to_dict_preserves_an_unparseable_nested_baud(tmp_path: Path) -> None:
    # HI-01, the nested-form half of the survival property
    # test_to_dict_leaves_unparseable_baud_untouched pins for the flat form. LO-14 pops
    # the nested block whole and the `serial_baud is not None` guard then declined to
    # emit a replacement, so an unrelated `monitor add` DELETED the operator's
    # declaration and the notice mutated from "declares serial_baud 'fast'" to the
    # generic "with no serial_baud" — destroying the diagnostic and breaking the
    # idempotence validate_active_transports' docstring claims.
    record: dict[str, object] = {
        "name": "mt",
        "ups": "cyberpower",
        "shutdown_method": "serial",
        "serial": {"device": "/dev/ttyUSB0", "baud": "fast"},
    }
    m = MonitoredMachine.from_dict(record)
    assert m.serial_baud is None
    before = validate_active_transports((m,))

    persisted = m.to_dict()
    assert persisted["serial_baud"] == "fast"  # verbatim, not a parser sentinel
    after = validate_active_transports((MonitoredMachine.from_dict(persisted),))

    assert after == before
    assert any("'fast'" in message for _n, _s, message in before)
    # ...and it survives any number of persists, not just the first.
    third = MonitoredMachine.from_dict(MonitoredMachine.from_dict(persisted).to_dict())
    assert validate_active_transports((third,)) == before


def test_to_dict_flat_serial_baud_still_wins_over_a_nested_one() -> None:
    # The lift must never resurrect a nested value the flat field already answers for,
    # or the nested block becomes a second source of truth again (LO-14).
    m = MonitoredMachine.from_dict({"name": "mt", "serial_baud": 19200, "serial": {"baud": "fast"}})
    assert m.to_dict()["serial_baud"] == 19200


def test_to_dict_drops_nested_serial_block() -> None:
    # LO-14: emitting a stale nested block alongside the flat fields gives a record two
    # contradictory sources of truth, so an operator editing the nested form is silently
    # ignored. The nested form stays ACCEPTED on input; the flat fields are the authority.
    m = MonitoredMachine.from_dict(
        {"name": "mt", "serial": {"device": "/dev/ttyUSB0", "baud": 9600}}
    )
    out = m.to_dict()
    assert "serial" not in out
    assert out["serial_device"] == "/dev/ttyUSB0"
    assert out["serial_baud"] == 9600


# --- INV-SEVERITY: the notice/disarm seam is a TYPE, not a rule to remember ---


def _err(msg: str = "boom") -> ConfigNotice:
    return ConfigNotice(severity="error", subject="mt", message=msg)


def _adv(msg: str = "look at this") -> ConfigNotice:
    return ConfigNotice(severity="advisory", subject="mt", message=msg)


def test_config_notice_rejects_an_unknown_severity() -> None:
    # MED-01. severity was an unvalidated str compared against a bare literal at nine
    # sites, every one of which read "not error" as advisory — so a typo silently
    # downgraded a disarm and the machine stayed ARMED while status labelled it
    # ADVISORY. Rejecting the value at construction means it cannot reach a fold.
    for bad in ("Error", "errror", "critical", "", "ERROR"):
        with pytest.raises(ValueError, match="severity"):
            ConfigNotice(severity=bad, subject="mt", message="boom")
    # dataclasses.replace runs __post_init__ too, so the four INV-DEGRADE helpers
    # cannot smuggle one in either.
    with pytest.raises(ValueError, match="severity"):
        dataclasses.replace(_err(), severity="critical")


def test_the_severity_fold_fails_closed_on_an_unrecognised_value() -> None:
    # The other half: _transport_notices produces severities as FREE strings that never
    # pass the construction boundary, so the fold itself has to be oriented to disarm
    # on anything it does not recognise.
    assert is_disarming_severity("error") is True
    assert is_disarming_severity("critical") is True
    assert is_disarming_severity("Error") is True
    assert is_disarming_severity("") is True
    assert is_disarming_severity("advisory") is False
    assert is_disarming(_err()) is True
    assert is_disarming(_adv()) is False


def test_an_unrecognised_transport_severity_disarms_rather_than_arms(monkeypatch) -> None:
    # Reachability for the fold above, through the real degrade path: step 5 of
    # _apply_degrades chooses disarm-vs-advise from a bare string. A future "critical"
    # used to leave the machine ARMED and merely mention it.
    import ups_orchestrator.config as config_mod

    monkeypatch.setattr(
        config_mod,
        "validate_active_transports",
        lambda machines: tuple(
            (m.name, "critical", "a severity nobody added a branch for") for m in machines
        ),
    )
    machines = _machines({"name": "mt", "ups": "cyberpower", "ssh": "mt", "shutdown_method": "ssh"})
    upses = {"cyberpower": UpsConfig.from_dict("cyberpower", {})}

    degraded_machines, _upses, notices = config_mod._apply_degrades(machines, upses)

    assert degraded_machines[0].disarmed is True
    assert degraded_machines[0].effective_method == "none"
    assert degraded_machines[0].shutdown_method == "ssh"  # INV-DECLARED
    assert any("nobody added a branch" in n.message for n in notices)


def test_disarmed_is_structurally_false_for_native() -> None:
    # Config.load cannot disarm a native authority: the remote box's own upsmon halts
    # it on the primary's FSD and lives in that box's /etc. Marking it disarmed would
    # report an armed box as unprotected.
    m = MonitoredMachine(name="spark", shutdown_method="native", load_notices=(_err(),))
    assert m.disarmed is False
    assert m.effective_method == "native"


def test_disarmed_true_for_push_with_error_notice() -> None:
    m = MonitoredMachine(name="mt", shutdown_method="ssh", ssh="mt", load_notices=(_err(),))
    assert m.disarmed is True
    assert m.effective_method == "none"


def test_advisory_never_disarms_a_push_machine() -> None:
    # The blocker-1 guarantee. An advisory traverses the whole notice chain and changes
    # nothing in it: attaching one cannot alter effective_method, because the fold
    # ignores every non-error severity.
    one = MonitoredMachine(name="mt", shutdown_method="ssh", ssh="mt", load_notices=(_adv(),))
    assert one.disarmed is False
    assert one.effective_method == one.shutdown_method == "ssh"

    # Two advisories — the tuple ACCUMULATES rather than overwriting, which a
    # single-slot ConfigNotice | None could not do.
    two = MonitoredMachine(
        name="mt", shutdown_method="ssh", ssh="mt", load_notices=(_adv("a"), _adv("b"))
    )
    assert two.disarmed is False
    assert two.effective_method == "ssh"
    assert [n.message for n in two.load_notices] == ["a", "b"]


def test_advisory_and_error_together_disarm_and_retain_both() -> None:
    # A push machine can legitimately earn BOTH the NEW-2 advisory and a T-02-10
    # ssh-alias error in one load. Every finding is preserved; the effect is the fold.
    m = MonitoredMachine(
        name="mt", shutdown_method="ssh", ssh="-oProxyCommand=x", load_notices=(_adv(), _err())
    )
    assert m.disarmed is True
    assert m.effective_method == "none"
    assert len(m.load_notices) == 2
    assert {n.severity for n in m.load_notices} == {"advisory", "error"}


def test_any_disarming_folds_the_heading_severity() -> None:
    # The heading is a SEPARATE question from each row's severity. Both renderers
    # used to hardcode it at the maximum, so an advisory-only config announced "a
    # shutdown authority was disarmed" in red on every invocation.
    assert any_disarming(()) is False
    assert any_disarming((_adv(), _adv())) is False
    assert any_disarming((_adv(), _err())) is True
    assert any_disarming((_err(),)) is True


def test_any_disarming_keeps_the_safe_orientation_of_its_fold() -> None:
    # is_disarming_severity treats anything it does not recognise as disarming; the
    # heading must inherit that, not re-derive a laxer rule. ConfigNotice validates
    # at construction, so a free-string severity is built by bypassing __init__ the
    # same way _transport_notices' strings reach the fold.
    weird = ConfigNotice.__new__(ConfigNotice)
    object.__setattr__(weird, "severity", "critical")
    object.__setattr__(weird, "subject", "mt")
    object.__setattr__(weird, "message", "a severity nobody added a branch for")
    assert any_disarming((weird,)) is True


def test_load_notices_are_not_compared() -> None:
    # compare=False: a diagnostic overlay must not make two identical declarations
    # unequal, and must never participate in persistence identity.
    bare = MonitoredMachine(name="mt", shutdown_method="ssh", ssh="mt")
    noted = MonitoredMachine(name="mt", shutdown_method="ssh", ssh="mt", load_notices=(_err(),))
    assert bare == noted


def test_target_effective_enabled_folds_on_error() -> None:
    t = ShutdownTarget(name="mt", kind="remote", enabled=True, host="mt")
    assert t.effective_enabled is True
    disarmed = dataclasses.replace(t, load_notices=(_err(),))
    assert disarmed.enabled is True  # the DECLARATION is never rewritten
    assert disarmed.effective_enabled is False
    advised = dataclasses.replace(t, load_notices=(_adv(),))
    assert advised.effective_enabled is True


def test_target_enabled_string_false_disables() -> None:
    # MED-07: bare bool("false") is True, which made the disabled-target exemption
    # conditional on the operator having written a real JSON boolean.
    assert ShutdownTarget.from_dict({"name": "s", "enabled": "false"}).enabled is False
    assert ShutdownTarget.from_dict({"name": "s", "enabled": "no"}).enabled is False
    assert ShutdownTarget.from_dict({"name": "s", "enabled": "true"}).enabled is True
    assert ShutdownTarget.from_dict({"name": "s", "enabled": True}).enabled is True
    assert ShutdownTarget.from_dict({"name": "s"}).enabled is False


# --- NEW-2's VALUE-based predicate (round-4 blocker 2) ---


def test_requires_root_escalation_true_for_unescalated_halt_binaries() -> None:
    # /sbin/shutdown, /usr/sbin/shutdown and a bare `shutdown` on PATH are one defect
    # wearing three spellings, so the test is on the BASENAME, case-folded.
    assert requires_root_escalation("/sbin/shutdown -h now") is True
    assert requires_root_escalation("shutdown -h now") is True
    assert requires_root_escalation("/usr/sbin/shutdown -h now") is True
    assert requires_root_escalation("/usr/sbin/poweroff") is True
    assert requires_root_escalation("SHUTDOWN -h now") is True
    assert requires_root_escalation("halt") is True
    assert requires_root_escalation("telinit 0") is True
    assert requires_root_escalation("systemctl poweroff") is True
    assert requires_root_escalation("/usr/bin/systemctl --no-block halt") is True


def test_requires_root_escalation_false_when_escalated_or_custom() -> None:
    assert requires_root_escalation("sudo /sbin/shutdown -h now") is False
    assert requires_root_escalation("doas shutdown -h now") is False
    assert requires_root_escalation("pkexec systemctl poweroff") is False
    assert requires_root_escalation("su -c 'shutdown -h now'") is False
    assert requires_root_escalation("run0 poweroff") is False
    # A genuinely custom command (a setuid wrapper, an operator script) is never nagged.
    assert requires_root_escalation("/usr/local/bin/my-halt") is False
    assert requires_root_escalation("systemctl restart foo") is False
    assert requires_root_escalation("") is False
    assert requires_root_escalation("   ") is False


def test_requires_root_escalation_never_raises_on_unbalanced_quote() -> None:
    # A predicate that raises out of the load path would turn an advisory into an
    # outage. shlex.split raises on an unbalanced quote; the fallback is str.split.
    result = requires_root_escalation('/sbin/shutdown -h "now')
    assert isinstance(result, bool)
    assert result is True
    assert requires_root_escalation("sudo 'shutdown") is False


# --- the legacy-target disarm set ---


def _ups_with_targets(*targets: dict[str, object]) -> dict[str, UpsConfig]:
    return {"cyberpower": UpsConfig.from_dict("cyberpower", {"shutdown_targets": list(targets)})}


def test_validate_legacy_targets_reports_unfireable_shapes() -> None:
    def one(target: dict[str, object]) -> str:
        found = validate_legacy_targets(_ups_with_targets(target))
        assert len(found) == 1, found
        ups_key, target_name, message = found[0]
        assert ups_key == "cyberpower"
        assert target_name == target["name"]
        return message

    # POSIX B0 means hang up the line, so `stty -F <dev> 0` disconnects the very
    # console it was about to write to.
    assert "baud" in one(
        {"name": "a", "kind": "serial", "enabled": True, "device": "/dev/ttyUSB0", "baud": 0}
    )
    assert "baud" in one(
        {"name": "b", "kind": "serial", "enabled": True, "device": "/dev/ttyUSB0", "baud": "fast"}
    )
    assert "device" in one({"name": "c", "kind": "serial", "enabled": True, "baud": 9600})
    # config.py accepts a blank host and ssh can never connect to it.
    assert "host" in one({"name": "d", "kind": "remote", "enabled": True})
    # events.py dispatch treats anything not local and not serial as SSH, so an
    # unknown kind silently becomes an ssh attempt against a blank host.
    assert "kind" in one({"name": "e", "kind": "telepathy", "enabled": True, "host": "h"})


def test_validate_legacy_targets_rejects_option_shaped_host_and_user() -> None:
    # BL-01. `MonitoredMachine.ssh` was hardened by T-02-10 because it becomes an argv
    # element in an unattended ssh at outage time. The legacy target reaches the
    # IDENTICAL sink via `events.ssh_dest` and was checked only for blankness, so
    # `-oProxyCommand=...` validated clean and stayed armed.
    def messages(target: dict[str, object]) -> list[str]:
        return [m for _ups, _name, m in validate_legacy_targets(_ups_with_targets(target))]

    injected = "-oProxyCommand=touch /tmp/pwn"
    host_problems = messages({"name": "h", "kind": "remote", "enabled": True, "host": injected})
    assert len(host_problems) == 1
    assert "host" in host_problems[0] and repr(injected) in host_problems[0]

    # `ssh_dest` concatenates f"{user}@{host}", so a leading '-' can come from either.
    user_problems = messages(
        {"name": "u", "kind": "remote", "enabled": True, "host": "mt", "user": "-oProxyCommand=x"}
    )
    assert len(user_problems) == 1
    assert "user" in user_problems[0]

    # Shell metacharacters are carried verbatim into the argv too.
    assert messages({"name": "s", "kind": "remote", "enabled": True, "host": "mt;reboot"})

    # Legitimate operator spellings stay clean.
    assert messages({"name": "a", "kind": "remote", "enabled": True, "host": "mt"}) == []
    assert (
        messages(
            {"name": "b", "kind": "remote", "enabled": True, "host": "h.example:22", "user": "root"}
        )
        == []
    )


def test_validate_legacy_targets_silent_on_healthy_and_disabled() -> None:
    healthy = _ups_with_targets(
        {"name": "ok-remote", "kind": "remote", "enabled": True, "host": "h"},
        {"name": "ok-serial", "kind": "serial", "enabled": True, "device": "/dev/ttyUSB0"},
        {"name": "ok-local", "kind": "local", "enabled": True},
        # DECLARED-disabled targets fire nothing, so they are never reported.
        {"name": "off", "kind": "telepathy", "enabled": False},
    )
    assert validate_legacy_targets(healthy) == ()


# --- Plan 02-06 Task 2: one canonical UPS key, the corrected detector, BL-01 ---
#
# These exercise the pure detectors DIRECTLY rather than through Config.load, which
# still raises on a conflict until Task 3.


def _machines(*records: dict[str, object]) -> tuple[MonitoredMachine, ...]:
    return tuple(MonitoredMachine.from_dict(r) for r in records)


def _upses(**spec: dict[str, object]) -> dict[str, UpsConfig]:
    return {name: UpsConfig.from_dict(name, data) for name, data in spec.items()}


def _enabled_mt_target() -> dict[str, object]:
    return {"shutdown_targets": [{"name": "mt", "kind": "remote", "enabled": True, "host": "mt"}]}


def test_canonical_ups_key_strips_host_whitespace_and_case() -> None:
    assert canonical_ups_key("cyberpower2") == "cyberpower2"
    assert canonical_ups_key("CyberPower2") == "cyberpower2"
    assert canonical_ups_key("  cyberpower2  ") == "cyberpower2"
    assert canonical_ups_key("CyberPower2@localhost") == "cyberpower2"
    assert canonical_ups_key("") == ""
    assert canonical_ups_key("   ") == ""


def test_canonical_ups_index_maps_and_reports_collisions() -> None:
    index, collisions = canonical_ups_index(_upses(CyberPower2={"label": "CP2"}))
    assert collisions == ()
    assert index["cyberpower2"].name == "CyberPower2"

    # Two authored keys that canonicalise to one make projection and lookup
    # ambiguous — monitoring-topology corruption, so Config.load raises on it.
    _index, collisions = canonical_ups_index(
        _upses(cyberpower2={"label": "a"}, CyberPower2={"label": "b"})
    )
    assert collisions == (("cyberpower2", "CyberPower2"),)


def test_dual_regime_pairs_returns_machine_ups_target_triples() -> None:
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}),
        _upses(cyberpower=_enabled_mt_target()),
    )
    # The triple lets a caller disarm the exact target, not merely learn a machine name.
    assert pairs == (("mt", "cyberpower", "mt"),)


def test_dual_regime_pairs_scans_every_ups_when_the_machine_has_no_ups() -> None:
    # BL-01. EVERY pre-existing mutual-exclusion fixture sets "ups": "cyberpower", which
    # is the shared blind spot that let 41 green tests miss this: a blank ups resolved to
    # no UpsConfig and the machine was SKIPPED, so the collision was never seen. A typo
    # must widen the scan, never silence it.
    upses = _upses(cyberpower=_enabled_mt_target())
    # derived ssh: no ups, backup {enabled:true, kind:remote}
    derived = _machines({"name": "mt", "ssh": "mt", "backup": {"enabled": True, "kind": "remote"}})
    assert derived[0].shutdown_method == "ssh"
    assert dual_regime_pairs(derived, upses) == (("mt", "cyberpower", "mt"),)
    # explicit ssh with no ups
    explicit = _machines({"name": "mt", "ssh": "mt", "shutdown_method": "ssh"})
    assert dual_regime_pairs(explicit, upses) == (("mt", "cyberpower", "mt"),)


def test_dual_regime_pairs_scans_every_ups_when_the_ups_is_unknown() -> None:
    # A machine naming a wholly unknown UPS still collides with an enabled target of the
    # same name on some other UPS. Fail closed on the typo.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "typo", "shutdown_method": "ssh"}),
        _upses(cyberpower=_enabled_mt_target()),
    )
    assert pairs == (("mt", "cyberpower", "mt"),)


def test_dual_regime_pairs_canonicalises_the_ups_name_on_both_sides() -> None:
    # HI-03: one canonicalisation used by the detector, the push-association check and
    # (02-07) the projector, so a capitalisation cannot mean two things in two modules.
    for declared in ("CyberPower2", "  cyberpower2  ", "cyberpower2@localhost"):
        pairs = dual_regime_pairs(
            _machines({"name": "mt", "ssh": "mt", "ups": declared, "shutdown_method": "ssh"}),
            _upses(cyberpower2=_enabled_mt_target()),
        )
        assert pairs == (("mt", "cyberpower2", "mt"),), declared

    # ...and the same when the AUTHORED key is the odd one out.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower2", "shutdown_method": "ssh"}),
        _upses(CyberPower2=_enabled_mt_target()),
    )
    assert pairs == (("mt", "CyberPower2", "mt"),)


def test_dual_regime_pairs_ignores_a_PUSH_target_on_a_different_ups() -> None:
    # STILL PINNED. The machine's ups RESOLVES and the method is a push, which
    # `_machine_targets` projects onto that UPS only, so the scan is scoped to it. Same
    # target name on another UPS is a different power domain, not a conflict.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}),
        _upses(cyberpower={"label": "CP"}, other=_enabled_mt_target()),
    )
    assert pairs == ()
    # Same shape, serial rather than ssh — the carve-out is about push-ness, not ssh.
    pairs = dual_regime_pairs(
        _machines(
            {
                "name": "mt",
                "ups": "cyberpower",
                "shutdown_method": "serial",
                "serial_device": "/dev/ttyUSB0",
                "serial_baud": 9600,
            }
        ),
        _upses(cyberpower={"label": "CP"}, other=_enabled_mt_target()),
    )
    assert pairs == ()


def test_dual_regime_pairs_CATCHES_a_native_machine_on_a_different_ups() -> None:
    # A native authority is not keyed to any UPS in this config, so a resolving `ups`
    # must NOT narrow the scan. This is the cross-UPS double-shutdown the final
    # verification found surviving.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "native"}),
        _upses(cyberpower={"label": "CP"}, other=_enabled_mt_target()),
    )
    assert pairs == (("mt", "other", "mt"),)


def test_dual_regime_pairs_native_widening_still_finds_its_own_ups_once() -> None:
    # Widening must not double-report the machine's own UPS, and must find a collision
    # there as before.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "native"}),
        _upses(cyberpower=_enabled_mt_target(), other={"label": "Other"}),
    )
    assert pairs == (("mt", "cyberpower", "mt"),)


def test_dual_regime_pairs_native_reports_every_colliding_ups() -> None:
    # Two enabled targets on two UPSes are two separate authorities to disable.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "native"}),
        _upses(cyberpower=_enabled_mt_target(), other=_enabled_mt_target()),
    )
    assert pairs == (("mt", "cyberpower", "mt"), ("mt", "other", "mt"))


def test_dual_regime_pairs_ignores_a_disabled_target() -> None:
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}),
        _upses(cyberpower={"shutdown_targets": [{"name": "mt", "enabled": False, "host": "mt"}]}),
    )
    assert pairs == ()


def test_dual_regime_pairs_reads_declared_state_not_effective_state() -> None:
    # INV-DECLARED: this is what keeps cli.py's --force gate firing against an
    # already-degraded config. A machine carrying an ERROR notice is effectively "none",
    # but the detector still sees the declaration and still reports the conflict.
    disarmed = dataclasses.replace(
        MonitoredMachine.from_dict(
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}
        ),
        load_notices=(_err(),),
    )
    assert disarmed.effective_method == "none"
    assert dual_regime_pairs((disarmed,), _upses(cyberpower=_enabled_mt_target())) == (
        ("mt", "cyberpower", "mt"),
    )


def test_dual_regime_conflicts_dedups_and_preserves_first_seen_order() -> None:
    # The public signature and de-duplicated return are unchanged: cli.py's gate and its
    # error text stay behaviourally identical for the shapes they already covered.
    machines = _machines(
        {"name": "zeta", "ssh": "zeta", "shutdown_method": "ssh"},
        {"name": "mt", "ssh": "mt", "shutdown_method": "ssh"},
    )
    upses = _upses(
        cyberpower={
            "shutdown_targets": [
                {"name": "mt", "enabled": True, "host": "mt"},
                {"name": "zeta", "enabled": True, "host": "zeta"},
            ]
        },
        other={
            "shutdown_targets": [{"name": "mt", "enabled": True, "host": "mt"}],
        },
    )
    # "mt" collides on two UPSes, so dual_regime_pairs reports it twice...
    assert len(dual_regime_pairs(machines, upses)) == 3
    # ...and the projection de-duplicates it, first-seen order preserved.
    assert dual_regime_conflicts(machines, upses) == ("zeta", "mt")


def test_unknown_ups_references_reports_only_non_empty_unresolvable_names() -> None:
    machines = _machines(
        {"name": "typo", "ssh": "typo", "ups": "cyberpwoer", "shutdown_method": "ssh"},
        {"name": "blank", "ssh": "blank", "shutdown_method": "ssh"},
        {"name": "fine", "ssh": "fine", "ups": "CyberPower", "shutdown_method": "ssh"},
    )
    assert unknown_ups_references(machines, _upses(cyberpower={"label": "CP"})) == (
        ("typo", "cyberpwoer"),
    )


def test_unprojectable_push_machines_reports_the_structurally_unfireable() -> None:
    # The push-association rule, and the correction to BL-01's premise. _machine_targets
    # projects only a machine whose UPS matches the UPS being handled, so a blank or
    # unresolvable ups on a push machine is projected on NO event at all: it reports as
    # protected and can never fire. Note that derive_shutdown_method yields a push method
    # ONLY when ups is blank, so the ENTIRE derived-push class lands here.
    upses = _upses(cyberpower={"label": "CP"})
    blank = _machines({"name": "blank", "ssh": "blank", "shutdown_method": "ssh"})
    unknown = _machines(
        {"name": "typo", "ssh": "typo", "ups": "cyberpwoer", "shutdown_method": "serial"}
    )
    derived = _machines({"name": "legacy", "ssh": "legacy", "backup": {"enabled": True}})
    assert unprojectable_push_machines(blank, upses) == ("blank",)
    assert unprojectable_push_machines(unknown, upses) == ("typo",)
    assert unprojectable_push_machines(derived, upses) == ("legacy",)


def test_unprojectable_push_machines_silent_on_native_and_on_resolvable_push() -> None:
    upses = _upses(cyberpower={"label": "CP"})
    # A native machine with a blank ups is Task 3's NOTICE-ONLY case, never a push case:
    # config cannot disarm a native authority.
    native_blank = _machines({"name": "spark", "ssh": "spark", "shutdown_method": "native"})
    assert native_blank[0].shutdown_method == "native"
    assert unprojectable_push_machines(native_blank, upses) == ()
    # A push machine whose ups resolves (even case-mismatched) is projectable.
    resolvable = _machines(
        {"name": "mt", "ssh": "mt", "ups": "CyberPower@localhost", "shutdown_method": "ssh"}
    )
    assert unprojectable_push_machines(resolvable, upses) == ()
    # "none" is not a push method.
    off = _machines({"name": "off", "shutdown_method": "none"})
    assert unprojectable_push_machines(off, upses) == ()


# --- Plan 02-06 Task 3: Config.load — the hard-fail line and the degrade accumulator ---
#
# ONE rule: a config error that makes the file unparseable, or that corrupts the
# MONITORING TOPOLOGY, is fatal; a config error that makes a SHUTDOWN AUTHORITY unsafe
# or unfireable disarms that authority — if it is disarmable — and always loads.


def test_unreadable_file_raises(tmp_path: Path) -> None:
    with pytest.raises(OSError):
        Config.load(tmp_path / "does-not-exist.json", env={})


def test_malformed_json_raises(tmp_path: Path) -> None:
    p = tmp_path / "config.json"
    p.write_text("{ not valid json ")
    with pytest.raises(ValueError):
        Config.load(p, env={})


def test_non_object_json_root_raises_value_error(tmp_path: Path) -> None:
    # LO-15: this escaped cli._load_config's (OSError, ValueError) handler as a raw
    # AttributeError traceback. json.JSONDecodeError is a ValueError subclass, so
    # matching the class here keeps the handler's contract.
    for root in ([], "a string", 7):
        p = tmp_path / "config.json"
        p.write_text(json.dumps(root))
        with pytest.raises(ValueError, match="JSON object"):
            Config.load(p, env={})


def test_non_object_entry_inside_upses_raises(tmp_path: Path) -> None:
    # Previously filtered out silently: a monitored UPS vanishing from the topology is
    # not a shutdown-authority error, it is a monitor that stops watching a UPS.
    p = _write(tmp_path, {"upses": {"good": {"label": "G"}, "bad": "not-an-object"}})
    with pytest.raises(ValueError, match="bad"):
        Config.load(p, env={})


def test_upses_that_all_filter_out_raises(tmp_path: Path) -> None:
    # A non-empty raw `upses` whose values are ALL non-objects passed the existing
    # emptiness check and then filtered to {}; watch would poll zero UPSes and look
    # healthy. Loud refusal beats a silently useless daemon.
    p = _write(tmp_path, {"upses": {"a": "x", "b": 7}})
    with pytest.raises(ValueError, match="[Nn]o usable"):
        Config.load(p, env={})


def test_colliding_canonical_ups_keys_raise(tmp_path: Path) -> None:
    p = _write(tmp_path, {"upses": {"cyberpower2": {"label": "a"}, "CyberPower2": {"label": "b"}}})
    with pytest.raises(ValueError, match="canonical"):
        Config.load(p, env={})


def test_load_accepts_a_str_path(tmp_path: Path) -> None:
    # LO-16, reproduced by the live probe against the real config file:
    # AttributeError: 'str' object has no attribute 'read_text' — the same
    # escaped-handler class as LO-15.
    p = _write(tmp_path, {"upses": {"u1": {"label": "U1"}}})
    assert Config.load(str(p), env={}).ups("u1") is not None


# --- BL-01 end to end: the shapes the reviewer probed as a silent clean load ---


def _bl01_config(tmp_path: Path, machine: dict[str, object]) -> Config:
    return Config.load(
        _write(
            tmp_path,
            {
                "monitored_machines": [machine],
                "upses": {
                    "cyberpower2": {
                        "label": "CP2",
                        "shutdown_targets": [
                            {
                                "name": "mt",
                                "kind": "serial",
                                "enabled": True,
                                "device": "/dev/ttyUSB0",
                                "baud": 9600,
                            }
                        ],
                    }
                },
            },
        ),
        env={},
    )


def test_bl01_derived_ssh_with_no_ups_degrades(tmp_path: Path) -> None:
    # The reviewer's first live probe: machine mt with ssh + backup{enabled:true,
    # kind:remote} and NO ups, against an enabled serial target named mt. It loaded
    # silently and reported an active method. It is now disarmed on BOTH counts —
    # dual-regime AND unprojectable — and says so.
    cfg = _bl01_config(
        tmp_path, {"name": "mt", "ssh": "mt", "backup": {"enabled": True, "kind": "remote"}}
    )
    m = _sole(cfg)
    assert m.shutdown_method == "ssh"  # derived
    assert m.disarmed is True
    assert m.effective_method == "none"
    assert _only_target(cfg, "cyberpower2").effective_enabled is False
    assert any("never" in n.message for n in m.load_notices)


def test_bl01_explicit_ssh_with_no_ups_degrades(tmp_path: Path) -> None:
    cfg = _bl01_config(tmp_path, {"name": "mt", "ssh": "mt", "shutdown_method": "ssh"})
    m = _sole(cfg)
    assert m.disarmed is True
    assert _only_target(cfg, "cyberpower2").effective_enabled is False


def test_push_machine_naming_an_unknown_ups_is_disarmed_once(tmp_path: Path) -> None:
    # The unresolvable half of the push-association rule. It is reported with the
    # specific reason (it can never be projected), and the generic unknown-UPS advisory
    # is suppressed so the operator gets one actionable notice about the ups rather than
    # two that disagree about severity.
    cfg = _one_machine(
        tmp_path,
        {"name": "mt", "ssh": "mt", "ups": "cyberpwoer", "shutdown_method": "ssh"},
    )
    m = _sole(cfg)
    assert m.disarmed is True
    assert m.effective_method == "none"
    ups_notices = [n for n in m.load_notices if "cyberpwoer" in n.message]
    assert len(ups_notices) == 1
    assert ups_notices[0].severity == "error"
    assert "matches no configured UPS" in ups_notices[0].message


def test_bl01_case_mismatched_ups_conflict_degrades(tmp_path: Path) -> None:
    # The reviewer's second live probe: machine mt with ups "CyberPower2" against an
    # enabled target mt on "cyberpower2". HI-03/IB-02.
    cfg = _bl01_config(
        tmp_path, {"name": "mt", "ssh": "mt", "ups": "CyberPower2", "shutdown_method": "ssh"}
    )
    m = _sole(cfg)
    assert m.disarmed is True
    assert _only_target(cfg, "cyberpower2").effective_enabled is False


# --- LO-13 duplicates: lossless on disk, fail-closed on shutdown ---


def test_duplicate_machine_names_keep_every_record_and_disarm_all(tmp_path: Path) -> None:
    # Dropping the duplicate mutated what _monitor_persist writes — it rewrites the
    # whole array from cfg.monitored_machines — so an unrelated `monitor remove <other>`
    # would have silently DELETED an operator-authored record, possibly the one carrying
    # the real device and baud. Keeping every record is lossless on disk; disarming all
    # of them is fail-closed, since firing "one of the two mt records" is a guess.
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {"name": "mt", "ssh": "mt-a", "ups": "cyberpower", "shutdown_method": "ssh"},
                {"name": "MT", "ssh": "mt-b", "ups": "cyberpower", "shutdown_method": "ssh"},
            ],
        },
    )
    cfg = Config.load(p, env={})
    assert len(cfg.monitored_machines) == 2
    assert all(m.disarmed for m in cfg.monitored_machines)
    assert all(
        any("duplicate" in n.message.lower() for n in m.load_notices)
        for m in cfg.monitored_machines
    )
    # A round-trip through to_dict still yields two records: no degrade deletes one.
    assert [m.to_dict()["ssh"] for m in cfg.monitored_machines] == ["mt-a", "mt-b"]


# --- IW-05: the hand-edit hole no CLI guard can close ---


def test_push_declaration_carrying_a_native_enrollment_ip_is_disarmed(tmp_path: Path) -> None:
    # `ip` is the nft saddr address resolved by _resolve_remote_ip and is written ONLY
    # by the native enrollment path. A push record carrying one is a probable
    # hand-edited former native secondary whose remote upsmon was never torn down: two
    # live authorities, one FSD self-halt plus one push. Config cannot know whether the
    # remote upsmon is armed, but it CAN recognise the fingerprint. Fail closed.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "spark",
                "ssh": "spark",
                "ups": "cyberpower",
                "shutdown_method": "ssh",
                "ip": "192.168.1.125",
            },
        )
    )
    assert m.disarmed is True
    (notice,) = [n for n in m.load_notices if n.severity == "error"]
    assert "monitor verify spark" in notice.message
    assert "monitor remove spark" in notice.message


def test_push_declaration_without_an_ip_is_not_disarmed(tmp_path: Path) -> None:
    m = _sole(
        _one_machine(
            tmp_path,
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
        )
    )
    assert m.disarmed is False
    assert m.effective_method == "ssh"


def test_push_declaration_carrying_only_the_LIST_form_ip_is_still_disarmed(
    tmp_path: Path,
) -> None:
    """KILLS: leaving the IW-05 check reading `ip` after 03-08 added `ips`.

    The check exists to catch a HAND EDIT, and a hand edit that moves the address
    into the new key while blanking the old one would walk straight through a check
    that only reads the old one — turning the fingerprint detector off for exactly
    the shape it was built to catch. Discriminating: the record carries `ips` and an
    EMPTY `ip`, so a first-field check sees nothing.
    """
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "spark",
                "ssh": "spark",
                "ups": "cyberpower",
                "shutdown_method": "ssh",
                "ip": "",
                "ips": ["192.168.1.121"],
            },
        )
    )
    assert m.disarmed is True


# --- 03-08: a machine record holds every address it can source from -----------


def test_legacy_single_address_config_round_trips_without_loss() -> None:
    """KILLS: dropping the back-compat `ip` emission on persist.

    `from_dict`/`to_dict` round-trip the LIVE config file on every `monitor add`
    and `monitor remove`, so a field only one direction knows about silently
    deletes operator data on an unrelated command. A release that drops `ip` makes
    a ROLLBACK to the previous release read an empty address — a firewall that
    stops granting a machine access, which is the shutdown-critical direction.
    Asserts BOTH keys survive TWO round trips, not one: a value that survives once
    can still be lost on the second persist.
    """
    from ups_orchestrator.config import MonitoredMachine

    once = MonitoredMachine.from_dict(
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "ip": "192.168.1.114"}
    ).to_dict()
    assert once["ip"] == "192.168.1.114"
    assert once["ips"] == ["192.168.1.114"]

    twice = MonitoredMachine.from_dict(once).to_dict()
    assert twice == once
    assert MonitoredMachine.from_dict(twice).addresses == ("192.168.1.114",)


def test_a_list_of_addresses_loads_in_order_and_deduplicated() -> None:
    from ups_orchestrator.config import MonitoredMachine

    m = MonitoredMachine.from_dict(
        {
            "name": "mt",
            "ips": ["192.168.1.114", " 192.168.1.133 ", "192.168.1.114", "", "192.168.1.133"],
        }
    )
    assert m.addresses == ("192.168.1.114", "192.168.1.133")
    # The scalar mirrors the FIRST entry, which is the route source.
    assert m.ip == "192.168.1.114"


def test_when_both_forms_are_present_the_list_wins_and_the_scalar_is_rewritten() -> None:
    """The documented precedence, pinned so it cannot drift into a silent union.

    Two sources of truth for the same fact must have one stated winner. The LIST
    wins because it is what the resolver writes and it is the only form that can
    express a multi-homed machine; the scalar is rewritten to its first entry so a
    reader of either key sees the same authoritative address.
    """
    from ups_orchestrator.config import MonitoredMachine

    m = MonitoredMachine.from_dict(
        {"name": "mt", "ip": "192.168.1.99", "ips": ["192.168.1.114", "192.168.1.133"]}
    )
    assert m.addresses == ("192.168.1.114", "192.168.1.133")
    assert m.ip == "192.168.1.114"
    assert m.to_dict()["ip"] == "192.168.1.114"


def test_a_blank_list_falls_back_to_the_scalar_rather_than_blanking_the_record() -> None:
    # A hand-edited `"ips": []` alongside a real `ip` must not silently revoke that
    # machine's firewall accept.
    from ups_orchestrator.config import MonitoredMachine

    m = MonitoredMachine.from_dict({"name": "mt", "ip": "192.168.1.114", "ips": []})
    assert m.addresses == ("192.168.1.114",)


def test_a_non_list_ips_value_is_ignored_rather_than_crashing_the_load() -> None:
    # The config file is hand-editable and Config.load runs on the OUTAGE path; a
    # TypeError here is a silent no-op that reports success (IW-06).
    from ups_orchestrator.config import MonitoredMachine

    for bogus in ("192.168.1.114", 7, {"a": 1}, None):
        m = MonitoredMachine.from_dict({"name": "mt", "ip": "192.168.1.114", "ips": bogus})
        assert m.addresses == ("192.168.1.114",)


# --- T-02-10: the ssh alias reaches an unattended argv ---


@pytest.mark.parametrize(
    "alias",
    [
        "-oProxyCommand=curl evil",  # option-shaped: ssh interprets it as an option
        "mt; rm -rf /",
        "mt host",
        "$(hostname)",
        "`hostname`",
        "mt|nc evil 1234",
    ],
)
def test_option_shaped_or_metacharacter_ssh_alias_disarms(tmp_path: Path, alias: str) -> None:
    # m.ssh flows into ssh_dest and then into an argv element. Before 02-02 that value
    # only ever reached a foreground operator command; after 02-02 it reaches an
    # UNATTENDED subprocess at outage time. The inconsistency was its own evidence:
    # --shutdown-cmd rejects a double-quote, the IP literals are validated and
    # machine.ups is guarded, while the alias alone got nothing.
    m = _sole(
        _one_machine(
            tmp_path,
            {"name": "mt", "ssh": alias, "ups": "cyberpower", "shutdown_method": "ssh"},
        )
    )
    assert m.disarmed is True
    assert any(repr(alias) in n.message for n in m.load_notices)


@pytest.mark.parametrize("alias", ["mt", "mt.lan", "MT-1", "root@mt.example.com", "mt:2222", "m_t"])
def test_plain_ssh_alias_is_accepted(tmp_path: Path, alias: str) -> None:
    m = _sole(
        _one_machine(
            tmp_path,
            {"name": "mt", "ssh": alias, "ups": "cyberpower", "shutdown_method": "ssh"},
        )
    )
    assert m.disarmed is False


# --- HI-05 corrected: native with a blank ups is REPORTED, never disarmed ---


def test_native_with_blank_ups_is_reported_not_disarmed(tmp_path: Path) -> None:
    m = _sole(
        _one_machine(tmp_path, {"name": "spark", "ssh": "spark", "shutdown_method": "native"})
    )
    assert m.disarmed is False
    assert m.effective_method == "native"
    (notice,) = m.load_notices
    assert notice.severity == "advisory"
    assert "monitor verify spark" in notice.message


# --- NEW-2, VALUE-based (round-4 blocker 2) ---


def test_new2_advises_a_push_record_at_the_default_shutdown_cmd(tmp_path: Path) -> None:
    # THE shape every persisted record actually has, and the one a key-presence detector
    # misses entirely: to_dict writes shutdown_cmd unconditionally, cli.py defaults
    # --shutdown-cmd method-independently, and _monitor_add writes it into every record
    # it builds. So every record ever persisted carries the key at exactly the
    # wrong-for-push value.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ssh": "mt",
                "ups": "cyberpower",
                "shutdown_method": "ssh",
                "shutdown_cmd": "/sbin/shutdown -h now",
            },
        )
    )
    (notice,) = m.load_notices
    assert notice.severity == "advisory"
    assert "shutdown_cmd" in notice.message
    # Advisory, never a disarm: config cannot know the far end's user, and a root
    # auto-login getty makes the default correct.
    assert m.disarmed is False
    assert m.effective_method == "ssh"


def test_new2_advises_a_push_record_with_the_key_absent(tmp_path: Path) -> None:
    # from_dict materialises the same default, so key presence carries no signal at all.
    m = _sole(
        _one_machine(
            tmp_path,
            {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
        )
    )
    assert [n.severity for n in m.load_notices] == ["advisory"]


def test_new2_silent_on_an_escalated_push_command(tmp_path: Path) -> None:
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ssh": "mt",
                "ups": "cyberpower",
                "shutdown_method": "ssh",
                "shutdown_cmd": "sudo /sbin/shutdown -h now",
            },
        )
    )
    assert m.load_notices == ()


def test_new2_silent_on_native_at_the_default(tmp_path: Path) -> None:
    # upsmon executes SHUTDOWNCMD as root, so the default is CORRECT for native.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "spark",
                "ssh": "spark",
                "ups": "cyberpower",
                "shutdown_method": "native",
                "shutdown_cmd": "/sbin/shutdown -h now",
            },
        )
    )
    assert m.load_notices == ()


# --- BL-02: routed to Config.degraded, not journal-only ---


def test_explicit_none_with_a_ups_is_an_advisory_in_degraded(tmp_path: Path) -> None:
    # RA-01's own argument is that a log line is an insufficient operator surface, so
    # the detector must obey it. Advisory, not a disarm: a "none" machine legitimately
    # carries a ups for inventory and projection scoping.
    cfg = _one_machine(
        tmp_path,
        {"name": "spark", "ssh": "spark", "ups": "cyberpower", "shutdown_method": "none"},
    )
    (notice,) = cfg.degraded
    assert notice.severity == "advisory"
    assert notice.subject == "spark"
    assert "monitor verify spark" in notice.message
    assert _sole(cfg).disarmed is False


# --- BLOCKER-1: the severity fold, end to end through Config.load ---


def test_advisory_only_push_machine_loads_still_armed(tmp_path: Path) -> None:
    # The guard round 3 was missing. An executor reaching for the only helper that
    # existed would have made every push machine carrying the NEW-2 advisory
    # effective_method="none": never projected, never fired, and reported
    # DISARMED (declared ssh) rc 1 — precisely the "operator believes protected, machine
    # does not shut down" outcome this phase exists to prevent, manufactured out of the
    # mechanism meant to prevent it.
    cfg = _one_machine(
        tmp_path,
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
    )
    m = _sole(cfg)
    assert m.load_notices and all(n.severity == "advisory" for n in m.load_notices)
    assert m.disarmed is False
    assert m.effective_method == m.shutdown_method == "ssh"
    assert cfg.degraded and all(n.severity == "advisory" for n in cfg.degraded)


def test_push_machine_with_an_advisory_and_an_error_is_disarmed_and_keeps_both(
    tmp_path: Path,
) -> None:
    # A push machine can legitimately earn BOTH the NEW-2 advisory and a T-02-10
    # ssh-alias error in one load. The tuple accumulates; the fold decides.
    m = _sole(
        _one_machine(
            tmp_path,
            {
                "name": "mt",
                "ssh": "-oProxyCommand=x",
                "ups": "cyberpower",
                "shutdown_method": "ssh",
            },
        )
    )
    assert {n.severity for n in m.load_notices} == {"advisory", "error"}
    assert m.disarmed is True
    assert m.effective_method == "none"


# --- the legacy-target disarm set, through Config.load ---


def test_unfireable_legacy_targets_are_disarmed(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {"name": "no-host", "kind": "remote", "enabled": True},
                        {"name": "odd-kind", "kind": "telepathy", "enabled": True, "host": "h"},
                        {"name": "no-device", "kind": "serial", "enabled": True, "baud": 9600},
                        {
                            "name": "bad-baud",
                            "kind": "serial",
                            "enabled": True,
                            "device": "/dev/ttyUSB0",
                            "baud": "fast",
                        },
                        {"name": "healthy", "kind": "remote", "enabled": True, "host": "h"},
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    ups = cfg.ups("cyberpower")
    assert ups is not None
    effective = {t.name: t.effective_enabled for t in ups.shutdown_targets}
    assert effective == {
        "no-host": False,
        "odd-kind": False,
        "no-device": False,
        "bad-baud": False,
        "healthy": True,
    }
    # Declarations are never rewritten.
    assert all(t.enabled for t in ups.shutdown_targets)
    assert {n.subject for n in cfg.degraded} >= {"cyberpower/no-host", "cyberpower/odd-kind"}


def test_option_shaped_legacy_host_is_disarmed_through_config_load(tmp_path: Path) -> None:
    # BL-01 end to end: the injected target must not survive the load ARMED. It fails
    # closed like every other transport error, and the declaration on disk is untouched.
    p = _write(
        tmp_path,
        {
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {
                            "name": "evil",
                            "kind": "remote",
                            "enabled": True,
                            "host": "-oProxyCommand=touch /tmp/pwn",
                        },
                        {"name": "healthy", "kind": "remote", "enabled": True, "host": "mt"},
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    ups = cfg.ups("cyberpower")
    assert ups is not None
    effective = {t.name: t.effective_enabled for t in ups.shutdown_targets}
    assert effective == {"evil": False, "healthy": True}
    assert all(t.enabled for t in ups.shutdown_targets)  # INV-DECLARED
    assert any(n.subject == "cyberpower/evil" and n.severity == "error" for n in cfg.degraded)


def test_advise_target_is_the_mirror_of_disarm_target() -> None:
    # The fourth helper. Two doors per kind, named for their severity, so picking the
    # wrong one is a visible, reviewable, testable choice rather than the silent
    # consequence of there being only one.
    from ups_orchestrator.config import _advise_target, _disarm_target

    t = ShutdownTarget(name="mt", kind="remote", enabled=True, host="mt")
    advised = _advise_target("cyberpower", t, "look at this")
    assert advised.enabled is True
    assert advised.effective_enabled is True
    assert advised.load_notices[0].severity == "advisory"
    assert advised.load_notices[0].subject == "cyberpower/mt"
    # ...and appending, never overwriting.
    both = _disarm_target("cyberpower", advised, "and this")
    assert [n.severity for n in both.load_notices] == ["advisory", "error"]
    assert both.effective_enabled is False


# --- the live production shape must stay clean ---


def test_live_production_shape_loads_with_an_empty_degraded(tmp_path: Path) -> None:
    # Ground truth from /etc/ups-orchestrator/config.json, read read-only on 2026-07-26:
    # ONE monitored machine (spark), no shutdown_method key, no serial fields,
    # backup {enabled:false, kind:remote}, empty shutdown_targets on all three UPSes,
    # and shutdown.enabled false. RA-01 ships as design, not incident response — this
    # asserts the real file is not degraded by any rule in this plan.
    p = _write(
        tmp_path,
        {
            "shutdown": {"enabled": False, "external": {"enabled": False}},
            "upses": {
                "cyberpower": {"label": "CP", "shutdown_targets": []},
                "cyberpower2": {"label": "CP2", "shutdown_targets": []},
                "cyberpower3": {"label": "CP3", "shutdown_targets": []},
            },
            "monitored_machines": [
                {
                    "name": "spark",
                    "ssh": "spark",
                    "ups": "cyberpower3",
                    "powervalue": 1,
                    "os": "auto",
                    "backup": {"enabled": False, "kind": "remote"},
                }
            ],
        },
    )
    cfg = Config.load(p, env={})
    m = _sole(cfg)
    assert m.shutdown_method == "native"
    assert m.disarmed is False
    assert cfg.degraded == ()


# --- the two property tests: one config carrying every degradable defect at once ---

EVERY_DEFECT: dict[str, object] = {
    "upses": {
        "cyberpower": {
            "label": "CP",
            "shutdown_targets": [
                # dual-regime partners
                {"name": "mt", "kind": "remote", "enabled": True, "host": "mt"},
                {"name": "spark", "kind": "remote", "enabled": True, "host": "spark"},
                # the legacy-target disarm set
                {"name": "no-host", "kind": "remote", "enabled": True},
                {"name": "odd-kind", "kind": "telepathy", "enabled": True, "host": "h"},
                {
                    "name": "bad-baud",
                    "kind": "serial",
                    "enabled": True,
                    "device": "/dev/ttyUSB0",
                    "baud": "fast",
                },
            ],
        },
        "cyberpower2": {"label": "CP2", "shutdown_targets": []},
    },
    "monitored_machines": [
        # dual-regime push conflict + NEW-2 advisory
        {
            "name": "mt",
            "ssh": "mt",
            "ups": "cyberpower",
            "shutdown_method": "ssh",
            "shutdown_cmd": "/sbin/shutdown -h now",
            "_comment": "the dell",
        },
        # dual-regime NATIVE conflict — must stay armed
        {
            "name": "spark",
            "ssh": "spark",
            "ups": "cyberpower",
            "shutdown_method": "native",
            "_comment": "live secondary",
        },
        # LO-13 duplicates: both kept, both disarmed
        {"name": "dup", "ssh": "dup-a", "ups": "cyberpower2", "shutdown_method": "ssh"},
        {"name": "DUP", "ssh": "dup-b", "ups": "cyberpower2", "shutdown_method": "ssh"},
        # unprojectable push (blank ups) + HI-04 unparseable baud + MED-10 device
        {
            "name": "unfireable",
            "shutdown_method": "serial",
            "serial_device": "/etc/passwd",
            "serial_baud": "fast",
            "_comment": "keep me",
        },
        # IW-05 stale enrollment fingerprint
        {
            "name": "handedited",
            "ssh": "handedited",
            "ups": "cyberpower2",
            "shutdown_method": "ssh",
            "ip": "192.168.1.200",
        },
        # T-02-10 option-shaped alias
        {
            "name": "injected",
            "ssh": "-oProxyCommand=x",
            "ups": "cyberpower2",
            "shutdown_method": "ssh",
        },
        # unknown UPS reference on a NATIVE machine — advisory, never a disarm
        {"name": "typo", "ssh": "typo", "ups": "cyberpwoer", "shutdown_method": "native"},
        # BL-02 explicit none with a ups — advisory
        {"name": "off", "ssh": "off", "ups": "cyberpower2", "shutdown_method": "none"},
        # HI-05 native with a blank ups — advisory
        {"name": "blanknative", "ssh": "blanknative", "shutdown_method": "native"},
    ],
}


def _every_defect(tmp_path: Path) -> Config:
    return Config.load(_write(tmp_path, EVERY_DEFECT), env={})


def test_inv_degrade_no_degrade_alters_what_a_persist_would_write(tmp_path: Path) -> None:
    # THE general property test. Round 1 found one instance of "an in-memory degrade
    # leaks to disk", guarded that instance, and treated the class as closed; it occurs
    # three times. This asserts the structural rule instead of the three patches, so the
    # class cannot recur — including for degrades nobody has thought of yet.
    #
    # (The one deliberate to_dict SHAPE change, LO-14's nested serial block, is asserted
    # separately by test_to_dict_drops_nested_serial_block and is not a degrade.)
    cfg = _every_defect(tmp_path)
    assert cfg.degraded
    authored = EVERY_DEFECT["monitored_machines"]
    assert isinstance(authored, list)
    # No record is dropped and the order is preserved: _monitor_persist rewrites the
    # whole array from cfg.monitored_machines, so a drop here is a DELETION on disk.
    assert len(cfg.monitored_machines) == len(authored)
    for record, machine in zip(authored, cfg.monitored_machines, strict=True):
        out = machine.to_dict()
        for key, value in record.items():
            assert out[key] == value, f"{machine.name}.{key} was rewritten by a degrade"


def test_inv_severity_no_advisory_ever_disarms_and_no_native_is_ever_disarmed(
    tmp_path: Path,
) -> None:
    # The general guard, so a future site that attaches an advisory through the wrong
    # helper fails HERE rather than in production.
    cfg = _every_defect(tmp_path)
    for m in cfg.monitored_machines:
        if m.shutdown_method == "native":
            assert m.disarmed is False, m.name
            assert m.effective_method == "native", m.name
        if m.load_notices and all(n.severity == "advisory" for n in m.load_notices):
            assert m.disarmed is False, m.name
            assert m.effective_method == m.shutdown_method, m.name
        if any(n.severity == "error" for n in m.load_notices) and m.shutdown_method in (
            "serial",
            "ssh",
        ):
            assert m.effective_method == "none", m.name
    for ups in cfg.upses.values():
        for t in ups.shutdown_targets:
            if t.load_notices and all(n.severity == "advisory" for n in t.load_notices):
                assert t.effective_enabled == t.enabled, t.name


def test_every_defect_config_still_loads_and_reports_each_class(tmp_path: Path) -> None:
    cfg = _every_defect(tmp_path)
    by_name = {m.name: m for m in cfg.monitored_machines}
    assert by_name["mt"].effective_method == "none"  # dual-regime push
    assert by_name["spark"].effective_method == "native"  # native survives, armed
    assert by_name["unfireable"].effective_method == "none"  # unprojectable + transport
    assert by_name["handedited"].effective_method == "none"  # IW-05
    assert by_name["injected"].effective_method == "none"  # T-02-10
    assert by_name["typo"].effective_method == "native"  # advisory only
    assert by_name["off"].effective_method == "none"  # declared off
    assert by_name["blanknative"].effective_method == "native"  # HI-05 notice-only
    assert by_name["off"].disarmed is False  # advisory did not disarm it
    # Every subject is named on the machine-readable surface monitor list / status read.
    subjects = {n.subject for n in cfg.degraded}
    assert {"mt", "spark", "dup", "DUP", "unfireable", "handedited", "injected"} <= subjects
    assert {"cyberpower/no-host", "cyberpower/odd-kind", "cyberpower/bad-baud"} <= subjects


# --- MED-04 / LO-05 / LO-06: the notice surface stays proportionate ------------


def _dup_config(tmp_path: Path) -> Path:
    """Two records named `mt` plus one enabled legacy target that collides with them."""
    return _write(
        tmp_path,
        {
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {"name": "mt", "kind": "remote", "enabled": True, "host": "mt"}
                    ],
                }
            },
            "monitored_machines": [
                {"name": "mt", "ups": "cyberpower", "ssh": "mt", "shutdown_method": "ssh"},
                {"name": "mt", "ups": "cyberpower", "ssh": "mt", "shutdown_method": "ssh"},
            ],
        },
    )


def test_duplicate_names_do_not_fan_out_the_notice_surface(tmp_path: Path) -> None:
    # MED-04. dual_regime_pairs yields one tuple PER RECORD and _apply_degrades applied
    # each to EVERY index sharing the name, so the product was n^2: two `mt` records
    # produced four identical dual-regime notices, five would produce twenty-five. That
    # tuple is not log-only — it feeds status.render, the web banner, monitor list and
    # a Discord embed, i.e. the operator's safety surface.
    cfg = Config.load(_dup_config(tmp_path), env={})

    messages = [n.message for n in cfg.degraded]
    assert len(messages) == len(set(messages)), messages
    dual = [m for m in messages if "governed by BOTH shutdown regimes" in m]
    assert len(dual) == 1
    # ...and every record still carries the finding, so nothing was weakened.
    for m in cfg.monitored_machines:
        assert m.disarmed is True
        assert sum("governed by BOTH" in n.message for n in m.load_notices) == 1


def test_a_targets_second_distinct_disarm_reason_is_recorded(tmp_path: Path) -> None:
    # LO-06: "first reason wins" is defensible; dropping the second WITHOUT A TRACE is
    # not. A target disarmed for the dual-regime collision never surfaced its blank-host
    # finding, so fixing the collision revealed a second failure the operator was never
    # told about.
    p = _write(
        tmp_path,
        {
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {"name": "mt", "kind": "remote", "enabled": True, "host": ""}
                    ],
                }
            },
            "monitored_machines": [
                {"name": "mt", "ups": "cyberpower", "ssh": "mt", "shutdown_method": "ssh"}
            ],
        },
    )
    cfg = Config.load(p, env={})
    ups = cfg.ups("cyberpower")
    assert ups is not None
    (target,) = ups.shutdown_targets
    reasons = [n.message for n in target.load_notices]
    assert any("collides with monitored machine" in r for r in reasons)
    assert any("blank host" in r for r in reasons)
    assert target.effective_enabled is False


def test_disarm_target_does_not_raise_on_a_ups_whose_name_differs_from_its_key() -> None:
    # LO-05: ups_key comes from dual_regime_pairs (ups.name) while target_lists is keyed
    # by the upses dict key. Config.load keeps them equal, but RA-01's whole premise is
    # that nothing on this path may raise.
    from ups_orchestrator.config import _apply_degrades

    target = ShutdownTarget(name="mt", kind="remote", enabled=True, host="mt")
    ups = UpsConfig(name="a-different-name", label="CP", shutdown_targets=(target,))
    machines = _machines({"name": "mt", "ups": "", "ssh": "mt", "shutdown_method": "ssh"})

    _machines_out, _upses_out, notices = _apply_degrades(machines, {"cyberpower": ups})

    assert notices  # it reported rather than raising


def test_to_dict_preserves_operator_sub_keys_under_backup_and_serial() -> None:
    # MED-07. The raw round-trip is documented as preserving operator-authored keys and
    # did so at the TOP level only: BackupShutdown.to_dict emitted exactly
    # {enabled, kind}, and merged.pop("serial") took the nested block whole. An
    # unrelated `monitor add`/`monitor remove` therefore silently edited the operator's
    # file. Same class as HI-01, without the safety consequence.
    record: dict[str, object] = {
        "name": "mt",
        "ups": "cyberpower",
        "shutdown_method": "serial",
        "_comment": "top level survives",
        "backup": {"enabled": False, "kind": "remote", "_tag": "x"},
        "serial": {"device": "/dev/ttyUSB0", "baud": 9600, "parity": "8N1", "_note": "keep me"},
    }

    out = MonitoredMachine.from_dict(record).to_dict()

    assert out["_comment"] == "top level survives"
    assert out["backup"] == {"enabled": False, "kind": "remote", "_tag": "x"}
    # The flat fields stay the single authority for device/baud (LO-14)...
    assert out["serial_device"] == "/dev/ttyUSB0"
    assert out["serial_baud"] == 9600
    # ...and the residue the flat lift cannot express is not destroyed.
    assert out["serial"] == {"parity": "8N1", "_note": "keep me"}
    # It is stable across a second persist, not merely preserved once.
    assert MonitoredMachine.from_dict(out).to_dict() == out


def test_to_dict_emits_no_serial_block_when_the_nested_form_had_nothing_extra() -> None:
    # LO-14 unchanged for the ordinary shape: no residue, no stale block.
    m = MonitoredMachine.from_dict(
        {"name": "mt", "serial": {"device": "/dev/ttyUSB0", "baud": 9600}}
    )
    assert "serial" not in m.to_dict()


def test_backup_raw_does_not_change_machine_equality() -> None:
    # `raw` is provenance, not identity — compare=False, like MonitoredMachine.raw.
    plain = BackupShutdown.from_dict({"enabled": True, "kind": "serial"})
    tagged = BackupShutdown.from_dict({"enabled": True, "kind": "serial", "_tag": "x"})
    assert plain == tagged


# --- LO-04: an internal threshold the local target cannot actually reach -------


def _threshold_config(tmp_path: Path, internal: int, external: int) -> Path:
    return _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "shutdown": {
                "enabled": True,
                "external": {"enabled": True, "battery_below": external},
                "internal": {"enabled": True, "battery_below": internal},
            },
        },
    )


def test_an_unreachable_internal_threshold_is_reported(tmp_path: Path) -> None:
    # LO-04. Local targets are held until every enabled remote has been sent, so an
    # internal threshold that trips EARLIER than the external one is not the promise it
    # reads as: the watcher host runs past its own declared threshold with (before
    # LO-03) no explanation anywhere.
    cfg = Config.load(_threshold_config(tmp_path, internal=10, external=5), env={})

    (notice,) = [n for n in cfg.degraded if "internal" in n.message]
    assert notice.severity == "advisory"  # a policy shape, never a disarm
    assert notice.subject == "cyberpower"
    assert "10%" in notice.message and "5%" in notice.message


def test_an_ordinary_threshold_ordering_is_not_reported(tmp_path: Path) -> None:
    assert Config.load(_threshold_config(tmp_path, internal=5, external=10), env={}).degraded == ()
    assert Config.load(_threshold_config(tmp_path, internal=10, external=10), env={}).degraded == ()


def test_threshold_ordering_is_not_reported_when_a_group_is_disabled(tmp_path: Path) -> None:
    # With one group off there is no hold and nothing to explain.
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "shutdown": {
                "enabled": True,
                "external": {"enabled": False, "battery_below": 5},
                "internal": {"enabled": True, "battery_below": 10},
            },
        },
    )
    assert Config.load(p, env={}).degraded == ()


# --- F4: a non-finite number degrades; it does not kill the daemon -------------
#
# `json.loads` accepts the bare tokens Infinity/-Infinity/NaN and maps any
# overflowing literal (1e400) to float('inf'). `int(inf)` raises OverflowError —
# an ArithmeticError, NOT a ValueError — so it escaped both the coercion helpers
# and `_load_config`'s `(OSError, ValueError)`. RA-01's rule is that only the seven
# structural monitoring-topology classes may be fatal; `poll_seconds` is a
# monitoring knob, not a shutdown authority.


@pytest.mark.parametrize("literal", ["Infinity", "-Infinity", "NaN", "1e400", "-1e400"])
def test_non_finite_poll_seconds_falls_back_instead_of_raising(tmp_path: Path, literal) -> None:
    p = tmp_path / "config.json"
    p.write_text(f'{{"poll_seconds": {literal}, "upses": {{"u1": {{"label": "U1"}}}}}}')

    cfg = Config.load(p, env={})  # must not raise

    assert cfg.poll_seconds == 30  # the documented default
    assert cfg.ups("u1") is not None  # ...and monitoring is unaffected


@pytest.mark.parametrize("literal", ["Infinity", "NaN", "1e400"])
def test_non_finite_optional_int_falls_back_to_none(tmp_path: Path, literal) -> None:
    """`_opt_int` has the same float branch — `serial_baud` is one of its callers."""
    p = tmp_path / "config.json"
    p.write_text(
        '{"upses": {"u1": {"label": "U1"}}, "monitored_machines": ['
        '{"name": "spark", "ups": "u1", "shutdown_method": "serial",'
        ' "serial_device": "/dev/ttyUSB0", "serial_baud": ' + literal + "}]}"
    )

    cfg = Config.load(p, env={})  # must not raise

    machine = cfg.monitored_machines[0]
    assert machine.serial_baud is None
    # ...and an unusable baud still disarms, which is the pre-existing contract.
    assert machine.disarmed


def test_non_finite_numbers_degrade_across_every_int_field(tmp_path: Path) -> None:
    """Six fields reach `_as_int`; none of them may be fatal."""
    p = tmp_path / "config.json"
    p.write_text(
        '{"poll_seconds": Infinity, "countdown_every_seconds": 1e400,'
        ' "onbatt_notify_grace_seconds": -Infinity,'
        ' "upses": {"u1": {"label": "U1"}},'
        ' "shutdown": {"min_on_battery_seconds": NaN,'
        ' "external": {"battery_below": Infinity, "runtime_below": 1e400}}}'
    )

    cfg = Config.load(p, env={})  # must not raise

    assert cfg.poll_seconds == 30
    assert cfg.countdown_every_seconds == 60
    assert cfg.onbatt_notify_grace_seconds == 20
    # Each falls back to that field's OWN default, not to a shared zero.
    assert cfg.shutdown_policy.min_on_battery_seconds == 120
    assert cfg.shutdown_policy.external.battery_below is None
    assert cfg.shutdown_policy.external.runtime_below is None


# --- INV-DECLARED, on an ALREADY-DISARMED machine ----------------------------
#
# `_apply_degrades` runs its steps in order and each disarm mutates the working
# record in place, so every step after the first sees machines whose
# `effective_method` has already collapsed to "none". A detector that read the
# EFFECT instead of the DECLARATION would therefore go silent on exactly the
# records that most need it — and the existing INV-DECLARED tests all use a machine
# that is still armed at the point the detector runs, where declaration and effect
# are indistinguishable. These three pin the discriminating shape.


def test_hand_edited_ip_is_still_reported_on_an_already_disarmed_push_machine(
    tmp_path: Path,
) -> None:
    # Step 3 (unprojectable ups) disarms this record; step 4 (the IW-05 hand-edit
    # hole) must still see shutdown_method='ssh' and report the stale nft address.
    # Reading effective_method there yields "none", which is not in _PUSH_METHODS,
    # and the likeliest two-live-authorities config in the codebase reports nothing.
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {
                    "name": "ghost",
                    "ssh": "ghost",
                    "ups": "nosuchups",
                    "shutdown_method": "ssh",
                    "ip": "10.0.0.5",
                }
            ],
        },
    )

    (machine,) = Config.load(p, env={}).monitored_machines

    assert machine.disarmed  # already disarmed by the time step 4 runs
    assert machine.effective_method == "none"
    ip_notices = [n for n in machine.load_notices if "carries a non-empty ip" in n.message]
    assert len(ip_notices) == 1
    assert ip_notices[0].severity == "error"
    assert "shutdown_method='ssh'" in ip_notices[0].message
    assert "'10.0.0.5'" in ip_notices[0].message


def test_push_shutdown_cmd_advisory_survives_an_earlier_disarm(tmp_path: Path) -> None:
    # Step 7's NEW-2 advisory is keyed on the DECLARED method too. It is the operator's
    # only warning that a serial push will report rc 0 for a box that stayed up, and it
    # must not vanish because an unrelated step already disarmed the record.
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {
                    "name": "r630",
                    "ups": "cyberpower",
                    "shutdown_method": "serial",
                    "serial_device": "/dev/ttyUSB0",
                    "serial_baud": 9600,
                    "ip": "10.0.0.9",
                }
            ],
        },
    )

    (machine,) = Config.load(p, env={}).monitored_machines

    assert machine.disarmed and machine.effective_method == "none"
    advisories = [n for n in machine.load_notices if n.severity == "advisory"]
    assert len(advisories) == 1
    assert "shutdown_method='serial'" in advisories[0].message
    assert "root-only halt binary" in advisories[0].message


def test_dual_regime_message_names_the_declared_method_after_a_duplicate_disarm(
    tmp_path: Path,
) -> None:
    # Step 1 (duplicate name) disarms both records; step 2 then builds the collision
    # message. Reading the effect there would print "shutdown_method='none'" on both
    # the machine notice and the legacy target's disarm reason, telling the operator
    # the machine declares nothing — the remedy sentence ("set this machine's
    # shutdown_method to 'none'") then reads as already done.
    p = _write(
        tmp_path,
        {
            "upses": {
                "cyberpower": {
                    "label": "CP",
                    "shutdown_targets": [
                        {
                            "name": "mt",
                            "kind": "remote",
                            "enabled": True,
                            "host": "mt",
                            "cmd": "poweroff",
                        }
                    ],
                }
            },
            "monitored_machines": [
                {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
                {"name": "mt", "ssh": "mt2", "ups": "cyberpower", "shutdown_method": "ssh"},
            ],
        },
    )

    cfg = Config.load(p, env={})

    for machine in cfg.monitored_machines:
        assert machine.effective_method == "none"  # already collapsed by step 1
        (dual,) = [n for n in machine.load_notices if "governed by BOTH" in n.message]
        assert "declared shutdown_method='ssh'" in dual.message
        assert "shutdown_method='none'" not in dual.message
    (target,) = cfg.upses["cyberpower"].shutdown_targets
    (collision,) = [n for n in target.load_notices if "collides with" in n.message]
    assert "shutdown_method='ssh'" in collision.message


def test_watchdog_is_not_a_serial_device_path() -> None:
    """/dev/watchdog passed the old `/dev/` + S_ISCHR guard, and must not pass now.

    It exists on this host as a character device under /dev/. The probe opens the
    device O_RDWR and closes it, which ARMS the hardware watchdog — on a nowayout
    kernel that reboots the box about a minute later. This host is the NUT primary:
    the machine whose entire job is to survive the outage and bring the others back.
    A config typo must not let `monitor verify --deep` reboot it.
    """
    for hazard in ("/dev/watchdog", "/dev/watchdog0", "/dev/mem", "/dev/sda", "/dev/kmsg"):
        assert is_serial_device_path(hazard) is False, hazard


def test_real_console_paths_are_still_accepted() -> None:
    """The allowlist must not be so narrow it rejects the deployment's own devices."""
    for good in (
        "/dev/ttyUSB0",
        "/dev/ttyS1",
        "/dev/ttyAMA0",
        "/dev/ttyACM0",
        "/dev/pts/7",  # every probe test drives a pty; also valid for socat/ser2net
        "/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0",
        "/dev/serial/by-path/platform-xhci-hcd.0-usb-0:2:1.0-port0",
    ):
        assert is_serial_device_path(good) is True, good


def test_a_bare_prefix_is_not_a_device() -> None:
    """`/dev/tty` is the controlling terminal and `/dev/serial/by-id/` is a directory."""
    for bare in ("/dev/tty", "/dev/pts/", "/dev/serial/by-id/", "/dev/serial/by-path/", "/dev/"):
        assert is_serial_device_path(bare) is False, bare
