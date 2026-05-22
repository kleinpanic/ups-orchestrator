from __future__ import annotations

import json
from pathlib import Path

import pytest

from ups_orchestrator.config import Config


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


def test_per_ups_defaults_and_targets(tmp_path: Path) -> None:
    p = _write(
        tmp_path,
        {
            "upses": {
                "u1": {
                    "label": "Rack",
                    "shutdown_pi_on_lowbatt": True,
                    "shutdown_targets": [
                        {
                            "name": "srv",
                            "enabled": True,
                            "host": "h",
                            "user": "u",
                            "delay_seconds": 120,
                        },
                        {"name": "nas", "host": "n", "user": "u"},
                    ],
                }
            }
        },
    )
    cfg = Config.load(p, env={})
    u1 = cfg.ups("u1")
    assert u1 is not None
    assert u1.shutdown_pi_on_lowbatt is True
    assert len(u1.shutdown_targets) == 2
    first = u1.shutdown_targets[0]
    assert first.name == "srv"
    assert first.enabled is True
    assert first.delay_seconds == 120
    assert u1.shutdown_targets[1].enabled is False  # default
    assert cfg.ups("missing") is None
