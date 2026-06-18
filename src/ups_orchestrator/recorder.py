"""High-frequency UPS telemetry recorder."""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime
from pathlib import Path

from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot, read_snapshot


def _boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def _snapshot_payload(snap: UpsSnapshot) -> dict[str, int | float | str | None]:
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


def build_record(
    cfg: Config,
    *,
    snapshot_reader: Callable[[str], UpsSnapshot] = read_snapshot,
) -> dict[str, object]:
    """Build one JSON-serializable UPS sample record."""
    return {
        "time": datetime.now().astimezone().isoformat(timespec="seconds"),
        "unix_time": time.time(),
        "boot_id": _boot_id(),
        "upses": {name: _snapshot_payload(snapshot_reader(name)) for name in cfg.upses},
    }


def _rotate(path: Path, max_bytes: int) -> None:
    if max_bytes <= 0:
        return
    try:
        if path.stat().st_size < max_bytes:
            return
    except FileNotFoundError:
        return
    rotated = path.with_suffix(path.suffix + ".1")
    with suppress(FileNotFoundError):
        rotated.unlink()
    path.rename(rotated)


def append_record(path: Path, record: dict[str, object], *, max_bytes: int) -> None:
    """Append and fsync one sample so abrupt power loss leaves useful evidence."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _rotate(path, max_bytes)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def run(
    cfg: Config,
    *,
    path: Path,
    interval: float = 1.0,
    max_bytes: int = 50_000_000,
    stop: Callable[[], bool] | None = None,
) -> None:
    """Record UPS telemetry until interrupted."""
    should_stop = stop or (lambda: False)
    sleep_for = max(0.2, interval)
    while not should_stop():
        append_record(path, build_record(cfg), max_bytes=max_bytes)
        deadline = time.monotonic() + sleep_for
        while not should_stop() and time.monotonic() < deadline:
            time.sleep(min(0.2, max(0.0, deadline - time.monotonic())))
