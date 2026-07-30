"""Small durable JSONL logs for UPS forensics."""

from __future__ import annotations

import fcntl
import json
import os
import socket
import time
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from ups_orchestrator.nut import UpsSnapshot


def boot_id() -> str:
    """Return the current Linux boot id when available."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


# These logs hold the ONLY record of what the orchestrator did during an outage — the
# shutdown_attempt/shutdown_result lines that answer "did we push to mt, and when". They
# had the shortest retention of anything in the project: one generation, versus the
# recorder's twenty. An outage burst that crossed max_bytes twice destroyed the evidence
# of the outage that produced it.
DEFAULT_MAX_ROTATIONS = 5


def _rotation_path(path: Path, index: int) -> Path:
    return path.with_suffix(path.suffix + f".{index}")


def _rotate_locked(path: Path, max_bytes: int, max_rotations: int) -> None:
    """Shift generations. Caller MUST hold the lock — see append_jsonl."""
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    if max_rotations <= 0:
        path.unlink(missing_ok=True)
        return
    _rotation_path(path, max_rotations).unlink(missing_ok=True)
    for index in range(max_rotations - 1, 0, -1):
        source = _rotation_path(path, index)
        if source.exists():
            source.replace(_rotation_path(path, index + 1))
    path.replace(_rotation_path(path, 1))


def append_jsonl(
    path: Path,
    record: dict[str, object],
    *,
    max_bytes: int = 10_000_000,
    max_rotations: int = DEFAULT_MAX_ROTATIONS,
) -> None:
    """Append one JSON object and fsync it so abrupt power loss leaves evidence.

    Rotation and the append happen under an exclusive lock on a sidecar file, because
    these paths have TWO writers in the shipped deployment: klein's ``watch`` loop and
    the ``nut`` user's upssched dispatcher. Unlocked, the stat-then-rename interleaved:

        A stats 10MB, renames events.jsonl -> events.jsonl.1
        A opens "a", creating a fresh events.jsonl, writes one record
        B (which stat'd before A's rename) renames THAT one-record file over .1

    Net: the full generation is unlinked and .1 holds a single line. The window only
    opens when the log crosses max_bytes — which is exactly when an outage is generating
    a burst, i.e. when the evidence matters most.

    The lock is a SIDECAR, not the log itself: the log gets renamed out from under us
    during rotation, so a lock held on it would stop protecting the path the moment it
    was needed. ``flock`` is advisory and per-open-file-description, which is what makes
    the sidecar work across both writers.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")
    with open(lock_path, "a+") as lock:
        # A filesystem without flock must not cost us the record. Proceeding unlocked
        # risks a narrow rotation window; refusing to write loses the line outright.
        with suppress(OSError):
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        _rotate_locked(path, max_bytes, max_rotations)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def base_record(kind: str) -> dict[str, object]:
    """Common envelope for local logs."""
    return {
        "kind": kind,
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "unix_time": time.time(),
        "boot_id": boot_id(),
        "host": socket.gethostname(),
    }


def snapshot_payload(snap: UpsSnapshot | None) -> dict[str, int | float | str | None] | None:
    """Return compact snapshot fields safe for local logs and Discord context."""
    if snap is None:
        return None
    return {
        "status": snap.status,
        "charge": snap.charge,
        "runtime_seconds": snap.runtime_seconds,
        "load": snap.load,
        "input_voltage": snap.input_voltage,
        "output_voltage": snap.output_voltage,
        "realpower_nominal": snap.realpower_nominal,
        "estimated_load_watts": snap.estimated_load_watts,
        "load_margin_percent": snap.load_margin_percent,
        "load_level": snap.load_level,
    }


def append_event(
    path: Path,
    event: str,
    *,
    ups_name: str,
    ups_label: str,
    message: str = "",
    snapshot: UpsSnapshot | None = None,
    data: dict[str, object] | None = None,
    max_bytes: int = 10_000_000,
) -> None:
    """Append one UPS event/decision record."""
    record = base_record("ups_event")
    record.update(
        {
            "event": event,
            "ups": ups_name,
            "label": ups_label,
            "message": message,
            "snapshot": snapshot_payload(snapshot),
            "data": data or {},
        }
    )
    append_jsonl(path, record, max_bytes=max_bytes)
