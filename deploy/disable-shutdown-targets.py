"""Clear every orchestrator-managed shutdown target from a live config file.

Invoked by ``deploy/disable-live-shutdown-targets.sh``, as root, against
``/etc/ups-orchestrator/config.json``. It lives in its own file rather than in a
shell heredoc so that it can import the project's shared writer — and so the suite
can drive it without a subprocess.

Deliberately carries NO shebang: it must run under the install venv's interpreter
(the only one that can import ``ups_orchestrator``), which is what the shell wrapper
resolves. Running it with the system ``python3`` fails at the import rather than
falling back to an unprotected write.

IF-02: the heredoc this replaces ended in a bare ``tmp.replace(path)``, verbatim
the pattern ``state.replace_preserving_metadata`` was written to eliminate. The
destination inode does not survive a bare rename, so neither does its mode, owner
or ACL: running the shipped "turn shutdown off" script silently took
``/etc/ups-orchestrator/config.json`` from the installer's ``0640 root:nut`` plus
``u:<user>:r`` to the root umask default. That file holds a Discord webhook URL, so
the regression was a world-readable secret with no warning and nothing visibly
broken.

Deliberately does NOT touch ``monitored_machines``. A declared ``native`` machine's
authority lives in that box's own ``/etc``, and no config change here disarms it
(INV-DECLARED); ``ups-orchestrator monitor remove <name>`` is the only real disarm.
The caller says so on stdout rather than leaving the operator to infer it.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from ups_orchestrator.state import write_json_preserving_metadata

_DISABLED_POLICY: dict[str, object] = {
    "enabled": False,
    "require_power_outage": True,
    "min_on_battery_seconds": 120,
    "notify": True,
    "external": {"enabled": False, "battery_below": 15, "runtime_below": 300},
    "internal": {"enabled": False, "battery_below": 10, "runtime_below": 120},
}


def disable_shutdown_targets(path: Path) -> int:
    """Disable the shutdown policy and empty every UPS's legacy targets.

    Returns the number of legacy targets removed, so the caller can report it.
    """
    data = json.loads(path.read_text())
    data["shutdown"] = dict(_DISABLED_POLICY)
    removed = 0
    for ups in data.get("upses", {}).values():
        if not isinstance(ups, dict):
            continue
        existing = ups.get("shutdown_targets")
        removed += len(existing) if isinstance(existing, list) else 0
        ups["shutdown_targets"] = []
        ups.pop("shutdown_scope", None)
    # The shared writer: temp + fsync + metadata-preserving rename. Never a bare
    # Path.replace on a file the installer gave a mode, an owner and an ACL.
    write_json_preserving_metadata(path, data, indent=2)
    return removed


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(f"usage: {Path(__file__).name} <config.json>", file=sys.stderr)
        return 2
    path = Path(argv[0])
    removed = disable_shutdown_targets(path)
    print(f"shutdown policy disabled in {path}; {removed} legacy shutdown_target(s) removed")
    print(
        "NOTE: monitored_machines are untouched. A declared 'native' machine's NUT "
        "secondary lives in that box's own /etc and nothing here disarms it — run "
        "'ups-orchestrator monitor remove <name>' for that."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
