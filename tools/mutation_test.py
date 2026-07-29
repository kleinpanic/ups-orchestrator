#!/usr/bin/env python3
"""Targeted mutation test for ups-orchestrator's critical logic.

mutmut 3.x's mutant-copy runner is flaky on this project, so this is a small,
explicit harness instead: each entry patches one source expression, runs the
full test suite, and expects it to FAIL (the mutant is "killed"). A surviving
mutant means the tests don't actually pin that behaviour — a real coverage gap.

Run: ``python tools/mutation_test.py`` (or ``make mutation``). Exits non-zero if
any mutant survives. Add a mutation whenever you add behaviour worth pinning.
"""

from __future__ import annotations

import os
import pathlib
import signal
import subprocess
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Never let a mutant's bytecode outlive the source restore. Without this, a
# restored .py whose mtime lands in the same second as the mutated write keeps
# the mutated .pyc cached, poisoning the next clean run with a phantom failure.
_CHILD_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

# This harness edits src/ IN PLACE, so its one hard promise is that a mutant
# never outlives the run. Two separate incidents broke that promise in one
# evening and both left broken logic on disk looking exactly like an ordinary
# working-tree edit:
#
#   1. Two runs raced. Each restored "its" original, so one wrote back a copy it
#      had read while the OTHER's mutant was applied — leaving `if False:` where
#      a MONITOR guard belongs, and `load / 10` where a watts calculation goes.
#   2. An external `timeout 900` sent SIGTERM mid-mutant. The `finally` never
#      ran, leaving the nftables rollback replaced by `pass`.
#
# Both are now structurally prevented rather than remembered: a lock stops the
# race, and a signal handler restores before dying. A reviewer reading src/
# mid-run still sees a mutant — that is unavoidable and is why the automated
# security scanner flagged one — but nothing is left behind afterwards.
_LOCK = ROOT / ".mutation-test.lock"
_IN_FLIGHT: tuple[pathlib.Path, str] | None = None


def _restore_in_flight() -> None:
    """Put back the mutant currently applied, if any. Safe to call twice."""
    global _IN_FLIGHT
    if _IN_FLIGHT is not None:
        path, original = _IN_FLIGHT
        _IN_FLIGHT = None
        try:
            path.write_text(original)
        except OSError as exc:  # pragma: no cover — best effort while dying
            print(f"FAILED to restore {path}: {exc}", file=sys.stderr)


def _on_signal(signum: int, _frame: types.FrameType | None) -> None:
    """Restore before dying. Without this a SIGTERM leaves a mutant on disk."""
    _restore_in_flight()
    _LOCK.unlink(missing_ok=True)
    print(f"\ninterrupted by signal {signum}; source restored", file=sys.stderr)
    raise SystemExit(128 + signum)


# (file, original_substring, mutated_substring, description)
MUTATIONS: list[tuple[str, str, str, str]] = [
    (
        "src/ups_orchestrator/cli.py",
        "return _verify_serial(machine, stat_fn, deep=args.deep, probe=serial_probe) or rc",
        "return _verify_serial(machine, stat_fn, deep=False, probe=serial_probe) or rc",
        "cli: --deep silently downgraded to the shallow serial check — the operator "
        "asks the far end to answer and gets 'a device node exists'",
    ),
    (
        "src/ups_orchestrator/config.py",
        "return any(is_disarming(n) for n in notices)",
        "return all(is_disarming(n) for n in notices)",
        "config: heading severity folds with all() — a real disarm mixed with "
        "advisories renders as advisory, hiding it",
    ),
    (
        "src/ups_orchestrator/nut.py",
        "self.realpower_nominal * self.load / 100",
        "self.realpower_nominal * self.load / 10",
        "nut: est_load_watts scale",
    ),
    (
        "src/ups_orchestrator/nut.py",
        "max(0, self.realpower_nominal - watts)",
        "max(0, self.realpower_nominal + watts)",
        "nut: load_headroom sign",
    ),
    (
        "src/ups_orchestrator/nut.py",
        "self.battery_voltage / self.battery_voltage_nominal * 100",
        "self.battery_voltage / self.battery_voltage_nominal * 10",
        "nut: battery_voltage_percent scale",
    ),
    (
        "src/ups_orchestrator/nut.py",
        "return max(0, 100 - self.load)",
        "return max(0, 99 - self.load)",
        "nut: load_margin off-by-one",
    ),
    (
        "src/ups_orchestrator/state.py",
        "os.fsync(tmp.fileno())",
        "pass",
        "state: fsync-before-replace removed",
    ),
    (
        "src/ups_orchestrator/jsonlog.py",
        "path.replace(rotated)",
        "pass  # mutated",
        "jsonlog: rotation no-op",
    ),
    (
        "src/ups_orchestrator/report.py",
        'snap.alarm.strip().lower() not in ("", "none")',
        'snap.alarm.strip().lower() in ("", "none")',
        "report: alarm_active inverted",
    ),
    (
        "src/ups_orchestrator/report.py",
        '"fail" in snap.test_result.lower()',
        '"fail" not in snap.test_result.lower()',
        "report: selftest_failed inverted",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "if result.configured and not result.ok:",
        "if False:",
        "audit: boot-audit retry bypass",
    ),
    (
        "src/ups_orchestrator/events.py",
        "drop < policy.drop_percent",
        "drop > policy.drop_percent",
        "events: load-step threshold flip",
    ),
    (
        "src/ups_orchestrator/status.py",
        "if charge <= 20:",
        "if charge <= 200:",
        "status: battery colour threshold",
    ),
    (
        "src/ups_orchestrator/baseline.py",
        "ordered[low] + (ordered[high] - ordered[low]) * frac",
        "ordered[low] - (ordered[high] - ordered[low]) * frac",
        "baseline: percentile interpolation sign",
    ),
    (
        "src/ups_orchestrator/baseline.py",
        "mean=round(sum(watts) / len(watts)),",
        "mean=round(sum(watts) * len(watts)),",
        "baseline: mean op",
    ),
    (
        "src/ups_orchestrator/selftest.py",
        'if "fail" in r:',
        'if "fail" not in r:',
        "selftest: classify failed inverted",
    ),
    (
        "src/ups_orchestrator/selftest.py",
        "if snapshot.on_battery or snapshot.low_battery:",
        "if snapshot.on_battery and snapshot.low_battery:",
        "selftest: on-battery guard weakened",
    ),
    (
        "src/ups_orchestrator/webui.py",
        "step = max(1, -(-len(pts) // 600))",
        "step = max(1, len(pts) // 600)",
        "webui: history downsample cap",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        '        "    upsmon secondary\\n"\n'
        '        f"{_UPSMON_END}\\n"\n'
        "    )\n"
        '    if stripped == "" or stripped.endswith("\\n"):\n'
        "        new_text = stripped + block\n"
        "    else:\n"
        '        new_text = stripped + "\\n" + block\n'
        "    return new_text, new_text != text",
        '        "    upsmon secondary\\n"\n'
        '        f"{_UPSMON_END}\\n"\n'
        "    )\n"
        '    if stripped == "" or stripped.endswith("\\n"):\n'
        "        new_text = stripped + block\n"
        "    else:\n"
        '        new_text = stripped + "\\n" + block\n'
        "    return new_text, True",
        "nutclient: upsd.users upsert idempotency (changed always True)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        # HI-C2 inserted the member validation between the guard and the join, so the
        # pattern is anchored on the guard alone. An empty set must REMOVE the rule,
        # never render a match-anything one.
        '    if not saddrs:\n        return ""\n',
        '    if not saddrs:\n        saddrs = ["0.0.0.0"]\n',
        "nutclient: empty-saddrs drops accept rule",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'if not inside and stripped.startswith("MONITOR "):',
        "if False:",
        "nutclient: guard non-marker MONITOR refusal bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'if mode in ("MODE=standalone", "MODE=netserver"):',
        "if False:",
        "nutclient: guard MODE=standalone/netserver refusal bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "if not any(tok in status.split() for tok in _STATUS_TOKENS):",
        "if False:",
        "nutclient: verify status-token match bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "changed = conf_changed or users_changed",
        "changed = conf_changed and users_changed",
        "nutclient: bootstrap either-changed restart predicate (or -> and)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    rc, _out, err = run_local(list(_RESTART_NUT), None)\n"
        "        if rc != 0:\n"
        '            log.append(f"nut-server restart failed: {_redact(err, password)}")\n'
        "            return 4, log",
        "    rc, _out, err = run_local(list(_RESTART_NUT), None)\n"
        "        if False:\n"
        '            log.append(f"nut-server restart failed: {_redact(err, password)}")\n'
        "            return 4, log",
        "nutclient: bootstrap restart-before-nft short-circuit bypassed",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "    if not password:\n"
        '        LOG.error("monitor add: %s not set in the environment", _SECRET_ENV)\n'
        "        return 2",
        "    if not password:\n"
        '        LOG.error("monitor add: %s not set in the environment", _SECRET_ENV)\n'
        "        return 0",
        "cli: monitor add missing-password exit-code (2 -> 0)",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "    if not ok:\n"
        '        LOG.error("monitor add: verification failed: %s", detail)\n'
        "        return 5",
        "    if not ok:\n"
        '        LOG.error("monitor add: verification failed: %s", detail)\n'
        "        return 0",
        "cli: monitor add verify-fail exit-code (5 -> 0)",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "    if conflicts and not args.force:",
        "    if conflicts and args.force:",
        "cli: monitor add dual-regime --force refusal predicate flipped",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "return bool(_NUT_NAME_RE.match(name))",
        "return True",
        "nutclient: valid_nut_name always-true (injection charset bypassed)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        '    if not valid_nut_name(ups):\n        return False, f"invalid UPS name: {ups!r}"',
        '    if False:\n        return False, f"invalid UPS name: {ups!r}"',
        "nutclient: verify ups-name guard bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        '    if not valid_ip(primary):\n        return False, f"invalid primary IP: {primary!r}"',
        '    if False:\n        return False, f"invalid primary IP: {primary!r}"',
        "nutclient: verify primary-ip guard bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    if timeout <= 0:",
        "    if False:",
        "nutclient: verify nonpositive-timeout guard bypassed",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'f"timeout {timeout} upsc {ups}@{primary} ups.status"',
        'f"upsc {ups}@{primary} ups.status"',
        "nutclient: verify drops the timeout bound (WR-03 regression)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    if saddrs is not None:\n        rc, out, err = apply_nft(",
        "    if saddrs is None:\n        rc, out, err = apply_nft(",
        "nutclient: bootstrap --no-firewall skip inverted (CR-02 regression)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    hook = _NFT_INPUT_HOOK_RE.search(stripped)\n    if hook is None:",
        "    hook = _NFT_INPUT_HOOK_RE.search(stripped)\n    if hook is not None:",
        "nutclient: upsert nft requires an input-hook chain (LIVE BUG #1 splice target)",
    ),
    (
        "src/ups_orchestrator/cli.py",
        '        if tok == "src" and i + 1 < len(tokens):',
        '        if tok == "src2" and i + 1 < len(tokens):',
        "cli: route-src parse keys on the src field (LIVE BUG #2)",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "    if _valid_ip(toward_ip):\n"
        '        rc, out, _err = _monitor_run_local_probe(["ip", "-o", "route", "get", toward_ip])',
        "    if not _valid_ip(toward_ip):\n"
        '        rc, out, _err = _monitor_run_local_probe(["ip", "-o", "route", "get", toward_ip])',
        "cli: primary-ip auto-detect route probe guard (LIVE BUG #3)",
    ),
    # --- the security-audit round (F1/F2/F4/F6/F7) and HI-C1/ME-C4 -------------
    (
        "src/ups_orchestrator/notify.py",
        "    return parts.scheme.lower() in _ALLOWED_SCHEMES and bool(parts.netloc)",
        "    return True",
        "notify: webhook URL scheme validation bypassed (F1 — daemon restart loop)",
    ),
    (
        "src/ups_orchestrator/notify.py",
        "                if status is None:",
        "                if False:",
        "notify: file:// URL None-status guard bypassed (F1 — TypeError escapes send)",
    ),
    (
        "src/ups_orchestrator/notify.py",
        "            except Exception as exc:  # noqa: BLE001 — the promise in the docstring",
        "            except _NeverRaised as exc:  # mutated",
        "notify: send catch-all removed (F1 — 'never raises' becomes false again)",
    ),
    (
        "src/ups_orchestrator/config.py",
        "        except (ValueError, OverflowError):  # NaN / ±inf\n            return default",
        "        except ValueError:  # mutated — OverflowError escapes again\n"
        "            return default",
        "config: _as_int OverflowError guard narrowed (F4 — inf kills the daemon)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    bad = [s for s in saddrs if not valid_ipv4(s)]",
        "    bad = [s for s in saddrs if not valid_ip(s)]",
        "nutclient: nft saddr accepts IPv6 again (F2 — nft -f rejects the ruleset)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "        try:\n            conf.write_text(text)",
        "        try:\n            pass  # mutated — leave the rejected ruleset on disk",
        "nutclient: apply_nft rollback removed (F2 — unloadable ruleset survives)",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "        return 2 if manual else 0",
        "        return 0",
        "cli: remote-shutdown no-UPS silent success restored (HI-C1)",
    ),
    (
        "src/ups_orchestrator/state.py",
        "            if ownership_flips or os.geteuid() == 0:",
        "            if os.geteuid() == 0:",
        "state: ownership-flip warning re-gated on root only (F7)",
    ),
    # --- P2-02 / P2-06 / P2-08 core -------------------------------------------
    # The final verification found the harness covered nut/state/jsonlog/report/
    # audit/status/baseline/selftest/webui/nutclient/cli/notify/config-numeric and
    # NOT the per-machine shutdown model: derive_shutdown_method, dual_regime_pairs,
    # disarmed/effective_method or _machine_targets. Nine verifier-authored mutants
    # against those all died, so the behaviour WAS pinned — the harness just did not
    # claim it. These make the claim.
    (
        "src/ups_orchestrator/config.py",
        "    if ups.strip():\n"
        '        return "native"\n'
        "    if backup.enabled:\n"
        "        kind = backup.kind.strip().lower()\n"
        '        if kind == "serial":\n'
        '            return "serial"\n'
        '        return "ssh"  # kind == "remote" (or unknown) maps to the ssh transport\n'
        '    return "none"',
        "    if backup.enabled:  # mutated — ORDER SWAPPED\n"
        "        kind = backup.kind.strip().lower()\n"
        '        if kind == "serial":\n'
        '            return "serial"\n'
        '        return "ssh"\n'
        "    if ups.strip():\n"
        '        return "native"\n'
        '    return "none"',
        "config: derive ORDERING swapped — an enabled backup would beat has-ups (P2-01)",
    ),
    (
        "src/ups_orchestrator/config.py",
        '    if ups.strip():\n        return "native"',
        '    if ups.strip():\n        return "ssh"  # mutated',
        "config: derive maps a Phase-1 native secondary to a push (P2-01)",
    ),
    (
        "src/ups_orchestrator/config.py",
        "return any(is_disarming(n) for n in self.load_notices) and "
        'self.shutdown_method != "native"',
        "return any(is_disarming(n) for n in self.load_notices)",
        "config: disarmed drops the native carve-out (INV-DECLARED)",
    ),
    (
        "src/ups_orchestrator/config.py",
        'return "none" if self.disarmed else self.shutdown_method',
        "return self.shutdown_method  # mutated",
        "config: effective_method ignores the degrade (INV-DEGRADE)",
    ),
    (
        "src/ups_orchestrator/config.py",
        '        native = m.shutdown_method.strip().lower() == "native"',
        "        native = False  # mutated",
        "config: dual_regime_pairs narrows a native machine to its own UPS again "
        "(cross-UPS double shutdown)",
    ),
    (
        "src/ups_orchestrator/config.py",
        "            for t in ups.shutdown_targets:\n"
        "                if t.enabled and t.name.strip().casefold() == name_key:",
        "            for t in ups.shutdown_targets:\n"
        "                if False and t.name.strip().casefold() == name_key:",
        "config: dual_regime_pairs detects nothing at all (P2-06)",
    ),
    (
        "src/ups_orchestrator/config.py",
        "        if not isinstance(machines_raw, list):",
        "        if False:  # mutated",
        "config: a non-list monitored_machines silently unprotects everything again",
    ),
    (
        "src/ups_orchestrator/config.py",
        "        non_dicts = [i for i, m in enumerate(machines_raw) if not isinstance(m, dict)]",
        "        non_dicts = []  # mutated",
        "config: a non-object monitored_machines entry is silently dropped again",
    ),
    (
        "src/ups_orchestrator/events.py",
        "        if not m.ups.strip() or canonical_ups_key(m.ups) != ups_key:",
        "        if not m.ups.strip():  # mutated — UPS association guard dropped",
        "events: _machine_targets projects a machine onto the WRONG UPS (P2-06)",
    ),
    (
        "src/ups_orchestrator/events.py",
        "        method = m.effective_method.strip().lower()",
        "        method = m.shutdown_method.strip().lower()  # mutated",
        "events: projection reads DECLARED not EFFECTIVE — a disarmed machine fires",
    ),
    (
        "src/ups_orchestrator/events.py",
        "            if m.serial_baud is None:",
        "            if False:  # mutated",
        "events: a serial machine projects with an unparseable baud (P2-08 silent no-op)",
    ),
    (
        "src/ups_orchestrator/events.py",
        "        else:\n            continue\n        key = m.name.strip().lower()",
        "        else:\n"
        "            target = ShutdownTarget(\n"
        '                name=m.name, kind="remote", enabled=True, host=m.ssh, cmd=m.shutdown_cmd\n'
        "            )\n"
        "        key = m.name.strip().lower()",
        "events: a native/none machine IS projected onto a push (P2-01 double shutdown)",
    ),
    (
        "src/ups_orchestrator/events.py",
        "    state.shutdowns_sent.append(target.name)\n    _log_event(\n"
        '        deps,\n        "shutdown_result",',
        "    if rc == 0:  # mutated — a failed remote now strands the local host\n"
        "        state.shutdowns_sent.append(target.name)\n    _log_event(\n"
        '        deps,\n        "shutdown_result",',
        "events: shutdowns_sent append gated on success (T-02-24 local starvation)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    candidates = dict.fromkeys((loopback_line, lan_line))",
        "    candidates = dict.fromkeys((lan_line,))  # mutated — loopback dropped",
        "nutclient: upsd.conf loses its loopback LISTEN — upsd dies on a boot before DHCP",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "    if was_clean:",
        "    if False:  # mutated — a clean reboot alerts as an outage again",
        "audit: clean-shutdown gate dropped (deliberate poweroff pages as power loss)",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "    if window.active:",
        "    if False:  # mutated — maintenance window no longer suppresses",
        "audit: maintenance window ignored (operator-declared downtime still pages)",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "        active=now < until,",
        "        active=True,  # mutated — window never expires",
        "audit: maintenance window never expires (a forgotten window silences real outages)",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "        if when - first <= window:",
        "        if True:  # mutated — late journald rotation counts as boot evidence",
        "audit: boot-evidence window dropped (stale journal lines alarm a healthy host)",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "    shutdown_actions = _matching(_strip_unix_prefix(journal), _SHUTDOWN_PATTERNS, limit)",
        "    shutdown_actions = _matching(within_boot_window(journal), _SHUTDOWN_PATTERNS, limit)",
        "audit: shutdown evidence windowed to 120s — count pinned to 0, body always"
        " claims no killpower evidence",
    ),
    (
        "src/ups_orchestrator/audit.py",
        "    return any(_contains_any(line, _CLEAN_SHUTDOWN_PATTERNS) for line in lines)",
        "    return False  # mutated — the clean-shutdown detector never fires",
        "audit: previous_boot_ended_cleanly always False (gate 2 inert)",
    ),
    (
        "src/ups_orchestrator/report.py",
        "    if not parts:",
        "    if False:  # mutated — body may render empty",
        "report: daily embed can lose its description entirely",
    ),
    (
        "src/ups_orchestrator/nut.py",
        "        _note_upsc_failure(ups_name, result.stderr.strip())",
        "        pass  # mutated — a refused upsc goes back to being silent",
        "nut: upsc failure silent again (the two-day outage's invisibility)",
    ),
    # --- nutproto: the per-UPS FSD trigger (03-06) ----------------------------
    #
    # Every entry below is paired with the test in tests/test_nutproto.py that
    # kills it. The one mutant deliberately NOT registered is "make the block read
    # unbounded" — it kills by hanging, which costs this harness its full 120 s
    # timeout on every run; test_read_block_is_bounded_against_an_endless_server
    # pins that behaviour directly instead.
    (
        "src/ups_orchestrator/nutproto.py",
        'FSD_ACK = "OK FSD-SET"',
        'FSD_ACK = "OK"  # mutated — the bare OK the auth lines also return',
        "nutproto: FSD ack weakened to the auth OK — a server that never set the flag "
        "reports success (killed by test_bare_ok_to_the_fsd_line_is_a_failure)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        "    if not valid_nut_name(upsname):\n        return FsdResult(",
        "    if False:  # mutated — UPS name no longer validated\n        return FsdResult(",
        "nutproto: set_fsd UPS-name guard dropped — a name carrying a newline appends a "
        "second command (killed by test_refuses_a_bad_ups_name_and_sends_nothing)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        "    if not _valid_credential(password):",
        "    if False:  # mutated — password no longer checked for control chars",
        "nutproto: credential control-char guard dropped — a newline in the password "
        "closes its line (killed by "
        "test_refuses_a_password_containing_a_newline_and_sends_nothing)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        "    if not _valid_credential(user):",
        "    if False:  # mutated — username no longer checked for control chars",
        "nutproto: USERNAME line left injectable (killed by "
        "test_refuses_a_username_containing_a_control_character_and_sends_nothing)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '            detail = f"{name} refused: {_scrub(response.strip(), password)!r}"\n'
        "            return FsdResult(ok=False, upsname=upsname, failed_line=name, detail=detail)",
        '            detail = f"{name} refused: {_scrub(response.strip(), password)!r}"\n'
        "            continue  # mutated — a refused line no longer stops the exchange",
        "nutproto: exchange no longer stops at the first refusal — the plaintext password "
        "goes on the wire to a server that already refused the user "
        "(killed by test_stops_when_username_is_refused)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '            detail = f"{name} refused: {_scrub(response.strip(), password)!r}"',
        '            detail = f"{line} refused: {_scrub(response.strip(), password)!r}"',
        "nutproto: failure detail built from the line that was SENT — the PASSWORD line's "
        "bytes ARE the secret (killed by test_failure_detail_never_contains_the_password)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        'f"{name} refused: {_scrub(response.strip(), password)!r}"',
        'f"{name} refused: {response.strip()!r}"  # mutated — reply no longer scrubbed',
        "nutproto: server reply no longer scrubbed — a server echoing the password back "
        "leaks it (killed by test_a_server_that_echoes_the_password_back_is_scrubbed)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        "        except Exception as exc:",
        "        except ValueError as exc:  # mutated — OSError now escapes set_fsd",
        "nutproto: transport exception escapes onto the shutdown path, stranding every "
        "target behind it (killed by "
        "test_a_transport_that_raises_returns_a_failure_not_an_exception)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '    if lines and lines[0].startswith("ERR "):',
        "    if False:  # mutated — ERR now reads as an empty census",
        "nutproto: LIST CLIENT ERR read as 'zero clients' — 'upsd does not know that UPS' "
        "becomes 'every secondary drained' (killed by "
        "test_list_client_err_is_a_failure_not_an_empty_census)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '        if len(parts) == 3 and parts[0] == "CLIENT" and parts[1] == upsname',
        '        if len(parts) == 3 and parts[0] == "CLIENT"  # mutated — any UPS counts',
        "nutproto: client census harvests other UPSes' clients (killed by "
        "test_list_client_ignores_client_lines_for_another_ups)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        "    if not line:",
        "    if False:  # mutated — EOF now reads as a blank line",
        "nutproto: a closed socket spins the read loop instead of failing; no timeout "
        "fires because nothing blocks (killed by test_read_line_raises_on_a_closed_connection)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '        if line.startswith("END LIST"):',
        "        if False:  # mutated — END LIST no longer terminates the block",
        "nutproto: block read runs past END LIST into the next reply, desynchronising the "
        "connection (killed by test_read_block_stops_at_end_list)",
    ),
    (
        "src/ups_orchestrator/nutproto.py",
        '        return send("LOGOUT")',
        '        return "OK Goodbye"  # mutated — LOGOUT never sent',
        "nutproto: LOGOUT dropped, leaving a stale entry in the very census list_clients "
        "reads (killed by test_logout_is_sent_verbatim_and_never_raises)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    for begin, end_marker in _NFT_MARKER_PAIRS:\n"
        "        while True:\n"
        "            shorter = _strip_one_nft_block(text, begin, end_marker)\n"
        "            if shorter == text:\n"
        "                break\n"
        "            text = shorter\n"
        "    return text",
        "    return _strip_one_nft_block(text, _NFT_BEGIN, _NFT_END)",
        "nutclient: _strip_nft_block back to a single find (second block accumulates)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'f\'counter name "{_NFT_COUNTER_ALLOWED}" accept comment "upsd NUT secondaries"\\n\'',
        'f\'accept counter name "{_NFT_COUNTER_ALLOWED}" comment "upsd NUT secondaries"\\n\'',
        "nutclient: counter reference after the accept verdict (never increments)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'f"{indent}counter {_NFT_COUNTER_ALLOWED} {{\\n"',
        "f'{indent}counter \"{_NFT_COUNTER_ALLOWED}\" {{\\n'",
        "nutclient: quoted named-counter declaration (nft -f syntax error)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "f'counter name \"{_NFT_COUNTER_DENIED}\" '",
        "f'counter name \"{_NFT_COUNTER_DENIED}\" accept '",
        "nutclient: verdict added to the denied counter rule (opens 3493 to all)",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    new_text = new_text[:table_at] + objects + new_text[table_at:]",
        "    new_text = new_text[:insert_at] + objects + new_text[insert_at:]",
        "nutclient: counter objects inserted at chain scope, not table scope",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        'if counter.get("table") != table or counter.get("family") != family:\n'
        "            continue",
        "if False:\n            continue",
        "nutclient: parse_nft_counters ignores the table/family scope",
    ),
    (
        "src/ups_orchestrator/nutclient.py",
        "    if rc != 0:\n        return False, {}\n"
        "    return True, parse_nft_counters(out, table, family)",
        "    return True, parse_nft_counters(out, table, family)",
        "nutclient: read_nft_counters cannot distinguish unreadable from absent",
    ),
    (
        "src/ups_orchestrator/cli.py",
        "    rc = _monitor_verify_machine(cfg, argv, stat_fn)\n"
        "    _say_firewall_counters()\n    return rc",
        "    rc = _monitor_verify_machine(cfg, argv, stat_fn)\n"
        "    readable, _c = nutclient.read_nft_counters(_NFT_TABLE, _monitor_run_nft_read)\n"
        "    _say_firewall_counters()\n    return rc if readable else 2",
        "cli: a firewall diagnostic failure changes monitor verify's exit code",
    ),
]


def main() -> int:
    # Refuse to start while another run holds the tree. Concurrent runs corrupt
    # each other's restores; see the note beside _LOCK.
    try:
        fd = os.open(_LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        print(
            f"another mutation run holds {_LOCK}. This harness patches src/ in place,\n"
            "so two runs corrupt each other. Wait for it, or remove the lock if stale.",
            file=sys.stderr,
        )
        return 2
    os.write(fd, f"{os.getpid()}\n".encode())
    os.close(fd)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, _on_signal)

    try:
        return _run()
    finally:
        _restore_in_flight()
        _LOCK.unlink(missing_ok=True)


def _run() -> int:
    global _IN_FLIGHT
    killed = survived = skipped = 0
    for rel, old, new, desc in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        if old not in original:
            print(f"  SKIP     {desc} (pattern not found — update the harness)")
            skipped += 1
            continue
        _IN_FLIGHT = (path, original)
        path.write_text(original.replace(old, new, 1))
        try:
            # Hard timeout: a mutant can turn a bounded loop into an infinite one
            # (e.g. a guard flip that makes a settle-loop never exit). A hang means
            # the suite *would* have caught it, so count it KILLED — never let a
            # single mutant wedge the whole harness.
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-x", "--no-header"],
                cwd=ROOT,
                capture_output=True,
                text=True,
                timeout=120,
                env=_CHILD_ENV,
            )
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            returncode = -1  # hang == detectable == killed
            print(f"  KILLED   {desc} (suite hung — mutant caused a non-terminating loop)")
        finally:
            path.write_text(original)
            _IN_FLIGHT = None
        if returncode != 0:
            if returncode != -1:
                print(f"  KILLED   {desc}")
            killed += 1
        else:
            print(f"  SURVIVED {desc}   <-- add a test that pins this")
            survived += 1

    total = killed + survived
    pct = 100 * killed // max(1, total)
    print(
        f"\nMutation score: {killed}/{total} killed ({pct}%), "
        f"{survived} survived, {skipped} skipped"
    )
    if skipped:
        print("NOTE: skipped mutants mean the source moved — refresh the patterns.")
    return 1 if survived else 0


if __name__ == "__main__":
    raise SystemExit(main())
