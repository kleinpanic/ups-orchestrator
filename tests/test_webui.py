from __future__ import annotations

import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer

from conftest import make_ups
from ups_orchestrator import webui
from ups_orchestrator.config import Config
from ups_orchestrator.nut import UpsSnapshot

_SNAP = UpsSnapshot(
    "OL",
    100,
    1800,
    25,
    120.0,
    output_voltage=119.0,
    realpower_nominal=900,
    battery_voltage=27.0,
    battery_voltage_nominal=24.0,
    alarm="none",
)


def test_status_payload_shape(monkeypatch) -> None:
    monkeypatch.setattr(webui, "read_snapshot", lambda _n: _SNAP)
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    payload = webui.status_payload(cfg, now=1000.0)
    assert payload["time"] == 1000.0
    (u,) = payload["upses"]
    assert u["label"] == "Test ups1"
    assert u["watts"] == 225 and u["headroom_watts"] == 675
    assert u["battery_voltage"] == 27.0 and u["battery_voltage_percent"] == 112


def test_history_payload_downsamples(tmp_path) -> None:
    samples = tmp_path / "samples.jsonl"
    with samples.open("w") as fh:
        fh.write(
            json.dumps({"unix_time": 0.0, "upses": {"ups1": {"estimated_load_watts": 0}}}) + "\n"
        )
        for i in range(1500):
            fh.write(
                json.dumps(
                    {
                        "unix_time": 1000.0 + i,
                        "upses": {"ups1": {"estimated_load_watts": 100 + i % 50}},
                    }
                )
                + "\n"
            )
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    payload = webui.history_payload(cfg, samples, hours=999, now=3000.0)
    series = payload["series"]["ups1"]
    assert 0 < len(series) <= 600  # downsampled
    assert all(len(pt) == 2 for pt in series)


def test_http_endpoints_round_trip(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(webui, "read_snapshot", lambda _n: _SNAP)
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    samples = tmp_path / "samples.jsonl"
    samples.write_text("")
    server = ThreadingHTTPServer(("127.0.0.1", 0), webui.make_handler(cfg, samples))
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/status", timeout=5) as r:
            data = json.loads(r.read())
            assert data["upses"][0]["status"] == "OL"
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5) as r:
            assert b"UPS Power" in r.read()
        # /api/history with an hours query — exercises the clamp/parse branch.
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/history?hours=12", timeout=5
        ) as r:
            assert "series" in json.loads(r.read())
        req = urllib.request.Request(f"http://127.0.0.1:{port}/nope")
        try:
            urllib.request.urlopen(req, timeout=5)
            raise AssertionError("expected 404")
        except urllib.error.HTTPError as exc:
            assert exc.code == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
