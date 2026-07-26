"""Power dashboard helpers — history parsing and Discord multipart upload."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ups_orchestrator.dashboard import (
    _daily_energy_kwh,
    _multipart,
    _read_series,
    post_png,
)


def _write_samples(path: Path, rows: list[tuple[float, dict[str, int]]]) -> None:
    with path.open("w") as fh:
        for t, watts in rows:
            fh.write(
                json.dumps(
                    {
                        "unix_time": t,
                        "upses": {n: {"estimated_load_watts": w} for n, w in watts.items()},
                    }
                )
                + "\n"
            )


def test_read_series_filters_by_cutoff_and_sorts(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    _write_samples(
        path,
        [
            (1000.0, {"ups1": 100, "ups2": 5}),
            (2000.0, {"ups1": 120, "ups2": 6}),
            (3000.0, {"ups1": 90, "ups2": 7}),
        ],
    )
    # First line is dropped as a possibly-partial post-seek line, so cut before it.
    series = _read_series(path, ["ups1", "ups2"], cutoff=1500.0)
    assert series["ups1"] == [(2000.0, 120), (3000.0, 90)]
    assert series["ups2"] == [(2000.0, 6), (3000.0, 7)]


def test_read_series_skips_missing_ups_and_bad_lines(tmp_path) -> None:
    path = tmp_path / "samples.jsonl"
    with path.open("w") as fh:
        fh.write("header-ignored\n")  # dropped by the seek-tail slice
        fh.write(
            json.dumps({"unix_time": 2000.0, "upses": {"ups1": {"estimated_load_watts": 50}}})
            + "\n"
        )
        fh.write("{not json}\n")
        fh.write(
            json.dumps({"unix_time": 2500.0, "upses": {"ups1": {"estimated_load_watts": 60}}})
            + "\n"
        )
    series = _read_series(path, ["ups1", "ups2"], cutoff=0.0)
    assert series["ups1"] == [(2000.0, 50), (2500.0, 60)]
    assert series["ups2"] == []


def test_daily_energy_kwh_integrates_watts_over_time() -> None:
    # 1000 W held across a 3600 s interval = 1.0 kWh, on the sample's local day.
    t0 = 1_700_000_000.0
    series = {"ups1": [(t0, 1000), (t0 + 3600, 1000)], "ups2": [(t0, 500), (t0 + 3600, 500)]}
    daily = _daily_energy_kwh(series, ["ups1", "ups2"], max_gap=4000)
    assert len(daily) == 1
    _label, kwh = daily[0]
    assert round(kwh["ups1"], 3) == 1.0
    assert round(kwh["ups2"], 3) == 0.5


def test_daily_energy_kwh_drops_long_gaps() -> None:
    # A 10 000 s gap (recorder restart) must not be integrated as usage.
    t0 = 1_700_000_000.0
    series = {"ups1": [(t0, 1000), (t0 + 10_000, 1000)]}
    assert _daily_energy_kwh(series, ["ups1"], max_gap=120.0) == []


def test_multipart_frames_payload_and_file() -> None:
    body, boundary = _multipart(b"\x89PNG-bytes", {"username": "bot", "content": "hi"}, "d.png")
    text = body.decode("latin-1")
    assert f"--{boundary}" in text
    assert 'name="payload_json"' in text
    assert 'name="files[0]"; filename="d.png"' in text
    assert '"username": "bot"' in text
    assert "\x89PNG-bytes" in text
    assert text.rstrip().endswith(f"--{boundary}--")


def test_post_png_noop_without_webhook() -> None:
    assert post_png("", b"x") == (False, 0)


def test_post_png_sets_user_agent(monkeypatch) -> None:
    # Discord's edge 403s the default python-urllib UA; the custom one is required.
    import ups_orchestrator.dashboard as dash

    seen: dict[str, str | None] = {}

    class _Resp:
        status = 200

        def __enter__(self) -> _Resp:
            return self

        def __exit__(self, *_a: object) -> bool:
            return False

    def _fake_urlopen(req: object, timeout: float = 0) -> _Resp:
        seen["ua"] = req.headers.get("User-agent")  # type: ignore[attr-defined]
        seen["ctype"] = req.headers.get("Content-type")  # type: ignore[attr-defined]
        return _Resp()

    monkeypatch.setattr(dash.urllib.request, "urlopen", _fake_urlopen)
    ok, status = post_png("https://discord/webhook", b"png", content="hi")
    assert (ok, status) == (True, 200)
    assert seen["ua"] and "ups-orchestrator" in seen["ua"]
    assert seen["ctype"] and "multipart/form-data" in seen["ctype"]


@pytest.mark.allow_subprocess  # matplotlib's font manager shells out to fc-list
def test_render_png_smoke(monkeypatch, tmp_path) -> None:
    # Exercises the matplotlib render path; skipped where matplotlib is absent
    # (CI installs the [dashboard] extra before pytest so this runs there).
    #
    # The subprocess opt-out is real, not a workaround: on a cold font cache
    # matplotlib runs `fc-list` to enumerate system fonts. A warm ~/.cache
    # hides it locally and a fresh CI runner does not, which is exactly the
    # environment-dependent spawn the tripwire exists to surface. Nothing in
    # ups_orchestrator spawns here — the transport seams are already faked
    # below — so the escape hatch is scoped to matplotlib's own startup.
    pytest.importorskip("matplotlib")
    import ups_orchestrator.dashboard as dash
    from conftest import make_ups
    from ups_orchestrator.config import Config
    from ups_orchestrator.nut import UpsSnapshot

    monkeypatch.setattr(
        dash,
        "read_snapshot",
        lambda _n: UpsSnapshot(
            "OL",
            100,
            600,
            25,
            120.0,
            realpower_nominal=900,
            battery_voltage=27.0,
            battery_voltage_nominal=24.0,
        ),
    )
    samples = tmp_path / "samples.jsonl"
    with samples.open("w") as fh:
        for i in range(6):
            fh.write(
                json.dumps(
                    {"unix_time": 1000.0 + i, "upses": {"ups1": {"estimated_load_watts": 200 + i}}}
                )
                + "\n"
            )
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    png = dash.render_png(cfg, samples, hours=1, host="ci", now=1005.0)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"  # valid PNG signature
