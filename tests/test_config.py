from __future__ import annotations

import json
from pathlib import Path

import pytest

from ups_orchestrator.config import Config, dual_regime_conflicts


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


def test_dual_regime_conflict_detected(caplog, tmp_path: Path) -> None:
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
    with caplog.at_level("WARNING"):
        cfg = Config.load(p, env={})
    assert dual_regime_conflicts(cfg.monitored_machines, cfg.upses) == ("mt",)
    assert any("mt" in rec.message for rec in caplog.records)


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


def test_malformed_json_config_loads_as_none(monkeypatch, tmp_path: Path) -> None:
    from ups_orchestrator import cli

    bad = tmp_path / "config.json"
    bad.write_text("{ not valid json ")
    monkeypatch.setenv("UPS_ORCH_CONFIG", str(bad))
    assert cli._load_config() is None  # graceful, not a crash
