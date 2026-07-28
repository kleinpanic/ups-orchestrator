"""`python -m ups_orchestrator` must exit with the code main() returned."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

SCRATCH_CONFIG = """\
{"poll_seconds":30,"discord_webhook_url":"","shutdown":{"enabled":false},
 "upses":{"cyberpower":{"label":"CPS 1500"}},
 "monitored_machines":[{"name":"mt","ups":"cyberpower","shutdown_method":"serial",
   "serial_device":"/dev/serial/by-id/fake","serial_baud":"fast"}]}
"""


def _run_module(args: list[str], config: Path) -> int:
    return subprocess.run(
        [sys.executable, "-m", "ups_orchestrator", *args],
        cwd=REPO,
        env={
            "PATH": "/usr/bin:/bin",
            "UPS_ORCH_CONFIG": str(config),
            "PYTHONPATH": str(REPO / "src"),
        },
        capture_output=True,
        text=True,
    ).returncode


@pytest.mark.allow_subprocess  # the whole point is the real process's exit status
def test_module_entrypoint_propagates_a_failing_exit_code(tmp_path: Path) -> None:
    # `main()` returns the exit code and __main__ used to drop it, so this entry
    # point always exited 0 while the console script exited correctly. A disarmed
    # machine printed DISARMED and exited 0 — anything keying on the code read the
    # opposite of the line it had just printed. The phase's own UAT aliases `uo`
    # to this form, so every rc assertion in it was vacuous.
    config = tmp_path / "config.json"
    config.write_text(SCRATCH_CONFIG)
    assert _run_module(["monitor", "verify", "mt"], config) == 1


@pytest.mark.allow_subprocess
def test_module_entrypoint_still_reports_success_as_zero(tmp_path: Path) -> None:
    config = tmp_path / "config.json"
    config.write_text(SCRATCH_CONFIG)
    assert _run_module(["monitor", "list"], config) == 0
