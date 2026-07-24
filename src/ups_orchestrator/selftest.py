"""Scheduled UPS battery self-tests via NUT ``upscmd``.

Starts a quick battery test and polls ``ups.test.result`` until it settles, then
classifies the outcome. Guarded so it never fires while the UPS is on battery (a
test drains the pack). All I/O is injected so the logic unit-tests without a real
UPS or draining a battery.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass

from ups_orchestrator.nut import UpsSnapshot, upscmd

_QUICK_TEST = "test.battery.start.quick"

# outcome -> whether it is a problem worth a warning-level alert
_PROBLEM = {"failed", "aborted", "timeout", "error", "unknown"}


@dataclass(frozen=True)
class SelfTestResult:
    ups: str
    outcome: str  # started|passed|warning|failed|aborted|skipped|error|timeout|unknown
    detail: str

    @property
    def is_problem(self) -> bool:
        return self.outcome in _PROBLEM


def classify(test_result: str) -> str:
    r = test_result.strip().lower()
    if not r or r in ("no test initiated", "not initiated"):
        return "unknown"
    if "progress" in r:
        return "in_progress"
    if "abort" in r or "cancel" in r:
        return "aborted"
    if "fail" in r:
        return "failed"
    if "warn" in r:
        return "warning"
    if "pass" in r or r.startswith("done") or "ok" in r:
        return "passed"
    return "unknown"


def run_selftest(
    ups_name: str,
    snapshot: UpsSnapshot,
    *,
    user: str,
    password: str,
    read_result: Callable[[str], str | None],
    start: Callable[..., tuple[int, str, str]] = upscmd,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    timeout: float = 180.0,
    poll: float = 5.0,
) -> SelfTestResult:
    """Start a quick battery test on ``ups_name`` and return its settled outcome."""
    if snapshot.on_battery or snapshot.low_battery:
        return SelfTestResult(ups_name, "skipped", "UPS on/low battery — a test would drain it")

    rc, out, err = start(ups_name, _QUICK_TEST, user=user, password=password)
    if rc != 0:
        return SelfTestResult(
            ups_name, "error", (err or out or "upscmd failed").splitlines()[0][:200]
        )

    deadline = clock() + timeout
    last = ""
    while clock() < deadline:
        last = (read_result(ups_name) or "").strip()
        outcome = classify(last)
        # Keep polling while the test runs or hasn't registered a result yet.
        if outcome in ("in_progress", "unknown"):
            sleep(poll)
            continue
        return SelfTestResult(ups_name, outcome, last or outcome)
    return SelfTestResult(
        ups_name, "timeout", f"still '{last or 'in progress'}' after {int(timeout)}s"
    )
