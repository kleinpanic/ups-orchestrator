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
    ups-orchestrator control <action>    # beeper/battery-test instant commands (all UPSes)
    ups-orchestrator boot-audit          # one-shot post-boot abrupt-loss alert
    ups-orchestrator record              # high-frequency UPS telemetry recorder
    ups-orchestrator power-dashboard     # render/post a live+history power image
    ups-orchestrator webui               # local web dashboard (live status + history)
    ups-orchestrator remote-shutdown [ups] [--dry-run]
                                         # evaluate configured + projected targets;
                                         # --dry-run previews every one with its gate
                                         # verdict and touches nothing
    ups-orchestrator shutdown rehearse <machine>
                                         # push a hard-coded NON-shutdown command over
                                         # the machine's configured transport
    ups-orchestrator notify-test         # send a Discord delivery test
    ups-orchestrator logs                # tail local JSONL logs

NUT exposes the active UPS to event handlers via the ``UPSNAME`` environment
variable. A misbehaving *handler* never wedges NUT's pipeline — every dispatch is
caught and logged, and the event path still exits 0. The one exception is a config
that cannot be LOADED (IW-06): that returns non-zero from both the event and the
watch entry points, because a silent successful no-op on `onbatt`/`lowbatt` is
worse than a loud failure at the moment the daemon exists for.
"""

from __future__ import annotations

import argparse
import dataclasses
import ipaddress
import json
import logging
import os
import signal
import socket
import stat
import subprocess
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from types import FrameType

from ups_orchestrator import audit, nutclient, recorder, report
from ups_orchestrator import status as status_view

# valid_ssh_alias is imported deliberately rather than re-spelled here: T-02-10's
# CLI check and 02-06's load-time check must be the SAME rule, or a value the CLI
# accepts could be disarmed at load (or worse, the reverse). One predicate, every sink.
from ups_orchestrator.config import (
    Config,
    MonitoredMachine,
    ShutdownTarget,
    UpsConfig,
    dual_regime_conflicts,
    is_disarming,
    valid_ssh_alias,
)

# The preview reuses the firing path's OWN gate and projection rather than
# re-deriving either. A second copy of "would this fire?" is a second answer.
from ups_orchestrator.events import (
    Deps,
    _machine_targets,
    _target_location,
    _target_should_fire,
    dispatch,
    ssh_dest,
)
from ups_orchestrator.jsonlog import append_event
from ups_orchestrator.notify import build_notifier
from ups_orchestrator.nut import UpsSnapshot, read_snapshot
from ups_orchestrator.state import StateStore, UpsState, replace_preserving_metadata

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


def _build_deps(cfg: Config, *, dry_run: bool = False) -> Deps:
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
        onbatt_notify_grace=cfg.onbatt_notify_grace_seconds,
        event_log=_event_log,
        load_step=cfg.load_step,
        sample_path=_sample_path(),
        monitored_machines=cfg.monitored_machines,
        dry_run=dry_run,
    )


def _resolve_ups_name(cli_value: str | None) -> str | None:
    if cli_value:
        return cli_value
    return os.environ.get("UPSNAME", "").strip() or None


def _notify_degraded(cfg: Config, deps: Deps) -> None:
    """Log every load-time notice and send ONE aggregated notification.

    ``Config.load`` runs before ``_build_deps`` constructs the notifier, so the
    load itself cannot notify — this is the first point at which one exists. It is
    a STARTUP surface only: ``tick`` runs every poll and must not page repeatedly.
    Exactly one notification, never one per notice, and nothing at all when the
    tuple is empty.
    """
    if not cfg.degraded:
        return
    from ups_orchestrator.notify import Level, Notification

    for n in cfg.degraded:
        if is_disarming(n):
            LOG.error("config degrade: %s", n)
        else:
            LOG.warning("config advisory: %s", n)
    errors = [n for n in cfg.degraded if is_disarming(n)]
    advisories = [n for n in cfg.degraded if not is_disarming(n)]
    # Discord caps an embed at 25 fields; the overflow is still in the journal and
    # in `monitor list`, so truncating here loses nothing an operator cannot reach.
    shown = list(cfg.degraded)[:20]
    overflow = len(cfg.degraded) - len(shown)
    body = (
        f"{len(errors)} shutdown authority/ies disarmed, {len(advisories)} advisory. "
        f"Run 'ups-orchestrator monitor list' for the full set and "
        f"'ups-orchestrator monitor verify <machine>' per machine."
    )
    if overflow:
        body += f" ({overflow} further notice(s) not shown.)"
    deps.notifier.send(
        Notification(
            title=f"⚠️ Config degraded at startup — {len(cfg.degraded)} notice(s)",
            body=body,
            level=Level.CRITICAL if errors else Level.WARNING,
            fields=[(n.subject, f"[{n.severity}] {n.message}") for n in shown],
        )
    )


def _cmd_event(event: str, ups_name: str | None) -> int:
    cfg = _load_config()
    if cfg is None:
        # IW-06: this returned 0. `deploy/upssched-cmd.sh` invokes it for onbatt,
        # lowbatt and remote_shutdown, so a config that cannot be loaded turned
        # every real NUT power event into a silent no-op that reported SUCCESS —
        # at the exact moment the daemon exists for. After 02-06's RA-01 only the
        # seven fatal classes reach here, so it fires rarely and correctly.
        return 1
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
        # IW-06: this returned 0, so systemd saw a clean exit and Restart= never
        # fired. A watch unit with a restart policy will now enter a restart loop
        # on an unparseable config — the intended louder failure.
        return 1
    deps = _build_deps(cfg)
    _notify_degraded(cfg, deps)
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


# Safe instant commands only. Power/shutdown commands (load.off, shutdown.*,
# driver.killpower) are deliberately NOT exposed here — they cut power to
# everything on the UPS and belong to the gated shutdown path, not a CLI verb.
_CONTROL_ACTIONS = {
    "beeper-mute": "beeper.mute",
    "beeper-disable": "beeper.disable",
    "beeper-enable": "beeper.enable",
    "test-quick": "test.battery.start.quick",
    "test-deep": "test.battery.start.deep",
    "test-stop": "test.battery.stop",
}

# Emoji per action family, for the Discord counterpart of a control run.
_CONTROL_EMOJI = {"beeper": "🔇", "test": "🔋"}


def _cmd_control(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="ups-orchestrator control",
        description="Run a safe instant command on one or all UPSes (beeper/battery test). "
        "These CyberPower units expose no display control.",
    )
    parser.add_argument("action", choices=sorted(_CONTROL_ACTIONS))
    parser.add_argument("ups", nargs="?", help="UPS name (default: every configured UPS)")
    parser.add_argument("--user-env", default="UPS_NUT_ADMIN_USER")
    parser.add_argument("--password-env", default="UPS_NUT_ADMIN_PASSWORD")
    parser.add_argument(
        "--no-notify", action="store_true", help="skip the Discord notification counterpart"
    )
    args = parser.parse_args(argv)

    cfg = _load_config()
    if cfg is None:
        return 1
    user = os.environ.get(args.user_env, "").strip()
    password = os.environ.get(args.password_env, "")
    if not user or not password:
        LOG.error("control: NUT admin creds not set (%s / %s)", args.user_env, args.password_env)
        return 2

    from ups_orchestrator.nut import upscmd

    command = _CONTROL_ACTIONS[args.action]
    targets = [args.ups] if args.ups else list(cfg.upses)
    # (label, ok, detail) per UPS actually acted on — drives both CLI and Discord.
    results: list[tuple[str, bool, str]] = []
    for name in targets:
        ups = cfg.ups(name)
        if ups is None:
            LOG.warning("control: unconfigured UPS %r — skipping", name)
            continue
        rc, out, err = upscmd(ups.name, command, user=user, password=password)
        ok = rc == 0
        detail = "OK" if ok else f"FAIL: {(err or out or 'upscmd error').splitlines()[0]}"
        print(f"{ups.label}: {args.action} ({command}) -> {detail}")
        results.append((ups.label, ok, detail))

    any_fail = any(not ok for _, ok, _ in results)
    if results and not args.no_notify:
        _notify_control(cfg, args.action, command, results, any_fail)
    return 1 if any_fail else 0


def _notify_control(
    cfg: Config,
    action: str,
    command: str,
    results: list[tuple[str, bool, str]],
    any_fail: bool,
) -> None:
    """Post the Discord counterpart of a control run (one summary embed)."""
    from ups_orchestrator.notify import Level, Notification

    emoji = _CONTROL_EMOJI.get(action.split("-", 1)[0], "🎛️")
    ok_n = sum(1 for _, ok, _ in results if ok)
    level = Level.WARNING if any_fail else Level.SUCCESS
    note = Notification(
        title=f"{emoji} control: {action} — {ok_n}/{len(results)} OK",
        body=f"`{command}` across {len(results)} UPS(es)",
        level=level,
        fields=[(label, detail) for label, _, detail in results],
    )
    _build_deps(cfg).notifier.send(note)


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


# ---- monitor CLI family (NUT secondary enrollment) --------------------------
#
# Injectable seams: tests monkeypatch these module-level callables so the whole
# enrollment sequence drives against recording fakes with no live host. Live
# runs use nutclient's real default runners (excluded from the coverage floor).
_monitor_run_ssh = nutclient._default_run_ssh


def _monitor_run_local(  # pragma: no cover — live only
    argv: Sequence[str], stdin: str | None = None
) -> tuple[int, str, str]:
    proc = subprocess.run(list(argv), input=stdin, capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


_SECRET_ENV = "UPS_NUT_SECONDARY_PASSWORD"
_UPSD_CONF_PATH = "/etc/nut/upsd.conf"
_UPSD_USERS_PATH = "/etc/nut/upsd.users"
_REMOTE_UPSMON_PATH = "/etc/nut/upsmon.conf"
_REMOTE_NUT_CONF_PATH = "/etc/nut/nut.conf"
# The accept is spliced into the operator's input base chain, which lives in the
# fragment that /etc/nftables.conf includes — NOT a dedicated file of our own.
# _NFT_PATH is the fragment we edit; _NFT_RELOAD_PATH is the top-level file handed
# to `nft -f` so the whole ruleset (with the include) is reloaded atomically.
_NFT_PATH = "/etc/nftables.d/main.nft"
_NFT_RELOAD_PATH = "/etc/nftables.conf"


def _monitor_run_nft(path: str) -> tuple[int, str, str]:  # pragma: no cover — live only
    return nutclient._default_run_local(["nft", "-f", path])


def _monitor_restart_bouncer() -> None:  # pragma: no cover — live only
    nutclient._default_run_local(["systemctl", "restart", "crowdsec-firewall-bouncer"])


def _monitor_run_local_probe(argv: Sequence[str]) -> tuple[int, str, str]:  # pragma: no cover
    """Live local command runner for read-only probes (no stdin, no /etc write)."""
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    return proc.returncode, proc.stdout, proc.stderr


def _resolve_primary_ip(cfg: Config, override: str | None, toward_ip: str) -> str | None:
    """Resolve the primary's LAN IP the secondary's MONITOR line points at.

    Order: explicit ``--primary-ip`` override → first non-loopback
    ``nut_server.listen`` entry → local ``ip -o route get <toward_ip>`` ``src``
    (the primary's address on the path to the secondary) → ``None``.

    The route probe closes the live gap where, without ``--primary-ip`` and with
    only a loopback LISTEN configured, the old code silently returned
    ``127.0.0.1`` — so no LAN LISTEN was written and upsd stayed localhost-only,
    failing enrollment at verify with no clear error. Returning ``None`` here lets
    the caller ERROR clearly instead.
    """
    if override:
        return override.strip() if _valid_ip(override) else None
    for addr in cfg.nut_server.listen:
        if addr not in ("127.0.0.1", "::1", "localhost") and _valid_ip(addr):
            return addr
    if _valid_ip(toward_ip):
        rc, out, _err = _monitor_run_local_probe(["ip", "-o", "route", "get", toward_ip])
        if rc == 0:
            return _parse_route_src(out)
    return None


def _survivor_saddrs(machines: tuple[MonitoredMachine, ...]) -> list[str]:
    """nft saddr union from the surviving NATIVE machines (validated IP literals).

    An empty-ip survivor would render an invalid ``ip saddr { }`` and fail
    ``nft -f``; drop it (deduplicated, order-preserving).

    HI-C2, two filters, both load-bearing:

    * ``_valid_ip``. The value is spliced verbatim into ``tcp dport 3493 ip saddr
      { … } accept`` and the result is loaded by ``nft -f`` as root. ``_valid_ip``
      guarded only the ``--ip`` argparse path, so a hand-edited record could close
      the brace and append its own ``ip saddr 0.0.0.0/0 accept`` above the
      operator's policy drop. A rejected value is logged, not silently dropped.
    * ``shutdown_method == "native"`` (the DECLARED method, INV-DECLARED). Only a
      native secondary talks to upsd; a serial/ssh/none record carrying a stale
      enrollment ``ip`` was being granted an upsd accept on every native add.

    Together the managed set becomes exactly "the IP of every surviving native
    record", which is the invariant it was always supposed to have.
    """
    out: list[str] = []
    for m in machines:
        if m.shutdown_method.strip().lower() != "native":
            continue
        ip = m.ip.strip()
        if not ip:
            continue
        if not _valid_ip(ip):
            LOG.warning(
                "monitor: ignoring machine %r's ip %r for the nft saddr set — it is not a "
                "valid IP literal, and this value is loaded into the ruleset by nft -f. "
                "Fix 'ip' in the config, or re-run 'monitor add %s' to re-resolve it.",
                m.name,
                m.ip,
                m.name,
            )
            continue
        if ip not in out:
            out.append(ip)
    return out


def _monitor_persist(cfg_path: Path, machines: list[dict[str, object]]) -> None:
    """Write monitored_machines back by mutating the RAW config dict, atomically.

    Unknown keys (e.g. a ``_comment``) are preserved because we round-trip the
    parsed JSON rather than a frozen Config. The write is
    temp+fsync+replace_preserving_metadata (state.py) so a crash mid-write
    can't corrupt the file the watch service reads, and the replace itself
    can't strip the mode/owner/ACL the installer gave the file (T-02-23).
    The secondary password is never among the written fields.
    """
    import tempfile

    raw = json.loads(cfg_path.read_text())
    raw["monitored_machines"] = machines
    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            dir=cfg_path.parent,
            prefix=f".{cfg_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp:
            tmp_path = Path(tmp.name)
            json.dump(raw, tmp, indent=2, sort_keys=True)
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        replace_preserving_metadata(tmp_path, cfg_path)
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _monitor_find(cfg: Config, name: str) -> MonitoredMachine | None:
    lname = name.strip().lower()
    for m in cfg.monitored_machines:
        if m.name.strip().lower() == lname:
            return m
    return None


_REMOTE_DISARM = (
    "sudo systemctl disable --now nut-monitor.service; "
    "sudo rm -f /etc/systemd/system/nut-monitor.service.d/network-online.conf; "
    "sudo systemctl daemon-reload; "
    "printf 'MODE=none\\n' | sudo install -m 0640 -o root -g nut /dev/stdin "
    f"{_REMOTE_NUT_CONF_PATH}"
)


def _print_degraded(cfg: Config) -> None:
    """Render every load-time notice under a marked heading, or nothing at all.

    RA-01's own argument is that a journal line is an insufficient operator
    surface, so ``Config.degraded`` has to reach the operator wherever the
    operator already looks. This is the ``monitor`` half; ``status`` and the web
    UI carry the same tuple.

    LO-C5: ``subject`` and ``message`` carry machine names, device paths and ssh
    aliases straight out of the config, so they are the same value-injection
    boundary MED-06 closed in ``status.py`` — a machine named ``mt\\x1b[2J\\x1b[H``
    erases the very banner reporting on it. Routed through that fix's own
    ``_safe`` rather than a second copy of the regex: one predicate, every sink.
    """
    if not cfg.degraded:
        return
    print("")
    print("⚠ DEGRADED CONFIG — a shutdown authority was disarmed or flagged at load:")
    for n in cfg.degraded:
        label = "ERROR" if is_disarming(n) else "ADVISORY"
        print(f"  {label} {status_view._safe(n.subject)}: {status_view._safe(n.message)}")


def _method_field(m: MonitoredMachine) -> str:
    """``method=<declared>`` plus the EFFECT only when the two differ.

    The declaration comes first because it is what every gate reads and what the
    operator authored; the effect is annotated only when a load-time degrade has
    taken the declaration away (INV-DECLARED / INV-DEGRADE).
    """
    if m.effective_method == m.shutdown_method:
        return f"method={m.shutdown_method}"
    return f"method={m.shutdown_method}(effective:{m.effective_method})"


def _monitor_list(cfg: Config) -> int:
    if not cfg.monitored_machines:
        # Still falls through to the notices: a config can carry zero machines and
        # a disabled legacy target.
        print("no machines enrolled")
    for m in cfg.monitored_machines:
        backup = "backup:on" if m.backup.enabled else "backup:off"
        print(
            f"{m.name}\tssh={m.ssh}\tups={m.ups}\tos={m.os}\tip={m.ip or '-'}\t"
            f"{_method_field(m)}\t{backup}"
        )
    _print_degraded(cfg)
    return 0


def _probe_secondary_reason(m: MonitoredMachine) -> str | None:
    """Why ``monitor verify`` must go looking for a live remote NUT secondary.

    The RULE rather than a list of methods to remember: probe ANY record that could
    plausibly have one, because that probe is the only evidence available on THIS
    box about an authority that lives on another one.

    * declared ``native`` — the remote ``upsmon`` IS the authority, and config can
      never disarm it, so verify must go and look;
    * declared ``none`` carrying a ``ups`` — BL-02's exact signature. ``none`` does
      not disarm an already-enrolled secondary, and answering "no active authority"
      would falsely reassure the operator who most needs the truth;
    * a push declaration carrying an ``ip`` — IW-05. ``ip`` is written only by the
      native enrollment path, so this is a probable hand-edited former secondary
      whose remote ``upsmon`` was never torn down.

    Reads the DECLARATION throughout (INV-DECLARED).
    """
    declared = m.shutdown_method.strip().lower()
    if declared == "native":
        return "native"
    if declared == "none" and m.ups.strip():
        return "none-with-ups"
    if declared in ("serial", "ssh") and m.ip.strip():
        return "push-with-enrollment-ip"
    return None


def _verify_serial(m: MonitoredMachine, stat_fn: Callable[[str], os.stat_result]) -> int:
    """Device presence and a declared baud, through an INJECTED stat.

    The stat is injected so a unit test never reaches for a real ``/dev`` node.
    """
    device = m.serial_device.strip()
    if not device:
        print(f"{m.name}: FAIL — declares serial with no serial_device")
        return 1
    try:
        mode = stat_fn(device).st_mode
    except OSError as exc:
        print(f"{m.name}: FAIL — serial device {device} is not present ({exc})")
        return 1
    if not stat.S_ISCHR(mode):
        print(f"{m.name}: FAIL — {device} is not a character device ({stat.filemode(mode)})")
        return 1
    if m.serial_baud is None or m.serial_baud <= 0:
        print(f"{m.name}: FAIL — no usable declared serial_baud (the live console here is 9600)")
        return 1
    print(f"{m.name}: OK — serial {device} present at a declared {m.serial_baud} baud")
    return 0


def _verify_ssh_alias(m: MonitoredMachine) -> int:
    """Reachability of the recorded alias. No NUT secondary check — there is none."""
    alias = m.ssh.strip()
    if not alias:
        print(f"{m.name}: FAIL — declares ssh with no alias")
        return 1
    if not _valid_ssh_alias(alias):
        LOG.error("monitor verify: config ssh alias %r for %s is invalid", m.ssh, m.name)
        return 2
    rc, _out, err = _monitor_run_ssh(alias, "true", None)
    if rc != 0:
        print(f"{m.name}: FAIL — ssh {alias} unreachable ({err.strip() or f'rc {rc}'})")
        return 1
    print(f"{m.name}: OK — ssh {alias} reachable")
    return 0


def _monitor_verify(
    cfg: Config,
    argv: list[str],
    stat_fn: Callable[[str], os.stat_result] = os.stat,
) -> int:
    """Answer the question an operator actually asks: *will this machine shut down?*

    Every branch reads the DECLARED ``shutdown_method``. Branching on the effect
    would render a machine a load-time degrade disarmed identically to one the
    operator deliberately declared ``none`` — the same truth wearing two very
    different meanings (T-02-46).
    """
    parser = argparse.ArgumentParser(prog="ups-orchestrator monitor verify")
    parser.add_argument("name")
    parser.add_argument("--timeout", type=int, default=10, help="seconds to await a result")
    parser.add_argument("--deep", action="store_true", help="also grep the remote auth journal")
    parser.add_argument("--primary-ip", help="override the LAN address the secondary connects to")
    args = parser.parse_args(argv)

    machine = _monitor_find(cfg, args.name)
    if machine is None:
        LOG.error("monitor verify: unknown machine %r", args.name)
        return 2
    if args.primary_ip and not _valid_ip(args.primary_ip):
        LOG.error("monitor verify: --primary-ip %r is not a valid IP literal", args.primary_ip)
        return 2

    declared = machine.shutdown_method
    effective = machine.effective_method
    head = f"{machine.name}: declared shutdown_method={declared}"
    if effective != declared:
        head += f" (effective: {effective})"
    print(head)

    rc = 0
    if machine.disarmed:
        for n in machine.load_notices:
            if is_disarming(n):
                print(f"  DISARMED (declared {declared}): {n.message}")
        rc = 1
    else:
        for n in machine.load_notices:
            print(f"  {n.severity.upper()}: {n.message}")

    probe = _probe_secondary_reason(machine)
    if probe is not None:
        # ME-C2: a BLANK ups is not bad input to this command — it is the state the
        # command was sent to diagnose. `Config` cannot disarm a native authority, so
        # the advisory for that record says in as many words: "Run 'monitor verify
        # <name>' to learn whether that secondary is actually armed." `valid_nut_name("")`
        # is False, so the remedy the phase designed for this state answered rc 2 —
        # which means "bad input to the command" in every other branch here, and sends
        # a script (and an operator) off to check for a typo'd machine name. Report the
        # real answer instead: there is no `upsc <ups>@<primary>` to run, so nothing can
        # be established, which is rc 1. Kept ABOVE the charset check so a blank value
        # never lands in the metachar branch — a blank alias is not an injection.
        if not machine.ups.strip():
            print(
                f"  cannot probe: this record has no 'ups', so there is no "
                f"'upsc <ups>@<primary>' to run and nothing here can establish whether a "
                f"secondary is armed on that box. Set 'ups' to the UPS that powers it, or "
                f"run 'monitor remove {machine.name}' — the only command that actually "
                f"disarms the remote secondary."
            )
            return 1
        # machine.ups is config-sourced and flows into a remote shell string in
        # verify_secondary; refuse a metachar-bearing value instead of running it.
        if not nutclient.valid_nut_name(machine.ups):
            LOG.error(
                "monitor verify: config UPS name %r for %s is invalid", machine.ups, machine.name
            )
            return 2
        # BL-C1: the alias is config-sourced too and becomes an argv element of the
        # ssh this is about to run. `verify_secondary` re-validates `ups` and `primary`
        # and documents that it does NOT validate the alias, and the loader cannot
        # cover this path either — `_transport_notices` applies the alias rule only
        # under method == "ssh", and a native record is never disarmed by
        # construction. This is the only checkpoint, and it was checking the other
        # field. `monitor remove` remains available to clean the record up.
        if machine.ssh.strip() and not _valid_ssh_alias(machine.ssh):
            LOG.error(
                "monitor verify: config ssh alias %r for %s is not a plain host or "
                "ssh_config alias; refusing to run the probe. Fix 'ssh' in the config, "
                "or run 'monitor remove %s'.",
                machine.ssh,
                machine.name,
                machine.name,
            )
            return 2
        # ME-C1: the SAME resolver `monitor add` uses, so the two commands agree on
        # what "the primary" is. The old `_monitor_primary_ip` validated nothing and
        # fell back to `127.0.0.1` — which, in a command whose whole job is to run
        # `upsc <ups>@<primary>` ON THE SECONDARY, made the probe interrogate that
        # box's own (absent) upsd instead of this one. Wrong answer in both
        # directions: normally a false FAIL telling an operator a protected box is
        # unprotected, and a false OK if the secondary happens to run its own upsd
        # with a same-named UPS. `_resolve_primary_ip` returns None rather than
        # inventing a value, which is what lets this say so.
        primary = _resolve_primary_ip(cfg, args.primary_ip, machine.ip)
        if not primary:
            LOG.error(
                "monitor verify: could not resolve the primary's LAN IP toward %s — the "
                "probe runs 'upsc %s@<primary>' ON THAT MACHINE, so a loopback address "
                "would interrogate its own upsd rather than this one. Pass --primary-ip "
                "or add a LAN LISTEN to nut_server.listen.",
                machine.name,
                machine.ups,
            )
            return 2
        ok, detail = nutclient.verify_secondary(
            machine.ssh,
            machine.ups,
            primary,
            _monitor_run_ssh,
            timeout=args.timeout,
            deep=args.deep,
        )
        print(f"{machine.name}: {'OK' if ok else 'FAIL'} — {detail}")
        if probe == "native":
            if machine.load_notices:
                print(
                    f"  the remote secondary remains the surviving authority — it lives in "
                    f"that box's /etc and no config change here disarms it. "
                    f"'monitor remove {machine.name}' is the only real disarm."
                )
            return 0 if ok else 1
        if ok:
            print(
                f"  this record declares {declared!r}, and a live NUT secondary answers on "
                f"that box. Run 'monitor remove {machine.name}' to actually disarm it."
            )
            rc = 1
        return rc

    if machine.disarmed:
        return rc  # it will not fire; there is no transport left to check
    if declared == "serial":
        return _verify_serial(machine, stat_fn) or rc
    if declared == "ssh":
        return _verify_ssh_alias(machine) or rc
    print(f"{machine.name}: no active shutdown authority")
    return rc


def _monitor_remove(cfg: Config, cfg_path: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator monitor remove")
    parser.add_argument("name")
    parser.add_argument(
        "--keep-remote", action="store_true", help="do not disarm the secondary over SSH"
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan, mutate nothing")
    parser.add_argument("--no-firewall", action="store_true", help="skip the nft rewrite")
    parser.add_argument("--no-restart-bouncer", action="store_true", help="skip bouncer restart")
    args = parser.parse_args(argv)

    # T-02-48: 02-06 KEEPS every duplicate record and disarms them all, so
    # `_monitor_find`'s first-wins would delete an arbitrary one of them — and
    # `_monitor_persist` rewrites the whole array, making that a real deletion.
    lname = args.name.strip().casefold()
    matches = [m for m in cfg.monitored_machines if m.name.strip().casefold() == lname]
    if len(matches) > 1:
        LOG.error(
            "monitor remove: %d records share the name %r (case-insensitively), so which "
            "one to remove is a guess. All of them are disarmed at load. De-duplicate "
            "monitored_machines by hand, then re-run this command.",
            len(matches),
            args.name,
        )
        return 2

    machine = _monitor_find(cfg, args.name)
    if machine is None:
        LOG.error("monitor remove: unknown machine %r", args.name)
        return 2

    survivors = tuple(m for m in cfg.monitored_machines if m is not machine)
    saddrs = _survivor_saddrs(survivors)
    # The remote NUT teardown and the nft rewrite exist for a native enrollment and
    # only for one. A serial/ssh/none record has no remote upsmon to disable and no
    # upsd accept of its own; running either would touch a box this record never
    # enrolled. Keyed on the DECLARED method (INV-DECLARED).
    is_native = machine.shutdown_method.strip().lower() == "native"

    if args.dry_run:
        if not is_native:
            print(f"[dry-run] disarm remote: skip (declared {machine.shutdown_method})")
            print(f"[dry-run] firewall: skip (declared {machine.shutdown_method})")
        else:
            disarm = "skip (--keep-remote)" if args.keep_remote else machine.ssh
            fw = "skip (--no-firewall)" if args.no_firewall else saddrs
            print(f"[dry-run] disarm remote: {disarm}")
            print(f"[dry-run] firewall: {fw}")
        print(f"[dry-run] persist: drop {machine.name} (config written LAST)")
        return 0

    # 1) disarm remote (unless --keep-remote, or the record is not native)
    if is_native and not args.keep_remote:
        # BL-C1. Checked HERE rather than at the top of the command deliberately:
        # removing the record is the remedy for a bad alias, so refusing the whole
        # verb would trap the operator. Only the step that puts the alias in an ssh
        # argv is refused; --keep-remote then completes the local half.
        if machine.ssh.strip() and not _valid_ssh_alias(machine.ssh):
            LOG.error(
                "monitor remove: config ssh alias %r for %s is not a plain host or "
                "ssh_config alias; refusing to run the remote disarm. Fix 'ssh' in the "
                "config, or re-run with --keep-remote and disarm that box by hand.",
                machine.ssh,
                machine.name,
            )
            return 2
        rc, _out, err = _monitor_run_ssh(machine.ssh, _REMOTE_DISARM, None)
        if rc != 0:
            LOG.error("monitor remove: remote disarm failed: %s", err)
            return 3

    # 2) firewall: rewrite the saddr set from survivors (unless --no-firewall)
    if is_native and not args.no_firewall:
        restart = (lambda: None) if args.no_restart_bouncer else _monitor_restart_bouncer
        rc, _out, err = nutclient.apply_nft(
            _NFT_PATH, saddrs, _monitor_run_nft, restart, reload_path=_NFT_RELOAD_PATH
        )
        if rc != 0:
            LOG.error("monitor remove: firewall reload failed: %s", err)
            return 4

    # 3) persist config LAST (so a firewall failure leaves config unchanged)
    _monitor_persist(cfg_path, [m.to_dict() for m in survivors])
    print(f"removed {machine.name}")
    return 0


def _valid_ip(value: str) -> bool:
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _strict_positive_baud(value: str | None) -> int | None:
    """Parse a DECLARED baud, or ``None``. No silent fallback (P2-08, T-02-12).

    ``argparse(type=int)`` would raise ``SystemExit(2)`` with argparse's own
    message; the operator needs to be told that the baud is theirs to declare and
    what the live console runs at, so the parse happens here instead. ``"9600.5"``
    and ``"fast"`` are rejected rather than truncated, and ``0`` is rejected
    because POSIX ``B0`` means *hang up the line*.
    """
    if value is None:
        return None
    try:
        baud = int(value.strip())
    except ValueError:
        return None
    return baud if baud > 0 else None


def _parse_route_src(route_out: str) -> str | None:
    """Extract the ``src`` field from ``ip -o route get`` output. PURE.

    ``ip route get`` prints the address the kernel would source a packet from on
    the path to a destination, e.g. ``… src 192.168.1.114 uid 0``. That is the
    exact source IP upsd sees — unlike ``$SSH_CONNECTION`` field 1, which for a
    box reached over a WAN/NAT path is the GATEWAY, not the machine's LAN IP
    (the live enrollment bug: mt resolved to 192.168.1.1, not 192.168.1.114).
    Returns the validated literal or ``None``.
    """
    tokens = route_out.split()
    for i, tok in enumerate(tokens):
        if tok == "src" and i + 1 < len(tokens):
            candidate = tokens[i + 1]
            return candidate if _valid_ip(candidate) else None
    return None


def _resolve_remote_ip(alias: str, explicit: str | None, primary_ip: str) -> str | None:
    """Resolve the source IP the secondary uses to reach the primary.

    Order: explicit ``--ip`` overrides everything → ``ip -o route get
    <primary_ip>`` run ON THE REMOTE (its ``src`` is the address upsd actually
    sees) → ``$SSH_CONNECTION`` field 1 as a last-resort fallback. Returns a
    validated IP literal or ``None`` when nothing usable resolves.

    The route probe replaces trusting ``$SSH_CONNECTION`` field 1 outright:
    over a WAN/NAT SSH path that field is the gateway, so a route lookup toward
    the primary is the only reliable way to learn the machine's real LAN IP.
    """
    if explicit:
        return explicit.strip() if _valid_ip(explicit) else None
    if _valid_ip(primary_ip):
        rc, out, _err = _monitor_run_ssh(alias, f"ip -o route get {primary_ip}", None)
        if rc == 0:
            src = _parse_route_src(out)
            if src is not None:
                return src
    rc, out, _err = _monitor_run_ssh(alias, "echo $SSH_CONNECTION", None)
    if rc == 0:
        fields = out.split()
        if fields and _valid_ip(fields[0]):
            return fields[0]
    return None


_NATIVE_SHUTDOWN_CMD = "/sbin/shutdown -h now"
# NEW-2. A push runs as the ssh user or the far end's auto-login user, not as root
# — and over serial a permission failure is SILENT (the bytes land, the write
# returns 0, and success is reported for a box that stayed up). Native keeps the
# unescalated form because upsmon runs SHUTDOWNCMD as root.
_PUSH_SHUTDOWN_CMD = "sudo /sbin/shutdown -h now"
_PUSH_METHODS = ("serial", "ssh", "none")


def _resolve_shutdown_cmd(method: str, explicit: str | None) -> str:
    """The operator's ``--shutdown-cmd``, else the default for this METHOD (NEW-2)."""
    if explicit is not None:
        return explicit
    return _NATIVE_SHUTDOWN_CMD if method == "native" else _PUSH_SHUTDOWN_CMD


def _valid_ssh_alias(alias: str) -> bool:
    """T-02-10: a plain host or ``ssh_config`` alias, not an option or a shell string.

    Shares its regex with ``Config.load``'s check so the CLI cannot accept a value
    the loader would disarm. The alias becomes an argv element in an unattended
    ``ssh`` at outage time once the push projection is live, where a leading ``-``
    is read as an option.
    """
    return valid_ssh_alias(alias)


def _monitor_add(cfg: Config, cfg_path: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator monitor add")
    parser.add_argument("name")
    parser.add_argument(
        "--method",
        default="native",
        choices=("none", "native", "serial", "ssh"),
        help="shutdown authority for this machine (default native, Phase-1 behaviour)",
    )
    parser.add_argument("--ssh", help="ssh_config alias (required for --method native/ssh)")
    parser.add_argument("--ups", help="NUT UPS name the machine draws from")
    parser.add_argument("--ip", help="explicit source IP (skips SSH_CONNECTION probe)")
    parser.add_argument("--os", default="auto", choices=("auto", "arch", "ubuntu", "debian"))
    parser.add_argument("--powervalue", type=int, default=1, choices=(0, 1))
    # Default resolved from --method, not fixed here: a push needs the escalated
    # form and native needs the bare one (NEW-2).
    parser.add_argument("--shutdown-cmd", default=None)
    parser.add_argument("--serial-device", default="", help="--method serial: console under /dev/")
    parser.add_argument(
        "--serial-baud",
        default=None,
        help="--method serial: the console's DECLARED baud; never assumed (P2-08)",
    )
    parser.add_argument("--primary-ip", help="override the LAN address in the MONITOR line")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, mutate nothing")
    parser.add_argument("--no-firewall", action="store_true", help="skip the nft open")
    parser.add_argument("--no-restart-bouncer", action="store_true", help="skip bouncer restart")
    # LO-C6: the help used to read "override refuse-on-existing guards" (plural),
    # describing the wider authorisation this flag carried BEFORE T-02-54 split it.
    # It now gates exactly one thing and — deliberately — cannot open the native
    # transition guard or authorise anything on a remote host.
    parser.add_argument(
        "--force",
        action="store_true",
        help="override the LOCAL dual-regime double-shutdown refusal, and nothing else",
    )
    # T-02-54: --force used to authorise BOTH the local guards above AND clobbering
    # a third host's /etc/nut/upsmon.conf, so an operator following the dual-regime
    # error message's own instruction also authorised the remote overwrite. Split.
    parser.add_argument(
        "--force-remote-config",
        action="store_true",
        help="overwrite an UNMARKED remote /etc/nut/upsmon.conf or demote its MODE",
    )
    args = parser.parse_args(argv)

    method: str = args.method
    shutdown_cmd = _resolve_shutdown_cmd(method, args.shutdown_cmd)

    # 1. up-front arg validation (rc 2), method-independent first.
    # LO-C4: shares `render_upsmon_conf`'s own predicate rather than re-spelling half
    # of it. The old `'"' in shutdown_cmd` rejected the quote and passed a NEWLINE, so
    # `--shutdown-cmd $'sudo /sbin/shutdown -h now\nNOTIFYCMD /tmp/x'` closed the
    # SHUTDOWNCMD line and installed a second directive in the SECONDARY's
    # /etc/nut/upsmon.conf. One predicate, every sink — as with `valid_ssh_alias`.
    if not nutclient.valid_shutdown_cmd(shutdown_cmd):
        LOG.error(
            "monitor add: --shutdown-cmd %r must not contain a double-quote or any "
            "control character. It is emitted as a single SHUTDOWNCMD \"<cmd>\" line in "
            "the secondary's /etc/nut/upsmon.conf, so a newline ends that directive and "
            "everything after it becomes a further upsmon directive on that machine.",
            shutdown_cmd,
        )
        return 2
    if args.ip and not _valid_ip(args.ip):
        LOG.error("monitor add: --ip %r is not a valid IP literal", args.ip)
        return 2
    if args.ups is not None and not nutclient.valid_nut_name(args.ups):
        LOG.error("monitor add: --ups %r is not a valid NUT UPS name", args.ups)
        return 2
    if args.primary_ip and not _valid_ip(args.primary_ip):
        LOG.error("monitor add: --primary-ip %r is not a valid IP literal", args.primary_ip)
        return 2
    if args.ssh is not None and not _valid_ssh_alias(args.ssh):
        LOG.error(
            "monitor add: --ssh %r is not a plain host or ssh_config alias. That value "
            "becomes an argv element in an unattended ssh at outage time, where a leading "
            "'-' is read as an option and shell metacharacters are carried verbatim.",
            args.ssh,
        )
        return 2

    # 2. method-specific required args (rc 2). `none` needs nothing at all.
    if method in ("native", "ssh") and not args.ssh:
        LOG.error("monitor add: --method %s requires --ssh <alias>", method)
        return 2
    if method != "none" and not args.ups:
        LOG.error(
            "monitor add: --method %s requires --ups <name> — a push is projected per UPS on "
            "that UPS's low-battery event, so a machine with no UPS can never fire",
            method,
        )
        return 2
    if method == "serial":
        device = args.serial_device.strip()
        if not device:
            LOG.error("monitor add: --method serial requires --serial-device (a path under /dev/)")
            return 2
        # ME-C3: the loader's own rule, applied at the argparse boundary. `--serial-baud`
        # was strictly validated here and `--serial-device` was checked only for
        # emptiness, so `--serial-device /etc/passwd` was accepted with a success
        # message for a record `Config.load` disarms on the very next read. That is
        # exactly the asymmetry the `_SSH_ALIAS_RE` comment at the top of this file
        # forbids — a value the CLI accepts that the loader would disarm — one field
        # over. Same predicate, same reason: the serial writer opens the device "wb".
        if not device.startswith("/dev/") or device == "/dev/":
            LOG.error(
                "monitor add: --serial-device %r is not an absolute path under /dev/. "
                "The serial writer opens it with mode 'wb', which TRUNCATES a regular "
                "file — so a typo would destroy that file and still report success. "
                "Config.load applies this same rule and would disarm the record, so "
                "accepting it here would only report success for a machine that can "
                "never shut down.",
                args.serial_device,
            )
            return 2
        baud = _strict_positive_baud(args.serial_baud)
        if baud is None:
            LOG.error(
                "monitor add: --method serial requires a positive integer --serial-baud "
                "(got %r). The baud is the operator's to declare and is never assumed: a "
                "mismatch writes garbage down the line and still returns rc 0, a silent "
                "no-shutdown. The live console on this site is 9600.",
                args.serial_baud,
            )
            return 2
    else:
        baud = None

    ups_name: str = args.ups or ""
    ssh_alias: str = args.ssh or ""
    target = args.name.strip().lower()
    others = tuple(m for m in cfg.monitored_machines if m.name.strip().lower() != target)
    existing = _monitor_find(cfg, args.name)

    # BL-C2: the same ambiguity `monitor remove` refuses (T-02-48), for the same two
    # reasons — `_monitor_find` is first-wins, so the transition guard below would
    # inspect an ARBITRARY one of the duplicates, while `others` filters out EVERY
    # match, so the persist deletes all of them and appends one. Together that lets
    # `monitor add <name> --method ssh` silently delete a live NATIVE record whose
    # name differs only in case: no remote NUT teardown, no nft revoke, and the box
    # is then declared a push target while its own upsmon is still armed — the exact
    # native->push double shutdown the transition guard exists to refuse, reached
    # without the guard ever firing.
    # Counted with the SAME comparison `others` partitions on, so the guard covers
    # exactly the set the persist would delete — not a near-miss of it.
    duplicates = [m for m in cfg.monitored_machines if m.name.strip().lower() == target]
    if len(duplicates) > 1:
        LOG.error(
            "monitor add: %d records share the name %r (case-insensitively), so which one "
            "this would re-enrol — and which the persist would delete — is a guess. All of "
            "them are disarmed at load. De-duplicate monitored_machines by hand, then "
            "re-run this command.",
            len(duplicates),
            args.name,
        )
        return 2

    # 3. Transition guard, on the DECLARED method (T-02-23). `monitor remove` is the
    # ONLY thing that actually disarms a native authority — it runs the real remote
    # NUT teardown. Refusing beats an implicit cross-host disarm. Reading the
    # declaration is load-bearing: nothing rewrites that field, so a load-time
    # degrade can never open this guard.
    #
    # LO-C2: normalised, like every other method comparison in this file
    # (`_probe_secondary_reason`, `_monitor_remove`, `_rehearsal_target`,
    # `_survivor_saddrs`). The bare `==` happened to hold only because `from_dict`
    # lower-cases the field — but `MonitoredMachine` itself makes no such promise and
    # `_monitor_add` constructs one directly a few lines below, so the guard's
    # correctness rested on a property of a DIFFERENT constructor. A `"Native"` that
    # reaches this comparison unnormalised opens the native->push hole outright.
    if (
        existing is not None
        and existing.shutdown_method.strip().lower() == "native"
        and method != "native"
    ):
        LOG.error(
            "monitor add: %s is already enrolled as a NATIVE secondary. Changing it to "
            "%r here would leave that box's remote upsmon armed AND add a second "
            "authority. Run 'monitor remove %s' first — that is what runs the real "
            "remote NUT teardown — then re-add it with --method %s.",
            args.name,
            method,
            args.name,
            method,
        )
        return 2

    # 4. dual-regime --force gate (rc 2 without --force). The candidate carries the
    # RESOLVED method: the refusal text reports it, and a candidate defaulting to
    # `none` would send the operator to fix the wrong thing (BL-02).
    candidate = MonitoredMachine(
        name=args.name,
        ssh=ssh_alias,
        ups=ups_name,
        powervalue=args.powervalue,
        os=args.os,
        shutdown_cmd=shutdown_cmd,
        shutdown_method=method,
        serial_device=args.serial_device,
        serial_baud=baud,
    )
    conflicts = dual_regime_conflicts((*others, candidate), cfg.upses)
    if conflicts and not args.force:
        LOG.error(
            "monitor add: %s is both an enrolled machine (shutdown_method=%r) and an "
            "enabled shutdown_target on its UPS (double-shutdown risk) — pass --force to "
            "override",
            args.name,
            method,
        )
        return 2

    # 5. serial/ssh/none are RECORD-ONLY. This branch is deliberately ABOVE the
    # secondary-password lookup and the whole native bootstrap: a machine with no
    # UPS enrollment of its own must not get upsd.users, a LISTEN, an nft opening or
    # a remote upsmon (T-02-11, P2-02/P2-04).
    if method != "native":
        entry = MonitoredMachine(
            name=args.name,
            ssh=ssh_alias,
            ups=ups_name,
            powervalue=args.powervalue,
            os=args.os,
            shutdown_cmd=shutdown_cmd,
            ip="",  # written ONLY by the native enrollment path
            shutdown_method=method,
            serial_device=args.serial_device,
            serial_baud=baud,
            raw=dict(existing.raw) if existing is not None else {},
        )
        if args.dry_run:
            transport = {
                "serial": f"serial {args.serial_device} @ {baud} baud",
                "ssh": f"ssh {ssh_alias}",
                "none": "no active shutdown authority",
            }[method]
            print(f"[dry-run] record-only add ({method}): {transport}")
            print(f"[dry-run] shutdown_cmd: {shutdown_cmd}")
            print("[dry-run] skipped: upsd.users, LISTEN, nft, remote upsmon (native-only)")
            print(f"[dry-run] persist: {args.name} (config written LAST)")
            return 0
        _monitor_persist(cfg_path, [*(m.to_dict() for m in others), entry.to_dict()])
        print(f"recorded {args.name} (shutdown_method={method})")
        return 0

    # 6. password from env ONLY — never invent, never store (rc 2 if absent)
    password = os.environ.get(_SECRET_ENV, "")
    if not password:
        LOG.error("monitor add: %s not set in the environment", _SECRET_ENV)
        return 2

    ns = cfg.nut_server

    # LO-C1: a dry run prints the plan and TOUCHES NOTHING — the remote included.
    # This print used to sit BELOW steps 7 and 8, i.e. below two SSH round-trips into
    # the machine (`_resolve_remote_ip`) and a local `ip -o route get`
    # (`_resolve_primary_ip`). Nothing was mutated, so the flag's own help was true,
    # but "mutate nothing" is not "touch nothing" — and this is the mechanism behind
    # 02-03's own reported live-contact exception. Only what the operator supplied on
    # the command line is shown; everything the real run would have to ASK a host for
    # is rendered unresolved rather than resolved.
    if args.dry_run:
        preview_ip = args.ip.strip() if args.ip else ""
        preview_primary = args.primary_ip.strip() if args.primary_ip else ""
        unresolved = "<unresolved — the real run probes for it>"
        saddrs = _survivor_saddrs((*others, dataclasses.replace(candidate, ip=preview_ip)))
        print(f"[dry-run] resolve ip: {preview_ip or unresolved}")
        print(
            f"[dry-run] bootstrap primary: LISTEN {preview_primary or unresolved}, "
            f"user {ns.secondary_user} (password <redacted>), nft "
            + ("skip" if args.no_firewall else str(saddrs))
        )
        print(f"[dry-run] remote bootstrap: {ssh_alias} detect/install/write/enable")
        print("[dry-run] verify (deep) then persist entry (no password)")
        print("[dry-run] contacted: nothing — no ssh, no route probe, no local command")
        return 0

    # 7. resolve the remote source IP (validated literal). When --primary-ip is
    # given, the remote `ip route get <primary>` learns the machine's real LAN
    # source; otherwise the resolver falls back to $SSH_CONNECTION field 1.
    ip = _resolve_remote_ip(ssh_alias, args.ip, args.primary_ip or "")
    if not ip:
        LOG.error("monitor add: could not resolve a valid source IP for %s", ssh_alias)
        return 2

    # 8. resolve the primary's LAN IP the secondary's MONITOR line points at.
    # Without --primary-ip, auto-detect it locally by routing toward the machine
    # (the primary's src on that path) rather than silently defaulting to
    # localhost, which would leave upsd bound to 127.0.0.1 and enrollment failing
    # at verify with no clear cause (the live bug).
    primary = _resolve_primary_ip(cfg, args.primary_ip, ip)
    if not primary:
        LOG.error(
            "monitor add: could not auto-detect the primary's LAN IP toward %s — "
            "pass --primary-ip or add a LAN LISTEN to nut_server.listen",
            ip,
        )
        return 2
    # Carry a pre-existing entry's raw dict so re-adding (idempotent replace)
    # preserves that machine's operator-authored keys (e.g. a _comment).
    # BL-02/IB-03: shutdown_method is passed EXPLICITLY. `to_dict` always emits the
    # field, so an omission here persists the dataclass default `none` — which the
    # explicit-value branch in `from_dict` then honours forever, burning the
    # has-`ups`⇒`native` derivation. The re-enroll path is the live instance: it
    # re-arms the remote upsmon in step 10 while declaring the box opted out.
    entry = MonitoredMachine(
        name=args.name,
        ssh=ssh_alias,
        ups=ups_name,
        powervalue=args.powervalue,
        os=args.os,
        shutdown_cmd=shutdown_cmd,
        ip=ip,
        shutdown_method="native",
        raw=dict(existing.raw) if existing is not None else {},
    )
    saddrs = _survivor_saddrs((*others, entry))

    # 9. bootstrap primary WITH the real password (upsd.users + LISTEN + restart + nft)
    is_root = os.geteuid() == 0
    restart = (lambda: None) if args.no_restart_bouncer else _monitor_restart_bouncer
    try:
        rc, log = nutclient.bootstrap_primary(
            lan_ip=primary,
            port=ns.port,
            user=ns.secondary_user,
            password=password,
            # --no-firewall must SKIP the nft step, not pass []: an empty saddr
            # set tears down the whole managed table (revoking every enrolled
            # secondary). Only the genuine last-removed path in `remove` clears it.
            saddrs=None if args.no_firewall else saddrs,
            upsd_conf_path=_UPSD_CONF_PATH,
            upsd_users_path=_UPSD_USERS_PATH,
            nft_path=_NFT_PATH,
            run_local=_monitor_run_local,
            run_nft=_monitor_run_nft,
            restart_bouncer=restart,
            is_root=is_root,
            nft_reload_path=_NFT_RELOAD_PATH,
        )
    except OSError as exc:
        # A boundary failure (read/write/mkdir on /etc) must surface as a clean
        # exit code, not an uncaught traceback that leaves the operator guessing
        # how far the half-applied primary got.
        LOG.error("monitor add: primary bootstrap failed at a filesystem boundary: %s", exc)
        return 4
    if rc != 0:
        for line in log:
            LOG.error("monitor add: %s", line)
        return 4

    # 10. remote bootstrap: detect → install → write config (password on stdin) → enable
    os_kind = args.os if args.os != "auto" else nutclient.detect_os(ssh_alias, _monitor_run_ssh)
    rc, _o, _e = nutclient.install_nut_client(ssh_alias, os_kind, _monitor_run_ssh)
    if rc != 0:
        LOG.error("monitor add: remote nut-client install failed")
        return 3
    upsmon_text = nutclient.render_upsmon_conf(
        ups_name,
        primary,
        ns.secondary_user,
        password,
        shutdown_cmd,
        powervalue=args.powervalue,
    )
    rc, reason = nutclient.write_remote_nut_config(
        ssh_alias,
        upsmon_text,
        nutclient.render_nut_conf(),
        _REMOTE_UPSMON_PATH,
        _REMOTE_NUT_CONF_PATH,
        _monitor_run_ssh,
        # T-02-54: the REMOTE overwrite takes its own authorisation. `--force`
        # clears the local guards its own error message names, and nothing else.
        force=args.force_remote_config,
    )
    if rc != 0:
        LOG.error("monitor add: remote config write refused/failed: %s", reason)
        return 3
    rc, _o, _e = nutclient.enable_nut_monitor(ssh_alias, _monitor_run_ssh)
    if rc != 0:
        LOG.error("monitor add: remote nut-monitor enable failed")
        return 3

    # 11. deep verify (catches a wrong/placeholder password — plain upsc is unauth)
    ok, detail = nutclient.verify_secondary(
        ssh_alias, ups_name, primary, _monitor_run_ssh, timeout=10, deep=True
    )
    if not ok:
        LOG.error("monitor add: verification failed: %s", detail)
        return 5

    # 12. persist by name (idempotent), no password, unknown keys preserved
    kept = [m.to_dict() for m in others]
    kept.append(entry.to_dict())
    _monitor_persist(cfg_path, kept)
    print(f"enrolled {args.name} ({ip}) on {ups_name}")
    return 0


# ---- remote-shutdown preview + shutdown rehearse ----------------------------


def _resolved_targets(ups: UpsConfig, deps: Deps) -> list[ShutdownTarget]:
    """Every target this UPS would consider: configured, plus machine projections."""
    return [*ups.shutdown_targets, *_machine_targets(ups, deps.monitored_machines)]


def _preview_verdict(
    ups: UpsConfig, state: UpsState, deps: Deps, target: ShutdownTarget, snap: UpsSnapshot
) -> tuple[bool, str]:
    """The verdict ``_run_shutdown_targets`` would reach for this target, right now.

    A-2 decision 1: the preview REPORTS the gate; it does not bypass it. The
    effective-enabled filter is applied first because that is the order the firing
    path applies it in — ``_target_should_fire`` never looks at the target's own
    flag, so delegating straight to it would annotate a disabled target with the
    UPS's charge state and imply it was merely waiting for a low battery.
    """
    if not target.effective_enabled:
        if any(is_disarming(n) for n in target.load_notices):
            return False, "disarmed at load — see 'monitor list'"
        return False, "target not enabled"
    return _target_should_fire(ups, state, deps, target, snap)


def _cmd_remote_shutdown(argv: list[str]) -> int:
    """``remote-shutdown [ups] [--dry-run]`` — the Layer-2 entry point.

    This is a TOP-LEVEL verb, not the implicit NUT event route: ``main`` used to
    fall an unknown mode through to ``_cmd_event``, where ``remote-shutdown``
    (hyphen) matched no handler and silently did nothing.
    """
    parser = argparse.ArgumentParser(prog="ups-orchestrator remote-shutdown")
    parser.add_argument("ups", nargs="?", help="UPS name (default: every configured UPS)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print every resolved target with its CURRENT gate verdict; touch nothing",
    )
    args = parser.parse_args(argv)

    if not args.dry_run:
        return _cmd_event("remote_shutdown", _resolve_ups_name(args.ups))

    cfg = _load_config()
    if cfg is None:
        return 1
    deps = _build_deps(cfg, dry_run=True)
    store = StateStore(_state_path())
    names = [args.ups] if args.ups else list(cfg.upses)
    for name in names:
        ups = cfg.ups(name)
        if ups is None:
            LOG.warning("remote-shutdown: unconfigured UPS %r — skipping", name)
            continue
        snap = deps.read_snapshot(ups.name)
        state = store.get(ups.name)
        print(f"{ups.label} ({ups.name}) — status {snap.status or 'unknown'}")
        targets = _resolved_targets(ups, deps)
        if not targets:
            print("  (no shutdown targets resolved)")
            continue
        for t in targets:
            should, reason = _preview_verdict(ups, state, deps, t, snap)
            print(
                f"  {t.name}\t{_target_location(t)}\t{t.cmd}\t"
                f"would fire: {'yes' if should else 'no'} — {reason}"
            )
    # `store.save()` is deliberately NOT called: a preview reads state and writes none.
    return 0


# T-02-24. Hard-coded, quote-free (the tag is what `journalctl -t ups-orchestrator`
# greps, and `monitor add` rejects a double-quote in a shutdown command). The
# rehearsal's safety property is that this command CANNOT halt a box — not that a
# policy flag happens to be set.
_REHEARSAL_CMD = "logger -t ups-orchestrator PHASE2_REHEARSAL"


def _rehearsal_target(cfg: Config, name: str) -> ShutdownTarget | None:
    """Build an ephemeral target for ``name`` carrying the REHEARSAL command.

    The machine's persisted ``shutdown_cmd`` is never read, so a crash mid-rehearsal
    can leave neither a no-op armed nor the real command queued. Resolution order is
    monitored machine first, then a configured legacy target of that name.

    The machine branch reads the DECLARED method on purpose. Rehearsal is a
    diagnostic for the cable, not a shutdown: refusing to rehearse a machine a load
    degrade disarmed would withhold the test at exactly the moment an operator is
    trying to work out what is wrong with it.
    """
    machine = _monitor_find(cfg, name)
    if machine is not None:
        method = machine.shutdown_method.strip().lower()
        if method == "serial":
            return ShutdownTarget(
                name=machine.name,
                kind="serial",
                enabled=True,
                device=machine.serial_device,
                baud=machine.serial_baud,
                cmd=_REHEARSAL_CMD,
            )
        if method == "ssh":
            return ShutdownTarget(
                name=machine.name,
                kind="remote",
                enabled=True,
                host=machine.ssh,
                cmd=_REHEARSAL_CMD,
            )
        return None  # native/none have no push transport to rehearse
    for ups in cfg.upses.values():
        for t in ups.shutdown_targets:
            if t.name.strip().casefold() == name.strip().casefold():
                return dataclasses.replace(t, enabled=True, cmd=_REHEARSAL_CMD)
    return None


def _cmd_shutdown(argv: list[str]) -> int:
    if not argv or argv[0] != "rehearse":
        LOG.error("usage: ups-orchestrator shutdown rehearse <machine>")
        return 2
    parser = argparse.ArgumentParser(prog="ups-orchestrator shutdown rehearse")
    parser.add_argument("name", help="the machine to rehearse (never a sweep)")
    args = parser.parse_args(argv[1:])

    cfg = _load_config()
    if cfg is None:
        return 1
    target = _rehearsal_target(cfg, args.name)
    if target is None:
        LOG.error(
            "shutdown rehearse: %r has no serial or ssh transport to rehearse. Only a "
            "push transport pushes bytes; a native secondary halts itself on this "
            "primary's FSD and a 'none' record has no authority at all.",
            args.name,
        )
        return 2
    if target.is_local:
        LOG.error(
            "shutdown rehearse: %r is a LOCAL target — this host. There is no cable to "
            "rehearse, and the command would run here.",
            args.name,
        )
        return 2
    if target.is_serial:
        if not target.device.strip():
            LOG.error("shutdown rehearse: %r has no serial device recorded", args.name)
            return 2
        if target.baud is None or target.baud <= 0:
            LOG.error(
                "shutdown rehearse: %r has no usable declared baud (the live console "
                "here is 9600); refusing to guess one",
                args.name,
            )
            return 2
    # HI-C3: validate the destination that is actually BUILT, not one of its halves.
    # `ssh_dest` returns f"{user}@{host}" whenever user is set, so checking `host`
    # alone left a legacy `user` of "-oProxyCommand=…" reaching the argv — and
    # `_rehearsal_target` force-enables the target, so a DISABLED legacy target is a
    # live ssh sink from this verb. `_SSH_ALIAS_RE` already permits the user@host
    # shape, so this is strictly tighter with no false refusals.
    elif not _valid_ssh_alias(ssh_dest(target)):
        LOG.error(
            "shutdown rehearse: %r has no usable ssh destination (%r)",
            args.name,
            ssh_dest(target),
        )
        return 2

    deps = _build_deps(cfg)
    where = _target_location(target)
    # Print the exact transport parameters BEFORE sending, so an operator can abort
    # a wrong device by reading the line rather than by reading the outcome.
    print(f"rehearse {target.name} via {where}")
    print(f"  command: {_REHEARSAL_CMD}   (hard-coded; the recorded shutdown_cmd is not read)")
    if target.is_serial:
        print(f"  device:  {target.device} @ {target.baud} baud")
    rc, _out, err = (
        deps.serial_shutdown(target) if target.is_serial else deps.ssh_shutdown(target)
    )
    if rc != 0:
        print(f"  FAIL — rc={rc} {err.strip() or '(no stderr)'}")
        return 1
    print(f"  OK — look for PHASE2_REHEARSAL on {target.name} "
          f"(journalctl -t ups-orchestrator)")
    return 0


def _cmd_monitor(argv: list[str]) -> int:
    if not argv:
        LOG.error("usage: ups-orchestrator monitor <add|list|verify|remove> [...]")
        return 2
    action, rest = argv[0], argv[1:]
    cfg = _load_config()
    if cfg is None:
        return 1
    cfg_path = _config_path()
    if action == "list":
        return _monitor_list(cfg)
    if action == "verify":
        return _monitor_verify(cfg, rest)
    if action == "remove":
        return _monitor_remove(cfg, cfg_path, rest)
    if action == "add":
        return _monitor_add(cfg, cfg_path, rest)
    LOG.error("monitor: unknown action %r", action)
    return 2


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
            "power-dashboard|webui|control|monitor|remote-shutdown|shutdown|"
            "notify-test|logs> [...]"
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
    if mode == "control":
        return _cmd_control(args[1:])
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
    if mode == "monitor":
        return _cmd_monitor(args[1:])
    # Both of these MUST precede the fall-through: an unknown mode becomes a NUT
    # event name, where "remote-shutdown" (hyphen) matches no handler and silently
    # succeeds. The NUT event route keeps its own underscore spelling.
    if mode == "remote-shutdown":
        return _cmd_remote_shutdown(args[1:])
    if mode == "shutdown":
        return _cmd_shutdown(args[1:])
    if mode == "watch":
        return _cmd_watch()
    return _cmd_event(mode, _resolve_ups_name(args[1] if len(args) > 1 else None))


if __name__ == "__main__":
    sys.exit(main())
