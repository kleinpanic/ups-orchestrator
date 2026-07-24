"""Small durable JSONL logs for UPS forensics."""

from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path

from ups_orchestrator.nut import UpsSnapshot


def boot_id() -> str:
    """Return the current Linux boot id when available."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _rotate(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    # Atomic replace-over-existing (no unlink+rename TOCTOU); matches recorder.py.
    path.replace(rotated)


def append_jsonl(path: Path, record: dict[str, object], *, max_bytes: int = 10_000_000) -> None:
    """Append one JSON object and fsync it so abrupt power loss leaves evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path, max_bytes)
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
