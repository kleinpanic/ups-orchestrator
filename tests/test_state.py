from __future__ import annotations

import json

from ups_orchestrator.state import StateStore


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
