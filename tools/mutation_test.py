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
        '    if not saddrs:\n        return ""\n    members = ", ".join(saddrs)',
        '    if not saddrs:\n        saddrs = ["0.0.0.0"]\n    members = ", ".join(saddrs)',
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
