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
    ups-orchestrator maintenance begin|end|status
                                         # declare that power cuts are expected, so a
                                         # deliberate shutdown stops alerting as an outage
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
    canonical_ups_key,
    dual_regime_conflicts,
    is_disarming,
    safe_text,
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
from ups_orchestrator.state import (
    StateStore,
    UpsState,
    file_identity,
    write_json_preserving_metadata,
)

LOG = logging.getLogger("ups_orchestrator")

_BASE = Path(__file__).resolve().parent.parent.parent
_ETC_CONFIG = Path("/etc/ups-orchestrator/config.json")
_VAR_STATE = Path("/var/lib/ups-orchestrator/state.json")
_VAR_SAMPLES = Path("/var/lib/ups-orchestrator/samples.jsonl")
_VAR_EVENTS = Path("/var/lib/ups-orchestrator/events.jsonl")
_VAR_NOTIFICATIONS = Path("/var/lib/ups-orchestrator/notifications.jsonl")
_BOOT_AUDIT_MARKER = Path("/var/lib/ups-orchestrator/boot-audit.json")
_MAINTENANCE_MARKER = Path("/var/lib/ups-orchestrator/maintenance.json")


def _say(text: str) -> None:
    """Print one line with config-authored control characters neutralised (F5).

    Almost everything this module prints carries operator-authored free text — a
    machine name, a device path, an ssh alias, a UPS label, a load-time notice —
    and JSON can encode any control character as ``\\uXXXX``. ``mt\\x1b[2J\\x1b[H``
    clears the screen and homes the cursor: a machine name that erases the report
    naming it.

    Sanitising at the ONE print boundary rather than at each of ~50 call sites is
    the whole point. MED-06 fixed `status.py`, LO-C5 then had to fix the degrade
    banner in this file, and `monitor list` was still printing the same raw name
    four lines above the sanitised copy — three rounds of the same defect because
    the rule lived at call sites. `safe_text` on a literal is the identity, so
    routing every print through here costs nothing and cannot be forgotten.
    """
    print(safe_text(text))


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


def _maintenance_path() -> Path:
    env = os.environ.get("UPS_ORCH_MAINTENANCE_STATE")
    if env:
        return Path(env).expanduser()
    if _MAINTENANCE_MARKER.parent.is_dir():
        return _MAINTENANCE_MARKER
    return _BASE / "maintenance.json"


def _load_config() -> Config | None:
    """Load the config, or ``None`` on a fatal problem. Never raises.

    F4: ``ArithmeticError`` is a BACKSTOP, not the fix. `json.loads` maps
    `Infinity` and `1e400` to `float('inf')` and `int(inf)` raises `OverflowError`,
    which is an ArithmeticError and not a ValueError, so it walked past this clause
    as an uncaught traceback. The real fix is that `_as_int`/`_opt_int` now fall
    back instead of raising — a bad number in a monitoring knob must DEGRADE, not
    be fatal (RA-01). This clause is here so the next numeric escape is a clean
    error rather than a crash.
    """
    try:
        return Config.load(_config_path())
    except (OSError, ValueError, ArithmeticError) as exc:
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
            # F5: Discord renders these in a code-free embed field, and the
            # journal carries the same text — sanitise like every other sink.
            fields=[
                (safe_text(n.subject), f"[{n.severity}] {safe_text(n.message)}") for n in shown
            ],
        )
    )


def _cmd_event(event: str, ups_name: str | None, *, manual: bool = False) -> int:
    """Dispatch one NUT event. ``manual`` marks an operator-typed top-level verb.

    HI-C1: the two callers need different answers to "no UPS resolved". On the NUT
    event route (``manual=False``) a non-zero exit wedges ``upssched``'s pipeline,
    which is worse than the miss, so it stays 0. For a verb an operator typed
    (``remote-shutdown``), returning 0 having done nothing is the exact
    "silently did nothing" failure ``_cmd_remote_shutdown`` was written to fix,
    moved one argument to the left — so that case is loud.
    """
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
        LOG.error(
            "No UPS name provided for event %r (set arg or $UPSNAME); nothing was "
            "evaluated and no target was fired",
            event,
        )
        return 2 if manual else 0

    for name in targets:
        ups = cfg.ups(name) if name else None
        if ups is None:
            LOG.warning("Event %r for unconfigured UPS %r — ignoring", event, name)
            continue
        try:
            dispatch(event, ups, store.get(ups.name), deps)
        except Exception:  # noqa: BLE001 — never let a handler crash NUT
            # IF-05: this is caught, logged, and the function still returns 0 —
            # while an unloadable config a few lines above returns 1. The asymmetry
            # is real and INTENDED, and is recorded here rather than left to be
            # rediscovered: the ROADMAP invariant is that the event path always
            # exits 0 so a broken handler can never wedge NUT's own shutdown logic,
            # and it holds — `upssched` does not gate its pipe loop on CMDSCRIPT's
            # status, and `SHUTDOWNCMD` is invoked by `upsmon` directly rather than
            # through this path, so the rc 1 above is safe too.
            #
            # Worth stating plainly all the same: the LOUD failure is on the rare
            # one (an unparseable config, already visible in `monitor list`,
            # `status` and the webui), while a projector or transport exception at
            # outage time — the failure that actually matters — is silent-with-rc-0.
            # The traceback below is the only signal for that case, so
            # `journalctl -t ups-orchestrator` is part of the shutdown-path check,
            # not just a monitoring one.
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
        # IF-01: `state.json` has two writers — this process and the `nut` user's
        # upssched dispatcher (also an operator's `remote-shutdown`, which takes the
        # same event path). Without this, the tick's `shutdowns_sent` dedupe was made
        # against a map loaded when the process started, so a machine already pushed
        # by the event path was pushed a SECOND time here. Re-read before the
        # decision, not after.
        store.reload_if_changed()
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
        _say(report.render_text(note))
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
        _say(f"wrote {args.out} ({len(png)} bytes)")
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
    _say(baseline.render_text(cfg, stats, hours=max(1, args.hours)))
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
        _say(f"{ups.label}: {result.outcome} — {result.detail}")
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
        _say(f"{ups.label}: {args.action} ({command}) -> {detail}")
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


def _config_reloader(initial: Config) -> Callable[[], Config]:
    """A ``Config`` source for long-lived servers that re-reads on file change.

    IF-06: ``webui.serve`` captured the config once at startup, so its degrade
    banner — both the server-rendered one and the ``/api/status`` payload LO-07
    renders client-side — reported the state as of process start forever. An
    operator who fixed a degraded config and reloaded still saw the banner; a config
    that degraded afterwards showed a healthy dashboard indefinitely.

    Keyed on the file's identity rather than re-parsing per request, because
    ``Config.load`` LOGS every degrade notice and the dashboard polls every five
    seconds — an unconditional reload would put one copy of each notice in the
    journal per refresh, turning a fix for a stale banner into a log flood.

    A file that becomes unloadable keeps the last good config rather than taking the
    dashboard down: ``_load_config`` already logs the failure, and the operator's
    other surfaces (``status``, ``monitor list``) report it loudly.
    """
    path = _config_path()
    stamp = file_identity(path)
    current = initial

    def _reload() -> Config:
        nonlocal stamp, current
        latest = file_identity(path)
        if latest != stamp:
            stamp = latest
            fresh = _load_config()
            if fresh is not None:
                current = fresh
        return current

    return _reload


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
    webui.serve(
        cfg,
        _sample_path(),
        host=args.host,
        port=args.port,
        reload_config=_config_reloader(cfg),
    )
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
    _say(result.text)
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
        maintenance_path=_maintenance_path(),
    )
    LOG.info(
        "boot-audit: sent=%s power_loss=%d shutdown_evidence=%d (%s)",
        result.sent,
        result.power_loss_count,
        result.shutdown_action_count,
        result.text,
    )
    return 0


def _log_maintenance(event: str, *, until: float = 0.0, reason: str = "") -> None:
    """Record that outage alerting was suppressed or re-armed, and by whom.

    The marker file carries no actor and is overwritten by the next call, so
    without this there is no trace that alerting was ever silenced — for a
    control whose whole effect is that nothing gets delivered, that is the one
    record worth having (T-03-07). Never fatal: failing to log must not stop an
    operator closing a window.
    """
    actor = os.environ.get("SUDO_USER") or os.environ.get("USER") or f"uid:{os.getuid()}"
    try:
        append_event(
            _event_log_path(),
            event,
            ups_name="-",
            ups_label="maintenance",
            message=reason,
            data={"actor": actor, "until": until},
        )
    except OSError as exc:
        LOG.warning("maintenance: could not write the audit record: %s", exc)


def _cmd_maintenance(argv: list[str]) -> int:
    """Declare or clear a window in which abrupt power loss is expected.

    Exists because the boot audit cannot tell a deliberate plug-pull from an
    outage — both leave the host dead with every UPS reporting OL right up to
    the cut. Only the operator knows which it was, so they get to say so.
    """
    parser = argparse.ArgumentParser(prog="ups-orchestrator maintenance")
    parser.add_argument("action", choices=("begin", "end", "status"))
    parser.add_argument(
        "--hours", type=float, default=4.0, help="window length for 'begin' (default 4)"
    )
    parser.add_argument("--reason", default="", help="free text shown when an alert is suppressed")
    args = parser.parse_args(argv)

    path = _maintenance_path()
    now = time.time()

    max_hours = audit.MAX_MAINTENANCE_SECONDS / 3600.0
    if args.action == "begin":
        if args.hours <= 0:
            _say("maintenance: --hours must be greater than 0")
            return 2
        # Bounded because this suppresses outage alerting; read_maintenance
        # enforces the same cap, so asking for more would silently do nothing.
        if args.hours > max_hours:
            _say(f"maintenance: --hours must be at most {max_hours:g} (re-run to extend)")
            return 2
        until = now + args.hours * 3600.0
        audit.write_maintenance(path, until=until, reason=args.reason)
        _log_maintenance("maintenance_begin", until=until, reason=args.reason)
        _say(
            f"maintenance window open until "
            f"{time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(until))} "
            f"({args.hours:g}h){f' — {args.reason}' if args.reason else ''}"
        )
        _say("boot-audit power-loss alerts are suppressed until then.")
        return 0

    if args.action == "end":
        try:
            cleared = audit.clear_maintenance(path)
        except OSError as exc:
            # Never report this as "no window was open" — that reads as ARMED.
            LOG.error("maintenance: could not clear %s: %s", path, exc)
            _say(f"maintenance: FAILED to close the window ({exc}) — alerts are STILL suppressed")
            return 1
        if cleared:
            _log_maintenance("maintenance_end")
        _say("maintenance window closed" if cleared else "no window was open")
        return 0

    window = audit.read_maintenance(path, now=now)
    if not window.active:
        _say("maintenance: no window open — power-loss alerts are ARMED")
        return 0
    _say(
        f"maintenance: window open until "
        f"{time.strftime('%Y-%m-%d %H:%M %Z', time.localtime(window.until))}"
        f"{f' — {window.reason}' if window.reason else ''}"
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
        _say(report.render_text(note))
        return 0

    deps = _build_deps(cfg)
    result = deps.notifier.send(note)
    _say(
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
    _say(f"{args.kind}: {path}")
    for line in _tail(path, max(1, args.lines)):
        _say(line)
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
        if not _valid_saddr_ip(ip):
            LOG.warning(
                "monitor: ignoring machine %r's ip %r for the nft saddr set — it is not a "
                "valid IPv4 literal, and this value is loaded into the ruleset by nft -f. "
                "That set is rendered as 'ip saddr', nftables' IPv4 matcher, so a v6 "
                "address there makes nft reject the ENTIRE ruleset (F2). Fix 'ip' in the "
                "config, or re-run 'monitor add %s' to re-resolve it.",
                safe_text(m.name),
                safe_text(m.ip),
                safe_text(m.name),
            )
            continue
        if ip not in out:
            out.append(ip)
    return out


def _rewrite_nft_saddrs(
    saddrs: list[str], *, no_restart_bouncer: bool, verb: str
) -> tuple[int, str]:
    """Recompute the managed nft accept from ``saddrs``. LOCAL, idempotent (ME-C4).

    Shared by ``monitor remove`` and the record-only branch of ``monitor add``,
    which are the two commands that can make a machine STOP being a native
    secondary. Neither used to run it — ``remove`` skipped it for a non-native
    record and ``add --method ssh/serial/none`` returned before reaching it — so no
    command in the family revoked the ip a record shed, and the managed set kept
    granting upsd access to a host with no native record at all.

    Touches nothing but this box: ``apply_nft`` returns ``(0, "no change", "")``
    when the rendered text is unchanged, so the common case is a read and a compare.
    The caller decides whether a failure is fatal; it is always logged here.

    That contract only holds if this NEVER raises. ``apply_nft`` reports a reload
    failure through its return code, but the compare step READS ``_NFT_PATH``
    first, and that read is an ordinary ``OSError`` on a box where the file is
    root-owned — which is every box, for the record-only ``monitor add`` path that
    deliberately does not need root. Letting it propagate turned "recompute a
    local firewall file" into a hard rc 1 that also skipped the persist, so
    ``monitor add --method serial`` could not record a machine as a normal user at
    all. Caught and reported as a non-zero rc so the caller's own fatal/non-fatal
    decision is the one that applies.
    """
    restart = (lambda: None) if no_restart_bouncer else _monitor_restart_bouncer
    try:
        rc, _out, err = nutclient.apply_nft(
            _NFT_PATH, saddrs, _monitor_run_nft, restart, reload_path=_NFT_RELOAD_PATH
        )
    except OSError as exc:
        LOG.error("%s: firewall rewrite could not read or write %s: %s", verb, _NFT_PATH, exc)
        return 1, str(exc)
    if rc != 0:
        LOG.error("%s: firewall reload failed: %s", verb, err)
    return rc, err


def _monitor_persist(cfg_path: Path, machines: list[dict[str, object]]) -> None:
    """Write monitored_machines back by mutating the RAW config dict, atomically.

    Unknown keys (e.g. a ``_comment``) are preserved because we round-trip the
    parsed JSON rather than a frozen Config. The write goes through
    ``state.write_json_preserving_metadata`` (temp + fsync + metadata-preserving
    rename) so a crash mid-write can't corrupt the file the watch service reads, and
    the rename itself can't strip the mode/owner/ACL the installer gave the file
    (T-02-23). That helper is now the project's only writer: IF-02 and IF-08 were
    two sites that had grown their own copy of this dance and never got the T-02-23
    migration. The secondary password is never among the written fields.
    """
    raw = json.loads(cfg_path.read_text())
    raw["monitored_machines"] = machines
    write_json_preserving_metadata(cfg_path, raw, indent=2)


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
    _say("")
    _say("⚠ DEGRADED CONFIG — a shutdown authority was disarmed or flagged at load:")
    for n in cfg.degraded:
        label = "ERROR" if is_disarming(n) else "ADVISORY"
        _say(f"  {label} {n.subject}: {n.message}")  # _say sanitises (F5/LO-C5)


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
        _say("no machines enrolled")
    for m in cfg.monitored_machines:
        backup = "backup:on" if m.backup.enabled else "backup:off"
        _say(
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
        _say(f"{m.name}: FAIL — declares serial with no serial_device")
        return 1
    try:
        mode = stat_fn(device).st_mode
    except OSError as exc:
        _say(f"{m.name}: FAIL — serial device {device} is not present ({exc})")
        return 1
    if not stat.S_ISCHR(mode):
        _say(f"{m.name}: FAIL — {device} is not a character device ({stat.filemode(mode)})")
        return 1
    if m.serial_baud is None or m.serial_baud <= 0:
        _say(f"{m.name}: FAIL — no usable declared serial_baud (the live console here is 9600)")
        return 1
    _say(f"{m.name}: OK — serial {device} present at a declared {m.serial_baud} baud")
    return 0


def _verify_ssh_alias(m: MonitoredMachine) -> int:
    """Reachability of the recorded alias. No NUT secondary check — there is none.

    ME-C5: the rc 2 branch below is DEFENCE IN DEPTH, not the live path. A declared
    ``ssh`` record with an option-shaped alias is disarmed by ``_transport_notices``
    at load, so ``_monitor_verify`` returns rc 1 from its ``machine.disarmed`` gate
    before ever calling this — and rc 1 is the better answer there, because the
    operator is told the machine will not fire rather than that they typed something
    wrong. The summary's "option-shaped alias at the sink -> rc 2" row described this
    function, not the command; the rc table in docs/Configuration.md says rc 1 now.

    It is kept rather than deleted because this function is a SINK — the alias below
    becomes an argv element of a real ``ssh`` — and the only thing standing between
    the two is the loader's rule staying in step with this one. Guarding the sink is
    what makes that coupling safe to lose. Covered by a direct-call test.
    """
    alias = m.ssh.strip()
    if not alias:
        _say(f"{m.name}: FAIL — declares ssh with no alias")
        return 1
    if not _valid_ssh_alias(alias):
        LOG.error("monitor verify: config ssh alias %r for %s is invalid", m.ssh, m.name)
        return 2
    rc, _out, err = _monitor_run_ssh(alias, "true", None)
    if rc != 0:
        _say(f"{m.name}: FAIL — ssh {alias} unreachable ({err.strip() or f'rc {rc}'})")
        return 1
    _say(f"{m.name}: OK — ssh {alias} reachable")
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
    _say(head)

    rc = 0
    if machine.disarmed:
        for n in machine.load_notices:
            if is_disarming(n):
                _say(f"  DISARMED (declared {declared}): {n.message}")
        rc = 1
    else:
        for n in machine.load_notices:
            _say(f"  {n.severity.upper()}: {n.message}")

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
            _say(
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
        if probe == "native":
            # A declared `native` record is asking "is my secondary alive?", so the
            # probe's own verdict IS the answer and the words match the code.
            _say(f"{machine.name}: {'OK' if ok else 'FAIL'} — {detail}")
            if machine.load_notices:
                _say(
                    f"  the remote secondary remains the surviving authority — it lives in "
                    f"that box's /etc and no config change here disarms it. "
                    f"'monitor remove {machine.name}' is the only real disarm."
                )
            return 0 if ok else 1

        # IF-10: every other probe reason asks the INVERTED question — "is there an
        # authority here that this record does not declare?" — so the probe's raw
        # OK/FAIL is the opposite of the verdict. A `none`-with-`ups` record whose
        # probe FAILED printed "FAIL" and returned 0: a script keying on the exit
        # code read the opposite of the line it had just printed, which is worse than
        # either being wrong alone. The rc semantics are the deliberate ones (1 means
        # "an undeclared authority is still live"); the printed word is what moves.
        if ok:
            rc = 1
            summary = (
                f"a live NUT secondary answers on that box and this record declares "
                f"{declared!r} ({detail}). Run 'monitor remove {machine.name}' to "
                f"actually disarm it."
            )
        else:
            summary = (
                f"no live NUT secondary answered on that box, which matches the "
                f"declared {declared!r} ({detail})"
            )
        # The word and the exit code are derived from the SAME value, so they cannot
        # disagree again. `rc` may already be 1 from the disarm branch above, and
        # FAIL is the right word for that too.
        _say(f"{machine.name}: {'OK' if rc == 0 else 'FAIL'} — {summary}")
        return rc

    if machine.disarmed:
        return rc  # it will not fire; there is no transport left to check
    if declared == "serial":
        return _verify_serial(machine, stat_fn) or rc
    if declared == "ssh":
        return _verify_ssh_alias(machine) or rc
    _say(f"{machine.name}: no active shutdown authority")
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
    # ME-C4. The gate bundled two actions with very different blast radii, and only
    # one of them belongs behind it:
    #
    #  * the REMOTE NUT disarm SSHes into another host and runs `sudo systemctl`.
    #    Skipping it for a record this tool never enrolled natively is correct and
    #    stays keyed on the DECLARED method (INV-DECLARED);
    #  * the nft rewrite recomputes a purely LOCAL file from the survivor list and
    #    reloads it. It touches no other host and `apply_nft` returns
    #    ("no change") when the text is unchanged, so it is idempotent and free.
    #
    # Skipping the second left a stale saddr in the managed set for a host with no
    # record at all — and with `monitor add --method ssh/serial/none` also returning
    # before its own nft step, NO command in the family revoked the ip a record shed.
    # The rewrite is unconditional now; with `_survivor_saddrs`'s native filter
    # (HI-C2) the managed set becomes exactly "the ip of every surviving native
    # record", which is the invariant it was always supposed to have.
    is_native = machine.shutdown_method.strip().lower() == "native"

    if args.dry_run:
        disarm = (
            f"skip (declared {machine.shutdown_method})"
            if not is_native
            else "skip (--keep-remote)"
            if args.keep_remote
            else machine.ssh
        )
        fw = "skip (--no-firewall)" if args.no_firewall else saddrs
        _say(f"[dry-run] disarm remote: {disarm}")
        _say(f"[dry-run] firewall: {fw}")
        _say(f"[dry-run] persist: drop {machine.name} (config written LAST)")
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

    # 2) firewall: rewrite the saddr set from survivors (unless --no-firewall).
    # ME-C4: unconditional. The rewrite is local and idempotent, and a NON-native
    # record can still hold a stale enrollment `ip` in the managed set — that is
    # precisely the ip nothing else revokes.
    if not args.no_firewall:
        rc, err = _rewrite_nft_saddrs(
            saddrs, no_restart_bouncer=args.no_restart_bouncer, verb="monitor remove"
        )
        # Fatal only for the native path, where the accept is this record's own and
        # step 3 has not run yet. For a non-native record the removal is the
        # operator's actual request and the rewrite is a repair sweep: refusing here
        # would leave BOTH the record AND the stale saddr, which is strictly worse.
        # Logged either way — never silent.
        if rc != 0 and is_native:
            return 4

    # 3) persist config LAST (so a firewall failure leaves config unchanged)
    _monitor_persist(cfg_path, [m.to_dict() for m in survivors])
    _say(f"removed {machine.name}")
    return 0


def _valid_ip(value: str) -> bool:
    """Any IP literal. Used where either family is legitimate (LISTEN, --primary-ip)."""
    try:
        ipaddress.ip_address(value.strip())
    except ValueError:
        return False
    return True


def _valid_saddr_ip(value: str) -> bool:
    """F2: a machine's ``ip`` is IPv**4** only — its sole consumer is the nft set.

    ``ip`` is documented as "resolved source IP added to the firewall saddr set",
    and that set is rendered as ``ip saddr { … }``, nftables' IPv4 matcher. A v6
    literal there does not merely fail to match: ``nft -f`` rejects the whole
    ruleset, and since the file is written before the reload judges it, the
    operator's policy-drop chain then fails to load at the next boot.

    Deliberately NOT applied to ``--primary-ip`` or ``nut_server.listen``: those
    feed upsd's LISTEN and the secondary's MONITOR line, where v6 is legitimate.
    """
    return nutclient.valid_ipv4(value)


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
    # F2: every rung yields the value that lands in `entry.ip`, whose only consumer
    # is the `ip saddr` (IPv4) set — so IPv4 is what "usable" means here. A
    # v6-reachable secondary's $SSH_CONNECTION field 1 is a v6 literal, which used
    # to be accepted and then rendered a ruleset `nft -f` refuses.
    if explicit:
        return explicit.strip() if _valid_saddr_ip(explicit) else None
    if _valid_ip(primary_ip):
        rc, out, _err = _monitor_run_ssh(alias, f"ip -o route get {primary_ip}", None)
        if rc == 0:
            src = _parse_route_src(out)
            if src is not None and _valid_saddr_ip(src):
                return src
    rc, out, _err = _monitor_run_ssh(alias, "echo $SSH_CONNECTION", None)
    if rc == 0:
        fields = out.split()
        if fields and _valid_saddr_ip(fields[0]):
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
            'control character. It is emitted as a single SHUTDOWNCMD "<cmd>" line in '
            "the secondary's /etc/nut/upsmon.conf, so a newline ends that directive and "
            "everything after it becomes a further upsmon directive on that machine.",
            shutdown_cmd,
        )
        return 2
    if args.ip and not _valid_saddr_ip(args.ip):
        # F2: IPv4 specifically. This value is written to the record's `ip`, whose
        # only consumer is the `ip saddr` set — nftables' IPv4 matcher. A v6
        # literal there makes `nft -f` reject the whole ruleset.
        LOG.error(
            "monitor add: --ip %r is not a valid IPv4 literal. This address is added to "
            "the managed 'ip saddr' nft set, which is IPv4-only; a v6 address there makes "
            "nft refuse the ENTIRE ruleset, taking the operator's policy drop with it.",
            args.ip,
        )
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
        # "on its UPS" was the old text and is no longer the whole truth: a declared
        # `native` machine is scanned against EVERY configured UPS, because its
        # authority is the remote box's own upsmon on this primary's FSD and is keyed
        # to no UPS in this file.
        scope_text = "on ANY configured UPS" if method == "native" else "on its UPS"
        LOG.error(
            "monitor add: %s is both an enrolled machine (shutdown_method=%r) and an "
            "enabled shutdown_target %s (double-shutdown risk) — pass --force to "
            "override",
            args.name,
            method,
            scope_text,
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
        # ME-C4: the ip this record SHEDS has to be revoked by something. `others`
        # already excludes every record of this name, and `_survivor_saddrs` keeps
        # only declared-native machines, so a re-enrolment of a former native
        # secondary as serial/ssh/none drops its ip out of the computed set — which
        # is exactly the revocation nothing in the family performed. The record-only
        # branch returned before ever reaching an nft step, and `monitor remove`
        # skipped its own rewrite for a non-native record, so between them the
        # managed set kept granting upsd access to a host with no native record.
        # This still opens NOTHING for this machine: a non-native entry contributes
        # no saddr of its own.
        saddrs = _survivor_saddrs((*others, entry))
        if args.dry_run:
            transport = {
                "serial": f"serial {args.serial_device} @ {baud} baud",
                "ssh": f"ssh {ssh_alias}",
                "none": "no active shutdown authority",
            }[method]
            _say(f"[dry-run] record-only add ({method}): {transport}")
            _say(f"[dry-run] shutdown_cmd: {shutdown_cmd}")
            _say("[dry-run] skipped: upsd.users, LISTEN, remote upsmon (native-only)")
            fw = "skip (--no-firewall)" if args.no_firewall else saddrs
            _say(f"[dry-run] firewall: {fw}   (revokes any ip this record sheds)")
            _say(f"[dry-run] persist: {args.name} (config written LAST)")
            return 0
        if not args.no_firewall:
            # Never fatal here: recording the machine is the operator's actual
            # request and the rewrite is a repair sweep for an accept this record
            # does not own. Refusing would leave the stale saddr AND no record.
            _rewrite_nft_saddrs(
                saddrs, no_restart_bouncer=args.no_restart_bouncer, verb="monitor add"
            )
        _monitor_persist(cfg_path, [*(m.to_dict() for m in others), entry.to_dict()])
        _say(f"recorded {args.name} (shutdown_method={method})")
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
        _say(f"[dry-run] resolve ip: {preview_ip or unresolved}")
        _say(
            f"[dry-run] bootstrap primary: LISTEN {preview_primary or unresolved}, "
            f"user {ns.secondary_user} (password <redacted>), nft "
            + ("skip" if args.no_firewall else str(saddrs))
        )
        _say(f"[dry-run] remote bootstrap: {ssh_alias} detect/install/write/enable")
        _say("[dry-run] verify (deep) then persist entry (no password)")
        _say("[dry-run] contacted: nothing — no ssh, no route probe, no local command")
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
    _say(f"enrolled {args.name} ({ip}) on {ups_name}")
    return 0


# ---- remote-shutdown preview + shutdown rehearse ----------------------------


def _resolved_targets(ups: UpsConfig, deps: Deps) -> list[ShutdownTarget]:
    """Every target this UPS would consider: configured, plus machine projections."""
    return [*ups.shutdown_targets, *_machine_targets(ups, deps.monitored_machines)]


def _unprojected_machines(
    ups: UpsConfig, deps: Deps, projected: list[ShutdownTarget]
) -> list[tuple[str, str]]:
    """``(name, reason)`` for every machine on this UPS that produced NO target.

    HI-C4: the preview listed only what `_machine_targets` yielded, so a machine
    the load disarmed — the exact machine an operator runs this command to ask
    about — did not appear at all: not as a line, not as a verdict, not on stderr.
    `_machine_targets` reads `effective_method`, so a disarmed machine resolves to
    `"none"` and hits its bare `else: continue`, with no `_report_unprojectable`
    call; and `_resolved_targets` passes no `deps`, so even the cases that DO
    report are reduced to a `LOG.error` the preview never shows.

    Answers "why not" from the same fields the firing path reads, so the reasons
    cannot drift from the omissions. Reporting, so it may read the EFFECT — and it
    names the DECLARATION alongside, because "disarmed at load" and "declared
    none" are the same effect wearing two very different meanings (T-02-46).
    """
    ups_key = canonical_ups_key(ups.name)
    listed = {t.name.strip().lower() for t in projected}
    out: list[tuple[str, str]] = []
    for m in deps.monitored_machines:
        if not m.ups.strip() or canonical_ups_key(m.ups) != ups_key:
            continue
        if m.name.strip().lower() in listed:
            continue
        declared = m.shutdown_method.strip().lower()
        if m.disarmed:
            reason = (
                f"disarmed at load (declared {declared}) — see "
                f"'monitor verify {m.name}' and the DEGRADED CONFIG block below"
            )
        elif declared == "native":
            reason = (
                "declared native — its own upsmon halts it on this primary's FSD, "
                "so it is deliberately never pushed to (a push as well would shut "
                "the box down twice)"
            )
        elif declared == "none":
            reason = "declared none — this record carries no shutdown authority"
        elif declared == "serial" and m.serial_baud is None:
            reason = (
                "declared serial with no usable serial_baud, so it cannot be "
                "projected; declare a positive integer baud"
            )
        else:
            reason = (
                "not projected — most likely a duplicate shutdown target name on "
                f"this UPS; run 'monitor verify {m.name}'"
            )
        out.append((m.name, reason))
    return out


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
        return _cmd_event("remote_shutdown", _resolve_ups_name(args.ups), manual=True)

    cfg = _load_config()
    if cfg is None:
        return 1
    deps = _build_deps(cfg, dry_run=True)
    store = StateStore(_state_path())
    # HI-C1: the SAME resolver the real path uses. The preview read `args.ups`
    # directly, so it ignored `$UPSNAME` while `_cmd_event` honours it — under
    # `upssched` the preview reported every UPS while the real run touched one.
    resolved = _resolve_ups_name(args.ups)
    names = [resolved] if resolved else list(cfg.upses)
    if not resolved:
        # ...and the remaining, deliberate asymmetry is stated rather than hidden.
        # A real run with no name resolves NO UPS and refuses (rc 2); it does not
        # sweep. Sweeping here is still the useful answer to "what is configured?",
        # but an operator who validates with the preview and then runs it for real
        # must not be surprised — that surprise was the whole finding.
        _say(
            f"no UPS named and $UPSNAME is unset — previewing all "
            f"{len(cfg.upses)} configured UPS(es). A REAL 'remote-shutdown' with no "
            f"name evaluates NOTHING and exits 2: pass a UPS name or set $UPSNAME."
        )
    for name in names:
        ups = cfg.ups(name)
        if ups is None:
            LOG.warning("remote-shutdown: unconfigured UPS %r — skipping", name)
            continue
        snap = deps.read_snapshot(ups.name)
        state = store.get(ups.name)
        _say(f"{ups.label} ({ups.name}) — status {snap.status or 'unknown'}")
        targets = _resolved_targets(ups, deps)
        omitted = _unprojected_machines(ups, deps, targets)
        if not targets and not omitted:
            _say("  (no shutdown targets resolved)")
            continue
        for t in targets:
            should, reason = _preview_verdict(ups, state, deps, t, snap)
            _say(
                f"  {t.name}\t{_target_location(t)}\t{t.cmd}\t"
                f"would fire: {'yes' if should else 'no'} — {reason}"
            )
        # HI-C4: a machine that will NOT fire is the thing the operator is asking
        # about, and it was the one thing the preview left out entirely.
        for name, reason in omitted:
            _say(f"  {name}\t(no target)\t—\twould fire: no — {reason}")
    # RA-01: a degrade must reach the operator where the operator already looks,
    # and "what will happen?" is exactly where they look. `monitor list` renders
    # this block; the command built to answer that question did not.
    _print_degraded(cfg)
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
    # F6: an UNKNOWN kind must not be dispatched. `_cmd_shutdown` below falls
    # through to `ssh_shutdown` for anything not local and not serial — which is
    # exactly WHY `validate_legacy_targets` disarms an unknown kind ("dispatch
    # treats anything not local and not serial as SSH, so an unknown kind becomes a
    # silent ssh attempt against whatever `host` happens to hold"). Rehearse then
    # force-enables the target, so this verb reached the dispatch that rule exists
    # to prevent. The rehearsal command cannot halt a box, but it can still open a
    # connection to whatever is in `host`, and "a rule that holds everywhere except
    # from this one verb" is not a rule.
    if not target.is_local and not target.is_serial and target.kind.strip().lower() != "remote":
        LOG.error(
            "shutdown rehearse: %r declares kind %r, which is none of remote/serial/"
            "local. Config disarms that shape precisely because dispatch treats "
            "anything not local and not serial as SSH — so rehearsing it would ssh to "
            "whatever 'host' happens to hold. Fix 'kind' first.",
            args.name,
            target.kind,
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
    _say(f"rehearse {target.name} via {where}")
    _say(f"  command: {_REHEARSAL_CMD}   (hard-coded; the recorded shutdown_cmd is not read)")
    if target.is_serial:
        _say(f"  device:  {target.device} @ {target.baud} baud")
    rc, _out, err = deps.serial_shutdown(target) if target.is_serial else deps.ssh_shutdown(target)
    if rc != 0:
        _say(f"  FAIL — rc={rc} {err.strip() or '(no stderr)'}")
        return 1
    _say(f"  OK — look for PHASE2_REHEARSAL on {target.name} (journalctl -t ups-orchestrator)")
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
            "power-dashboard|webui|control|monitor|maintenance|remote-shutdown|shutdown|"
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
    if mode == "maintenance":
        return _cmd_maintenance(args[1:])
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
