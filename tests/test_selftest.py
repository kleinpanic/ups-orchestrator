from __future__ import annotations

from ups_orchestrator.nut import UpsSnapshot
from ups_orchestrator.selftest import classify, run_selftest


def test_classify_outcomes() -> None:
    assert classify("Done and passed") == "passed"
    assert classify("Done and warning") == "warning"
    assert classify("Test failed") == "failed"
    assert classify("Aborted") == "aborted"
    assert classify("In progress") == "in_progress"
    assert classify("No test initiated") == "unknown"
    assert classify("") == "unknown"


def _ok_start(*_a: object, **_k: object) -> tuple[int, str, str]:
    return 0, "OK", ""


def _snap(status: str = "OL") -> UpsSnapshot:
    return UpsSnapshot(status, 100, 1800, 20, 120.0)


def test_run_selftest_polls_until_passed() -> None:
    results = iter(["In progress", "In progress", "Done and passed"])
    r = run_selftest(
        "ups1",
        _snap(),
        user="admin",
        password="pw",
        start=_ok_start,
        read_result=lambda _u: next(results),
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        poll=1.0,
        timeout=100.0,
    )
    assert r.outcome == "passed"
    assert r.is_problem is False


def test_run_selftest_flags_failure() -> None:
    r = run_selftest(
        "ups1",
        _snap(),
        user="admin",
        password="pw",
        start=_ok_start,
        read_result=lambda _u: "Test failed",
        sleep=lambda _s: None,
        clock=lambda: 0.0,
        poll=1.0,
        timeout=100.0,
    )
    assert r.outcome == "failed"
    assert r.is_problem is True


def test_run_selftest_skips_on_battery() -> None:
    called = {"start": 0}

    def _start(*_a: object, **_k: object) -> tuple[int, str, str]:
        called["start"] += 1
        return 0, "", ""

    # Advancing clock + short timeout: a correct guard skips before the poll loop,
    # so the clock is never read. If the guard is ever broken (e.g. the on-battery
    # check flipped or->and), the loop it wrongly enters hits the deadline within a
    # couple of ticks and the assertions below fail *fast* — so that mutant dies by
    # a clean assertion failure, never by hanging the suite.
    ticks = iter(range(0, 100, 5))
    r = run_selftest(
        "ups1",
        _snap("OB DISCHRG"),
        user="admin",
        password="pw",
        start=_start,
        read_result=lambda _u: "x",
        sleep=lambda _s: None,
        clock=lambda: next(ticks),
        poll=1.0,
        timeout=10.0,
    )
    assert r.outcome == "skipped"
    assert called["start"] == 0  # never started a test while on battery


def test_run_selftest_reports_upscmd_error() -> None:
    r = run_selftest(
        "ups1",
        _snap(),
        user="admin",
        password="pw",
        start=lambda *_a, **_k: (1, "", "access denied"),
        read_result=lambda _u: "",
        sleep=lambda _s: None,
        clock=lambda: 0.0,
    )
    assert r.outcome == "error"
    assert "access denied" in r.detail


def test_run_selftest_times_out() -> None:
    clock = iter([0.0, 0.0, 1000.0])  # third read is past the deadline
    r = run_selftest(
        "ups1",
        _snap(),
        user="admin",
        password="pw",
        start=_ok_start,
        read_result=lambda _u: "In progress",
        sleep=lambda _s: None,
        clock=lambda: next(clock),
        poll=1.0,
        timeout=10.0,
    )
    assert r.outcome == "timeout"
