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
    ups-orchestrator notify-test         # send a Discord delivery test
    ups-orchestrator logs                # tail local JSONL logs

NUT exposes the active UPS to event handlers via the ``UPSNAME`` environment
variable. The process **always exits 0** on the event path so a misbehaving
handler never wedges NUT's pipeline; failures are logged.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from types import FrameType

from ups_orchestrator import audit, nutclient, recorder, report
from ups_orchestrator import status as status_view
from ups_orchestrator.config import Config, MonitoredMachine, UpsConfig, dual_regime_conflicts
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
        onbatt_notify_grace=cfg.onbatt_notify_grace_seconds,
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


def _monitor_primary_ip(cfg: Config, override: str | None) -> str:
    """First non-loopback LISTEN address (the LAN IP the secondary connects to)."""
    if override:
        return override
    for addr in cfg.nut_server.listen:
        if addr not in ("127.0.0.1", "::1", "localhost"):
            return addr
    return "127.0.0.1"


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
    """nft saddr union from monitored machines, empty IPs filtered out.

    An empty-ip survivor would render an invalid ``ip saddr { }`` and fail
    ``nft -f``; drop it (deduplicated, order-preserving).
    """
    out: list[str] = []
    for m in machines:
        ip = m.ip.strip()
        if ip and ip not in out:
            out.append(ip)
    return out


def _monitor_persist(cfg_path: Path, machines: list[dict[str, object]]) -> None:
    """Write monitored_machines back by mutating the RAW config dict, atomically.

    Unknown keys (e.g. a ``_comment``) are preserved because we round-trip the
    parsed JSON rather than a frozen Config. The write is temp+fsync+os.replace
    (state.py pattern) so a crash mid-write can't corrupt the file the watch
    service reads. The secondary password is never among the written fields.
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
        tmp_path.replace(cfg_path)
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


def _monitor_list(cfg: Config) -> int:
    if not cfg.monitored_machines:
        print("no machines enrolled")
        return 0
    for m in cfg.monitored_machines:
        backup = "backup:on" if m.backup.enabled else "backup:off"
        print(f"{m.name}\tssh={m.ssh}\tups={m.ups}\tos={m.os}\tip={m.ip or '-'}\t{backup}")
    return 0


def _monitor_verify(cfg: Config, argv: list[str]) -> int:
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
    # machine.ups is config-sourced (monitored_machines[].ups) and flows into a
    # remote shell string in verify_secondary; refuse a metachar-bearing value
    # here instead of executing it.
    if not nutclient.valid_nut_name(machine.ups):
        LOG.error("monitor verify: config UPS name %r for %s is invalid", machine.ups, machine.name)
        return 2
    primary = _monitor_primary_ip(cfg, args.primary_ip)
    ok, detail = nutclient.verify_secondary(
        machine.ssh,
        machine.ups,
        primary,
        _monitor_run_ssh,
        timeout=args.timeout,
        deep=args.deep,
    )
    print(f"{machine.name}: {'OK' if ok else 'FAIL'} — {detail}")
    return 0 if ok else 1


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

    machine = _monitor_find(cfg, args.name)
    if machine is None:
        LOG.error("monitor remove: unknown machine %r", args.name)
        return 2

    survivors = tuple(m for m in cfg.monitored_machines if m is not machine)
    saddrs = _survivor_saddrs(survivors)

    if args.dry_run:
        disarm = "skip (--keep-remote)" if args.keep_remote else machine.ssh
        fw = "skip (--no-firewall)" if args.no_firewall else saddrs
        print(f"[dry-run] disarm remote: {disarm}")
        print(f"[dry-run] firewall: {fw}")
        print(f"[dry-run] persist: drop {machine.name} (config written LAST)")
        return 0

    # 1) disarm remote (unless --keep-remote)
    if not args.keep_remote:
        rc, _out, err = _monitor_run_ssh(machine.ssh, _REMOTE_DISARM, None)
        if rc != 0:
            LOG.error("monitor remove: remote disarm failed: %s", err)
            return 3

    # 2) firewall: rewrite the saddr set from survivors (unless --no-firewall)
    if not args.no_firewall:
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


def _monitor_add(cfg: Config, cfg_path: Path, argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="ups-orchestrator monitor add")
    parser.add_argument("name")
    parser.add_argument("--ssh", required=True, help="ssh_config alias for the secondary")
    parser.add_argument("--ups", required=True, help="NUT UPS name the secondary draws from")
    parser.add_argument("--ip", help="explicit source IP (skips SSH_CONNECTION probe)")
    parser.add_argument("--os", default="auto", choices=("auto", "arch", "ubuntu", "debian"))
    parser.add_argument("--powervalue", type=int, default=1, choices=(0, 1))
    parser.add_argument("--shutdown-cmd", default="/sbin/shutdown -h now")
    parser.add_argument("--primary-ip", help="override the LAN address in the MONITOR line")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, mutate nothing")
    parser.add_argument("--no-firewall", action="store_true", help="skip the nft open")
    parser.add_argument("--no-restart-bouncer", action="store_true", help="skip bouncer restart")
    parser.add_argument("--force", action="store_true", help="override refuse-on-existing guards")
    args = parser.parse_args(argv)

    # 1. up-front arg validation (rc 2)
    if '"' in args.shutdown_cmd:
        LOG.error("monitor add: --shutdown-cmd must not contain a double-quote")
        return 2
    if args.ip and not _valid_ip(args.ip):
        LOG.error("monitor add: --ip %r is not a valid IP literal", args.ip)
        return 2
    if not nutclient.valid_nut_name(args.ups):
        LOG.error("monitor add: --ups %r is not a valid NUT UPS name", args.ups)
        return 2
    if args.primary_ip and not _valid_ip(args.primary_ip):
        LOG.error("monitor add: --primary-ip %r is not a valid IP literal", args.primary_ip)
        return 2

    # 2. password from env ONLY — never invent, never store (rc 2 if absent)
    password = os.environ.get(_SECRET_ENV, "")
    if not password:
        LOG.error("monitor add: %s not set in the environment", _SECRET_ENV)
        return 2

    # 3. dual-regime --force gate (rc 2 without --force)
    candidate = MonitoredMachine(
        name=args.name,
        ssh=args.ssh,
        ups=args.ups,
        powervalue=args.powervalue,
        os=args.os,
        shutdown_cmd=args.shutdown_cmd,
    )
    target = args.name.strip().lower()
    others = tuple(m for m in cfg.monitored_machines if m.name.strip().lower() != target)
    conflicts = dual_regime_conflicts((*others, candidate), cfg.upses)
    if conflicts and not args.force:
        LOG.error(
            "monitor add: %s is both an enrolled secondary and an enabled shutdown_target "
            "on its UPS (double-shutdown risk) — pass --force to override",
            args.name,
        )
        return 2

    # 4. resolve the remote source IP (validated literal). When --primary-ip is
    # given, the remote `ip route get <primary>` learns the machine's real LAN
    # source; otherwise the resolver falls back to $SSH_CONNECTION field 1.
    ip = _resolve_remote_ip(args.ssh, args.ip, args.primary_ip or "")
    if not ip:
        LOG.error("monitor add: could not resolve a valid source IP for %s", args.ssh)
        return 2

    # 5. resolve the primary's LAN IP the secondary's MONITOR line points at.
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
    ns = cfg.nut_server
    # Carry a pre-existing entry's raw dict so re-adding (idempotent replace)
    # preserves that machine's operator-authored keys (e.g. a _comment).
    existing = _monitor_find(cfg, args.name)
    entry = MonitoredMachine(
        name=args.name,
        ssh=args.ssh,
        ups=args.ups,
        powervalue=args.powervalue,
        os=args.os,
        shutdown_cmd=args.shutdown_cmd,
        ip=ip,
        raw=dict(existing.raw) if existing is not None else {},
    )
    saddrs = _survivor_saddrs((*others, entry))

    if args.dry_run:
        print(f"[dry-run] resolve ip: {ip}")
        print(
            f"[dry-run] bootstrap primary: LISTEN {primary}, user {ns.secondary_user} "
            "(password <redacted>), nft " + ("skip" if args.no_firewall else str(saddrs))
        )
        print(f"[dry-run] remote bootstrap: {args.ssh} detect/install/write/enable")
        print("[dry-run] verify (deep) then persist entry (no password)")
        return 0

    # 5. bootstrap primary WITH the real password (upsd.users + LISTEN + restart + nft)
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

    # 6. remote bootstrap: detect → install → write config (password on stdin) → enable
    os_kind = args.os if args.os != "auto" else nutclient.detect_os(args.ssh, _monitor_run_ssh)
    rc, _o, _e = nutclient.install_nut_client(args.ssh, os_kind, _monitor_run_ssh)
    if rc != 0:
        LOG.error("monitor add: remote nut-client install failed")
        return 3
    upsmon_text = nutclient.render_upsmon_conf(
        args.ups,
        primary,
        ns.secondary_user,
        password,
        args.shutdown_cmd,
        powervalue=args.powervalue,
    )
    rc, reason = nutclient.write_remote_nut_config(
        args.ssh,
        upsmon_text,
        nutclient.render_nut_conf(),
        _REMOTE_UPSMON_PATH,
        _REMOTE_NUT_CONF_PATH,
        _monitor_run_ssh,
        force=args.force,
    )
    if rc != 0:
        LOG.error("monitor add: remote config write refused/failed: %s", reason)
        return 3
    rc, _o, _e = nutclient.enable_nut_monitor(args.ssh, _monitor_run_ssh)
    if rc != 0:
        LOG.error("monitor add: remote nut-monitor enable failed")
        return 3

    # 7. deep verify (catches a wrong/placeholder password — plain upsc is unauth)
    ok, detail = nutclient.verify_secondary(
        args.ssh, args.ups, primary, _monitor_run_ssh, timeout=10, deep=True
    )
    if not ok:
        LOG.error("monitor add: verification failed: %s", detail)
        return 5

    # 8. persist by name (idempotent), no password, unknown keys preserved
    kept = [m.to_dict() for m in others]
    kept.append(entry.to_dict())
    _monitor_persist(cfg_path, kept)
    print(f"enrolled {args.name} ({ip}) on {args.ups}")
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
            "power-dashboard|webui|control|monitor|notify-test|logs> [...]"
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
    if mode == "watch":
        return _cmd_watch()
    return _cmd_event(mode, _resolve_ups_name(args[1] if len(args) > 1 else None))


if __name__ == "__main__":
    sys.exit(main())
