"""Per-UPS persisted state, written atomically.

NUT invokes the orchestrator once per event, so anything we need to remember
across invocations (when a UPS went on battery, whether we already fired a
one-shot action) lives here. State is keyed by NUT UPS name so multiple UPSes
never clobber each other.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass
class UpsState:
    """Mutable per-UPS bookkeeping."""

    onbatt_since: int | None = None
    r630_shutdown_sent: bool = False
    last_tick_notified: int | None = None

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> UpsState:
        return cls(
            onbatt_since=_opt_int(data.get("onbatt_since")),
            r630_shutdown_sent=bool(data.get("r630_shutdown_sent", False)),
            last_tick_notified=_opt_int(data.get("last_tick_notified")),
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
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
        tmp.replace(self.path)
