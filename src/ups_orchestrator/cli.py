"""Command-line entry point.

Modes::

    ups-orchestrator <nut-event> [ups]   # NUT upssched path: onbatt/online/
                                          # lowbatt/commbad/commok/remote_shutdown
    ups-orchestrator tick [ups]          # one poll iteration (shutdown checks + countdown)
    ups-orchestrator watch               # long-running poll loop (systemd --user service)
    ups-orchestrator status [--watch]    # terminal status table
    ups-orchestrator audit               # incident-oriented journald/UPS report
    ups-orchestrator baseline            # per-UPS draw baseline from recorder history
    ups-orchestrator selftest [ups]      # run a NUT battery self-test and alert on failure
    ups-orchestrator boot-audit          # one-shot post-boot abrupt-loss alert
    ups-orchestrator record              # high-frequency UPS telemetry recorder
    ups-orchestrator power-dashboard     # render/post a live+history power image
    ups-orchestrator webui               # local web dashboard (live status + history)
    ups-orchestrator notify-test         # send a Discord delivery test
    ups-orchestrator logs                # tail local JSONL logs

NUT exposes the active UPS to event handlers via the ``UPSNAME`` environment
variable. The process **always exits 0** on the event path so a misbehaving
handler never wedges NUT's pipeline; failures are logged.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import socket
import sys
import time
from pathlib import Path
from types import FrameType

from ups_orchestrator import audit, recorder, report
from ups_orchestrator import status as status_view
from ups_orchestrator.config import Config, UpsConfig
from ups_orchestrator.events import Deps, dispatch
from ups_orchestrator.jsonlog import append_event
from ups_orchestrator.notify import build_notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import StateStore

LOG = logging.getLogger("ups_orchestrator")

_BASE = Path(__file__).resolve().parent.parent.parent
_ETC_CONFIG = Path("/etc/ups-orchestrator/config.json")
_VAR_STATE = Path("/var/lib/ups-orchestrator/state.json")
_VAR_SAMPLES = Path("/var/lib/ups-orchestrator/samples.jsonl")
_VAR_EVENTS = Path("/var/lib/ups-orchestrator/events.jsonl")
_VAR_NOTIFICATIONS = Path("/var/lib/ups-orchestrator/notifications.jsonl")
_BOOT_AUDIT_MARKER = Path("/var/lib/ups-orchestrator/boot-audit.json")


def _config_path() -> Path:
    env = os.environ.get("UPS_ORCH_CONFIG")
    if env:
        return Path(env).expanduser()
    if _ETC_CONFIG.exists():
        return _ETC_CONFIG
    return _BASE / "config.json"


def _state_path() -> Path:
    env = os.environ.get("UPS_ORCH_STATE")
    if env:
        return Path(env).expanduser()
    if _VAR_STATE.parent.is_dir():
        return _VAR_STATE
    return _BASE / "state.json"


def _sample_path() -> Path:
    env = os.environ.get("UPS_ORCH_SAMPLES")
    if env:
        return Path(env).expanduser()
    if _VAR_SAMPLES.parent.is_dir():
        return _VAR_SAMPLES
    return _BASE / "samples.jsonl"


def _event_log_path() -> Path:
    env = os.environ.get("UPS_ORCH_EVENT_LOG")
    if env:
        return Path(env).expanduser()
    if _VAR_EVENTS.parent.is_dir():
        return _VAR_EVENTS
    return _BASE / "events.jsonl"


def _notification_log_path() -> Path:
    env = os.environ.get("UPS_ORCH_NOTIFICATION_LOG")
    if env:
        return Path(env).expanduser()
    if _VAR_NOTIFICATIONS.parent.is_dir():
        return _VAR_NOTIFICATIONS
    return _BASE / "notifications.jsonl"


def _boot_audit_marker_path() -> Path:
    env = os.environ.get("UPS_ORCH_BOOT_AUDIT_STATE")
    if env:
        return Path(env).expanduser()
    if _BOOT_AUDIT_MARKER.parent.is_dir():
        return _BOOT_AUDIT_MARKER
    return _BASE / "boot-audit.json"


def _load_config() -> Config | None:
    try:
        return Config.load(_config_path())
    except (OSError, ValueError) as exc:
        LOG.error("Failed to load config: %s", exc)
        return None


def _build_deps(cfg: Config) -> Deps:
    notifier = build_notifier(
        cfg.webhook_url,
        username=cfg.discord_username,
        avatar_url=cfg.discord_avatar_url,
        host=socket.gethostname(),
        delivery_log_path=_notification_log_path(),
    )
    event_log_path = _event_log_path()

    def _event_log(
        event: str,
        ups: UpsConfig,
        snap: UpsSnapshot | None,
        message: str,
        data: dict[str, object] | None,
    ) -> None:
        append_event(
            event_log_path,
            event,
            ups_name=ups.name,
            ups_label=ups.label,
            message=message,
            snapshot=snap,
            data=data,
        )

    return Deps(
        notifier=notifier,
        countdown_every=cfg.countdown_every_seconds,
        event_log=_event_log,
        load_step=cfg.load_step,
        sample_path=_sample_path(),
    )


def _resolve_ups_name(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    return os.environ.get("UPSNAME", "").strip() or None


def _cmd_event(event: str, ups_name: str | None) -> int:
    cfg = _load_config()
    if cfg is None:
        return 0
    deps = _build_deps(cfg)
    store = StateStore(_state_path())

    # `tick` with no UPS name sweeps every configured UPS.
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


def _cmd_watch() -> int:
    cfg = _load_config()
    if cfg is None:
        return 0
    deps = _build_deps(cfg)
    store = StateStore(_state_path())
    interval = max(5, cfg.poll_seconds)

    stop = False

    def _sig(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)

    LOG.info("watch: polling %d UPS(es) every %ds", len(cfg.upses), interval)
    while not stop:
        for name, ups in cfg.upses.items():
            try:
                dispatch("tick", ups, store.get(name), deps)
            except Exception:  # noqa: BLE001
                LOG.exception("tick failed for UPS %s", name)
        try:
            store.save()
        except OSError as exc:
            LOG.warning("Failed to persist state: %s", exc)
        # Sleep in short slices so SIGTERM is honoured promptly.
        for _ in range(interval):
            if stop:
                break
            time.sleep(1)
    LOG.info("watch: stopped")
    return 0


def _cmd_status(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator status")
    parser.add_argument("--watch", action="store_true", help="live-refresh until Ctrl-C")
    parser.add_argument("--interval", type=float, default=2.0, help="refresh seconds (--watch)")
    args = parser.parse_args(argv)
    cfg = _load_config()
    if cfg is None:
        return 1
    return status_view.run(
        cfg, watch=args.watch, interval=args.interval, sample_path=_sample_path()
    )


def _cmd_report(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator report")
    parser.add_argument("--print", action="store_true", help="print the report instead of sending")
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="also post the weekly power dashboard now (regardless of weekday)",
    )
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    note = report.build_report(cfg)
    if args.print:
        print(report.render_text(note))
        return 0
    deps = _build_deps(cfg)
    result = deps.notifier.send(note)
    LOG.info(
        "report: ok=%s configured=%s attempts=%d status=%s UPSes=%d",
        result.ok,
        result.configured,
        result.attempts,
        result.status_code,
        len(cfg.upses),
    )
    # The daily report carries the power dashboard once a week (Monday), so the
    # existing report timer delivers it without a separate timer/service.
    if args.dashboard or time.localtime().tm_wday == _DASHBOARD_WEEKDAY:
        _send_dashboard(cfg, hours=168)
    return 0 if result.ok else 1


_DASHBOARD_WEEKDAY = 0  # Monday; the daily report posts the dashboard on this day.


def _send_dashboard(cfg: Config, hours: int) -> tuple[bool, int]:
    """Render the power dashboard and post it to Discord. Never raises.

    Returns ``(ok, status)``; ``(False, 0)`` if matplotlib is unavailable or the
    render/post fails, so a missing renderer degrades gracefully instead of
    breaking the daily report.
    """
    from ups_orchestrator import dashboard

    try:
        png = dashboard.render_png(cfg, _sample_path(), hours=hours, host=socket.gethostname())
    except ImportError:
        LOG.warning("power-dashboard: matplotlib not installed; skipping image")
        return False, 0
    except Exception:  # noqa: BLE001 — a render failure must not sink the report
        LOG.exception("power-dashboard: render failed")
        return False, 0
    ok, status = dashboard.post_png(
        cfg.webhook_url,
        png,
        content=f"**Power dashboard — {socket.gethostname()}** (last {hours}h)",
        username=cfg.discord_username,
        avatar_url=cfg.discord_avatar_url,
    )
    LOG.info("power-dashboard: posted ok=%s status=%s hours=%d", ok, status, hours)
    return ok, status


def _cmd_power_dashboard(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator power-dashboard")
    parser.add_argument("--hours", type=int, default=168, help="history window (default 168=7d)")
    parser.add_argument("--out", type=Path, help="also write the PNG to this path")
    parser.add_argument("--post", action="store_true", help="post the image to Discord")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    from ups_orchestrator import dashboard

    try:
        png = dashboard.render_png(
            cfg, _sample_path(), hours=max(1, args.hours), host=socket.gethostname()
        )
    except ImportError:
        LOG.error("power-dashboard: matplotlib is not installed in this environment")
        return 1
    if args.out:
        args.out.write_bytes(png)
        print(f"wrote {args.out} ({len(png)} bytes)")
    if args.post:
        ok, status = dashboard.post_png(
            cfg.webhook_url,
            png,
            content=f"**Power dashboard — {socket.gethostname()}** (last {args.hours}h)",
            username=cfg.discord_username,
            avatar_url=cfg.discord_avatar_url,
        )
        LOG.info("power-dashboard: posted ok=%s status=%s", ok, status)
        return 0 if ok else 1
    if not args.out:
        LOG.error("power-dashboard: nothing to do — pass --out PATH and/or --post")
        return 2
    return 0


def _cmd_baseline(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator baseline")
    parser.add_argument("--hours", type=int, default=168, help="analysis window (default 168=7d)")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    from ups_orchestrator import baseline

    stats = baseline.compute_baselines(_sample_path(), list(cfg.upses), hours=max(1, args.hours))
    print(baseline.render_text(cfg, stats, hours=max(1, args.hours)))
    return 0


def _cmd_selftest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator selftest")
    parser.add_argument("ups", nargs="?", help="UPS name (default: every configured UPS)")
    parser.add_argument(
        "--timeout", type=float, default=180.0, help="max seconds to await a result"
    )
    parser.add_argument(
        "--user-env", default="UPS_NUT_ADMIN_USER", help="env var holding the admin user"
    )
    parser.add_argument(
        "--password-env",
        default="UPS_NUT_ADMIN_PASSWORD",
        help="env var holding the admin password",
    )
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    user = os.environ.get(args.user_env, "").strip()
    password = os.environ.get(args.password_env, "")
    if not user or not password:
        LOG.error(
            "selftest: NUT admin credentials not set (%s / %s)", args.user_env, args.password_env
        )
        return 2

    from ups_orchestrator import selftest
    from ups_orchestrator.notify import Level, Notification
    from ups_orchestrator.nut import upsc_var

    deps = _build_deps(cfg)
    targets = [args.ups] if args.ups else list(cfg.upses)
    any_problem = False
    for name in targets:
        ups = cfg.ups(name)
        if ups is None:
            LOG.warning("selftest: unconfigured UPS %r — skipping", name)
            continue
        snap = read_snapshot(ups.name)
        result = selftest.run_selftest(
            ups.name,
            snap,
            user=user,
            password=password,
            read_result=lambda u: upsc_var(u, "ups.test.result"),
            timeout=max(5.0, args.timeout),
        )
        print(f"{ups.label}: {result.outcome} — {result.detail}")
        if result.outcome != "skipped":
            deps.notifier.send(
                Notification(
                    title=f"🔋 {ups.label} — battery self-test: {result.outcome}",
                    body=result.detail,
                    level=Level.WARNING if result.is_problem else Level.INFO,
                )
            )
        any_problem = any_problem or result.is_problem
    return 1 if any_problem else 0


def _cmd_webui(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator webui")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (localhost by default)")
    parser.add_argument("--port", type=int, default=8765, help="listen port")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    from ups_orchestrator import webui

    LOG.info("webui: serving http://%s:%d — no auth, do not expose publicly", args.host, args.port)
    webui.serve(cfg, _sample_path(), host=args.host, port=args.port)
    return 0


def _cmd_audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator audit")
    parser.add_argument("--since", default="today", help="journalctl --since window")
    parser.add_argument("--limit", type=int, default=80, help="max matching lines per section")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    result = audit.build_audit(
        cfg,
        since=args.since,
        limit=max(1, args.limit),
        state_path=_state_path(),
        event_log_path=_event_log_path(),
        notification_log_path=_notification_log_path(),
        sample_path=_sample_path(),
    )
    print(result.text)
    return 0


def _cmd_boot_audit(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator boot-audit")
    parser.add_argument("--limit", type=int, default=12, help="max evidence lines to inspect")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    deps = _build_deps(cfg)
    result = audit.send_boot_audit(
        cfg,
        deps.notifier,
        marker_path=_boot_audit_marker_path(),
        sample_path=_sample_path(),
        limit=max(1, args.limit),
    )
    LOG.info(
        "boot-audit: sent=%s power_loss=%d shutdown_evidence=%d",
        result.sent,
        result.power_loss_count,
        result.shutdown_action_count,
    )
    return 0


def _cmd_notify_test(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator notify-test")
    parser.add_argument("--print", action="store_true", help="print the test payload instead")
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    note = report.build_report(cfg)
    note.title = "UPS Orchestrator Discord delivery test"
    note.body = (
        "This is a delivery test from the live UPS orchestrator. "
        "If this arrives, webhook auth, systemd environment, and embed rendering work."
    )
    if args.print:
        print(report.render_text(note))
        return 0

    deps = _build_deps(cfg)
    result = deps.notifier.send(note)
    print(
        "notification delivery: "
        f"ok={result.ok} configured={result.configured} attempts={result.attempts} "
        f"status={result.status_code or 'n/a'}" + (f" error={result.error}" if result.error else "")
    )
    return 0 if result.ok else 1


def _tail(path: Path, lines: int) -> list[str]:
    try:
        return path.read_text().splitlines()[-lines:]
    except FileNotFoundError:
        return [f"{path}: not found"]
    except OSError as exc:
        return [f"{path}: unreadable ({exc})"]


def _cmd_logs(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator logs")
    parser.add_argument(
        "kind",
        choices=("events", "notifications", "samples"),
        nargs="?",
        default="events",
        help="which local JSONL log to tail",
    )
    parser.add_argument("--lines", type=int, default=20, help="number of JSONL lines")
    args = parser.parse_args(argv)

    path = {
        "events": _event_log_path(),
        "notifications": _notification_log_path(),
        "samples": _sample_path(),
    }[args.kind]
    print(f"{args.kind}: {path}")
    for line in _tail(path, max(1, args.lines)):
        print(line)
    return 0


def _cmd_record(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator record")
    parser.add_argument("--interval", type=float, default=1.0, help="sample interval seconds")
    parser.add_argument("--path", type=Path, default=_sample_path(), help="JSONL sample path")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=recorder.DEFAULT_MAX_BYTES,
        help="rotate after this size",
    )
    parser.add_argument(
        "--max-rotations",
        type=int,
        default=recorder.DEFAULT_MAX_ROTATIONS,
        help="number of historical sample segments to retain",
    )
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1

    stop = False

    def _sig(_signum: int, _frame: FrameType | None) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGTERM, _sig)
    signal.signal(signal.SIGINT, _sig)
    LOG.info("record: writing UPS samples to %s every %.2fs", args.path, args.interval)
    recorder.run(
        cfg,
        path=args.path,
        interval=args.interval,
        max_bytes=args.max_bytes,
        max_rotations=max(0, args.max_rotations),
        stop=lambda: stop,
    )
    LOG.info("record: stopped")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    logging.basicConfig(
        level=getattr(logging, os.environ.get("UPS_ORCH_LOGLEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    if not args:
        LOG.error(
            "usage: ups-orchestrator "
            "<event|tick|watch|status|report|audit|baseline|selftest|boot-audit|record|"
            "power-dashboard|webui|notify-test|logs> [...]"
        )
        return 0

    mode = args[0].lower()
    if mode == "status":
        return _cmd_status(args[1:])
    if mode == "report":
        return _cmd_report(args[1:])
    if mode == "audit":
        return _cmd_audit(args[1:])
    if mode == "baseline":
        return _cmd_baseline(args[1:])
    if mode == "selftest":
        return _cmd_selftest(args[1:])
    if mode == "webui":
        return _cmd_webui(args[1:])
    if mode == "boot-audit":
        return _cmd_boot_audit(args[1:])
    if mode == "notify-test":
        return _cmd_notify_test(args[1:])
    if mode == "logs":
        return _cmd_logs(args[1:])
    if mode == "record":
        return _cmd_record(args[1:])
    if mode == "power-dashboard":
        return _cmd_power_dashboard(args[1:])
    if mode == "watch":
        return _cmd_watch()
    return _cmd_event(mode, _resolve_ups_name(args[1] if len(args) > 1 else None))


if __name__ == "__main__":
    sys.exit(main())
