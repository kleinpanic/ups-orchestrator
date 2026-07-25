"""Per-UPS persisted state, written atomically.

NUT invokes the orchestrator once per event, so anything we need to remember
across invocations (when a UPS went on battery, whether we already fired a
one-shot action) lives here. State is keyed by NUT UPS name so multiple UPSes
never clobber each other.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class UpsState:
    """Mutable per-UPS bookkeeping."""

    onbatt_since: int | None = None
    shutdowns_sent: list[str] = field(default_factory=list)
    last_tick_notified: int | None = None
    last_status: str | None = None
    recent_loads: list[int] = field(default_factory=list)
    last_load_step_notified: int | None = None
    # True once the ON BATTERY page has been sent for the current outage — keeps a
    # transfer that clears within the notify grace (a blip or our own self-test)
    # from ever paging, and stops the poll loop re-paging a persistent outage.
    onbatt_notified: bool = False

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> UpsState:
        raw_sent = data.get("shutdowns_sent", [])
        sent = [str(x) for x in raw_sent] if isinstance(raw_sent, list) else []
        raw_recent = data.get("recent_loads", [])
        recent = (
            [int(x) for x in raw_recent if isinstance(x, (int, float))]
            if isinstance(raw_recent, list)
            else []
        )
        if not recent:
            # Pre-window state files stored a single last_load.
            legacy = _opt_int(data.get("last_load"))
            if legacy is not None:
                recent = [legacy]
        return cls(
            onbatt_since=_opt_int(data.get("onbatt_since")),
            shutdowns_sent=sent,
            last_tick_notified=_opt_int(data.get("last_tick_notified")),
            last_status=str(data["last_status"]) if data.get("last_status") is not None else None,
            recent_loads=recent,
            last_load_step_notified=_opt_int(data.get("last_load_step_notified")),
            onbatt_notified=bool(data.get("onbatt_notified", False)),
        )


def _opt_int(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


class StateStore:
    """Loads/saves a ``{ups_name: UpsState}`` map from a JSON file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._states: dict[str, UpsState] = {}
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self._states = {
                name: UpsState.from_dict(data)
                for name, data in raw.items()
                if isinstance(data, dict)
            }

    def get(self, ups_name: str) -> UpsState:
        """Return the state for ``ups_name``, creating a blank one on first use."""
        return self._states.setdefault(ups_name, UpsState())

    def save(self) -> None:
        """Atomically persist all UPS states (write to temp, then replace)."""
        payload = {name: asdict(st) for name, st in self._states.items()}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as tmp:
                tmp_path = Path(tmp.name)
                json.dump(payload, tmp, indent=2, sort_keys=True)
                tmp.write("\n")
                # Durability: flush userspace + kernel buffers before the atomic
                # rename, so a power loss right after replace() can't leave the
                # state file pointing at a zero-length/partial inode. Matches the
                # fsync in recorder.py/jsonlog.py.
                tmp.flush()
                os.fsync(tmp.fileno())
            tmp_path.replace(self.path)
        finally:
            if tmp_path is not None and tmp_path.exists():
                tmp_path.unlink(missing_ok=True)
