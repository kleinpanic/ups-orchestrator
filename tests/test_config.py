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
    canonical_ups_index,
    canonical_ups_key,
    derive_shutdown_method,
    dual_regime_conflicts,
    dual_regime_pairs,
    legacy_only_targets,
    requires_root_escalation,
    unknown_ups_references,
    unprojectable_push_machines,
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
                "not-a-dict",
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


def test_dual_regime_conflict_detected(tmp_path: Path) -> None:
    # P2-06: mutual exclusion is enforced, not warned. "mt" has a ups and no
    # explicit method, so it derives "native"; an enabled shutdown_target with the
    # same name on that UPS is the classic double-shutdown and is now a load error.
    p = _write(
        tmp_path,
        {
            "monitored_machines": [{"name": "mt", "ssh": "mt", "ups": "u1"}],
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [{"name": "mt", "enabled": True}],
                }
            },
        },
    )
    with pytest.raises(ValueError, match="BOTH shutdown regimes"):
        Config.load(p, env={})


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


def test_legacy_serial_backup_without_serial_fields_rejected() -> None:
    # A legacy backup {enabled:true, kind:serial} has no serial_device/baud to project;
    # it must raise a clear migration error rather than derive an unfireable serial.
    with pytest.raises(ValueError, match="serial"):
        MonitoredMachine.from_dict(
            {"name": "mt", "backup": {"enabled": True, "kind": "serial"}}
        )


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


def test_transport_valid_serial_empty_device_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {
                    "name": "mt",
                    "ups": "cyberpower",
                    "shutdown_method": "serial",
                    "serial_baud": 9600,
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="serial"):
        Config.load(p, env={})


def test_transport_valid_serial_nonpositive_baud_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {
                    "name": "mt",
                    "ups": "cyberpower",
                    "shutdown_method": "serial",
                    "serial_device": "/dev/ttyUSB0",
                    "serial_baud": 0,
                }
            ],
        },
    )
    with pytest.raises(ValueError, match="baud"):
        Config.load(p, env={})


def test_transport_valid_ssh_empty_alias_rejected(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {"cyberpower": {"label": "CP"}},
            "monitored_machines": [
                {"name": "mt", "ups": "cyberpower", "shutdown_method": "ssh", "ssh": ""}
            ],
        },
    )
    with pytest.raises(ValueError, match="ssh"):
        Config.load(p, env={})


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


def test_mutual_exclusion_native_legacy_target_raises(tmp_path: Path) -> None:
    # native + enabled legacy target: the secondary fires below LB and the target
    # fires on the external-group thresholds — the live double-shutdown (events.py).
    p = _mutual_exclusion_config(
        tmp_path,
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "native"},
    )
    with pytest.raises(ValueError, match="shutdown_method=native"):
        Config.load(p, env={})


def test_mutual_exclusion_none_legacy_target_raises(tmp_path: Path) -> None:
    # none + enabled legacy target: the machine is declared OFF yet still gets shut
    # down by the legacy regime. Fail closed rather than honour the stale target.
    p = _mutual_exclusion_config(
        tmp_path,
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "none"},
    )
    with pytest.raises(ValueError, match="shutdown_method=none"):
        Config.load(p, env={})


def test_mutual_exclusion_serial_legacy_target_raises(tmp_path: Path) -> None:
    p = _mutual_exclusion_config(
        tmp_path,
        {
            "name": "mt",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
        },
    )
    with pytest.raises(ValueError, match="shutdown_method=serial"):
        Config.load(p, env={})


def test_mutual_exclusion_ssh_legacy_target_raises(tmp_path: Path) -> None:
    p = _mutual_exclusion_config(
        tmp_path,
        {"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"},
    )
    with pytest.raises(ValueError, match="shutdown_method=ssh"):
        Config.load(p, env={})


def test_mutual_exclusion_ignores_target_on_a_different_ups(tmp_path: Path) -> None:
    # Same target name, different UPS — not the same power domain, not a conflict.
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


def test_pure_legacy_target_warns_only_and_still_loads(caplog, tmp_path: Path) -> None:
    # P2-07: a shutdown_target with NO monitored_machines entry has no effective
    # method to key on, so it stays warn-only and keeps loading.
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "U1",
                    "shutdown_targets": [
                        {"name": "fileserver", "enabled": True, "host": "fs.lan"}
                    ],
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
            _machines(
                {"name": "mt", "ssh": "mt", "ups": declared, "shutdown_method": "ssh"}
            ),
            _upses(cyberpower2=_enabled_mt_target()),
        )
        assert pairs == (("mt", "cyberpower2", "mt"),), declared

    # ...and the same when the AUTHORED key is the odd one out.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower2", "shutdown_method": "ssh"}),
        _upses(CyberPower2=_enabled_mt_target()),
    )
    assert pairs == (("mt", "CyberPower2", "mt"),)


def test_dual_regime_pairs_ignores_a_target_on_a_different_ups() -> None:
    # The machine's ups RESOLVES, so the scan is scoped to that UPS. Same target name on
    # another UPS is a different power domain, not a conflict.
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}),
        _upses(cyberpower={"label": "CP"}, other=_enabled_mt_target()),
    )
    assert pairs == ()


def test_dual_regime_pairs_ignores_a_disabled_target() -> None:
    pairs = dual_regime_pairs(
        _machines({"name": "mt", "ssh": "mt", "ups": "cyberpower", "shutdown_method": "ssh"}),
        _upses(
            cyberpower={"shutdown_targets": [{"name": "mt", "enabled": False, "host": "mt"}]}
        ),
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
