from __future__ import annotations

import json

from ups_orchestrator import state as state_mod
from ups_orchestrator.state import StateStore


def test_save_fsyncs_before_replace(monkeypatch, tmp_path) -> None:
    # Durability: the state tempfile must be fsync'd before the atomic replace.
    calls = {"fsync": 0}
    monkeypatch.setattr(
        state_mod.os, "fsync", lambda _fd: calls.__setitem__("fsync", calls["fsync"] + 1)
    )
    store = StateStore(tmp_path / "state.json")
    store.get("ups1").onbatt_since = 123
    store.save()

    assert calls["fsync"] >= 1
    assert json.loads((tmp_path / "state.json").read_text())["ups1"]["onbatt_since"] == 123


def test_state_save_creates_parent_and_writes_json(tmp_path) -> None:
    path = tmp_path / "missing" / "state.json"
    store = StateStore(path)
    store.get("ups1").onbatt_since = 123

    store.save()

    assert json.loads(path.read_text()) == {
        "ups1": {
            "recent_loads": [],
            "last_load_step_notified": None,
            "last_tick_notified": None,
            "last_status": None,
            "onbatt_since": 123,
            "shutdowns_sent": [],
        }
    }
