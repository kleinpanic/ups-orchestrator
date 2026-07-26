"""INV-DECLARED at the rehearsal seam.

Deliberately its own module rather than an addition to ``tests/test_cli.py``: this
covers one seam only, and the CLI test module is under concurrent edit.

``_rehearsal_target`` documents that its machine branch reads the DECLARED method
"on purpose" — refusing to rehearse a machine a load degrade disarmed would withhold
the cable diagnostic at exactly the moment an operator is trying to work out what is
wrong with it. Nothing tested that, so switching the read to ``effective_method``
made ``shutdown rehearse`` silently unavailable for every degraded machine and the
suite stayed green.
"""

from __future__ import annotations

import json
from pathlib import Path

from ups_orchestrator.cli import _REHEARSAL_CMD, _rehearsal_target
from ups_orchestrator.config import Config


def _load(tmp_path: Path, machine: dict[str, object]) -> Config:
    p = tmp_path / "config.json"
    p.write_text(
        json.dumps(
            {
                "upses": {"cyberpower": {"label": "CP"}},
                "monitored_machines": [machine],
            }
        )
    )
    return Config.load(p, env={})


def test_a_disarmed_serial_machine_is_still_rehearsable(tmp_path: Path) -> None:
    # The stale `ip` disarms this record (IW-05), so effective_method is "none" — the
    # branch that returns None. The declared method is still 'serial'.
    cfg = _load(
        tmp_path,
        {
            "name": "r630",
            "ups": "cyberpower",
            "shutdown_method": "serial",
            "serial_device": "/dev/ttyUSB0",
            "serial_baud": 9600,
            "ip": "10.0.0.9",
        },
    )
    (machine,) = cfg.monitored_machines
    assert machine.disarmed and machine.effective_method == "none"

    target = _rehearsal_target(cfg, "r630")

    assert target is not None
    assert target.is_serial
    assert (target.device, target.baud) == ("/dev/ttyUSB0", 9600)
    assert target.cmd == _REHEARSAL_CMD  # never the persisted shutdown_cmd


def test_a_disarmed_ssh_machine_is_still_rehearsable(tmp_path: Path) -> None:
    cfg = _load(
        tmp_path,
        {
            "name": "ghost",
            "ssh": "ghost",
            "ups": "nosuchups",  # unprojectable => disarmed
            "shutdown_method": "ssh",
        },
    )
    (machine,) = cfg.monitored_machines
    assert machine.disarmed and machine.effective_method == "none"

    target = _rehearsal_target(cfg, "ghost")

    assert target is not None
    assert target.kind == "remote"
    assert target.host == "ghost"
    assert target.cmd == _REHEARSAL_CMD


def test_a_native_machine_has_no_push_transport_to_rehearse(tmp_path: Path) -> None:
    # The carve-out the two tests above must not widen into: native is armed and
    # declared, and still has nothing to rehearse.
    cfg = _load(
        tmp_path,
        {"name": "spark", "ssh": "spark", "ups": "cyberpower", "shutdown_method": "native"},
    )

    assert _rehearsal_target(cfg, "spark") is None
