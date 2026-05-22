"""Command-line entry point invoked by NUT's ``upssched`` (and a systemd timer).

Usage::

    ups-orchestrator <event> [ups_name]

``event`` is one of: onbatt, online, lowbatt, commbad, commok, tick,
shutdown_r630. ``ups_name`` is the NUT device name; NUT exposes it to the
dispatcher as the ``UPSNAME`` environment variable. For the periodic ``tick``
event the UPS name may be omitted, in which case every configured UPS is checked.

The process **always exits 0** — a misbehaving handler must never wedge NUT's
event pipeline. Failures are logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
from pathlib import Path

from ups_orchestrator.config import Config
from ups_orchestrator.events import Deps, dispatch
from ups_orchestrator.notify import build_notifier
from ups_orchestrator.state import StateStore

LOG = logging.getLogger("ups_orchestrator")

_BASE = Path(__file__).resolve().parent.parent.parent
_ETC_CONFIG = Path("/etc/ups-orchestrator/config.json")
_VAR_STATE = Path("/var/lib/ups-orchestrator/state.json")


def _config_path() -> Path:
    """Resolve config: ``$UPS_ORCH_CONFIG`` → ``/etc/ups-orchestrator`` → ``<repo>``."""
    env = os.environ.get("UPS_ORCH_CONFIG")
    if env:
        return Path(env).expanduser()
    if _ETC_CONFIG.exists():
        return _ETC_CONFIG
    return _BASE / "config.json"


def _state_path() -> Path:
    """Resolve state: ``$UPS_ORCH_STATE`` → ``/var/lib/ups-orchestrator`` → ``<repo>``."""
    env = os.environ.get("UPS_ORCH_STATE")
    if env:
        return Path(env).expanduser()
    if _VAR_STATE.parent.is_dir():
        return _VAR_STATE
    return _BASE / "state.json"


def _resolve_ups_name(cli_value: str | None) -> str | None:
    """CLI arg wins; otherwise fall back to NUT's ``UPSNAME`` env var."""
    if cli_value:
        return cli_value
    env = os.environ.get("UPSNAME", "").strip()
    return env or None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator", description=__doc__)
    parser.add_argument("event", help="NUT event / timer name")
    parser.add_argument("ups_name", nargs="?", help="NUT UPS name (else $UPSNAME)")
    parser.add_argument("--config", type=Path, default=None, help="Override config path")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, os.environ.get("UPS_ORCH_LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        cfg = Config.load(args.config or _config_path())
    except (OSError, ValueError) as exc:
        LOG.error("Failed to load config: %s", exc)
        return 0  # never break NUT

    notifier = build_notifier(
        cfg.webhook_url,
        username=cfg.discord_username,
        avatar_url=cfg.discord_avatar_url,
        host=socket.gethostname(),
    )
    deps = Deps(notifier=notifier)
    store = StateStore(_state_path())

    ups_name = _resolve_ups_name(args.ups_name)
    event = args.event.lower()

    # `tick` with no UPS name → sweep every configured UPS.
    targets = [ups_name] if ups_name else (list(cfg.upses) if event == "tick" else [])
    if not targets:
        LOG.error("No UPS name provided for event %r (set arg or $UPSNAME)", event)
        return 0

    for name in targets:
        ups = cfg.ups(name) if name else None
        if ups is None:
            LOG.warning("Event %r for unconfigured UPS %r — ignoring", event, name)
            continue
        try:
            dispatch(event, ups, store.get(ups.name), deps)
        except Exception:  # noqa: BLE001 — never let a handler crash NUT
            LOG.exception("Handler for event %r (UPS %s) raised", event, ups.name)

    try:
        store.save()
    except OSError as exc:
        LOG.warning("Failed to persist state: %s", exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
