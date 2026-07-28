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
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent

# Never let a mutant's bytecode outlive the source restore. Without this, a
# restored .py whose mtime lands in the same second as the mutated write keeps
# the mutated .pyc cached, poisoning the next clean run with a phantom failure.
_CHILD_ENV = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}

# (file, original_substring, mutated_substring, description)
MUTATIONS: list[tuple[str, str, str, str]] = [
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
]


def main() -> int:
    killed = survived = skipped = 0
    for rel, old, new, desc in MUTATIONS:
        path = ROOT / rel
        original = path.read_text()
        if old not in original:
            print(f"  SKIP     {desc} (pattern not found — update the harness)")
            skipped += 1
            continue
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
