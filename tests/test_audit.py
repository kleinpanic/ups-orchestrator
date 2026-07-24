from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeNotifier, make_ups
from ups_orchestrator import audit
from ups_orchestrator.config import Config
from ups_orchestrator.notify import DeliveryResult, Level, Notification
from ups_orchestrator.nut import UpsSnapshot


class _FlakyNotifier:
    """Fails the first ``fail_times`` sends, then succeeds. Tracks call count."""

    def __init__(self, fail_times: int) -> None:
        self.calls = 0
        self.fail_times = fail_times

    def send(self, _note: Notification) -> DeliveryResult:
        self.calls += 1
        if self.calls <= self.fail_times:
            return DeliveryResult(configured=True, ok=False, attempts=3, error="down")
        return DeliveryResult(configured=True, ok=True, attempts=1, status_code=204)


def test_audit_reports_power_loss_without_shutdown_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"ups1": {"shutdowns_sent": []}}))

    monkeypatch.setattr(
        audit,
        "_journal_since",
        lambda _since: [
            "Jun 18 host kernel: FAT-fs: Volume was not properly unmounted. Dirty bit is set.",
            "Jun 18 host usbhid-ups[1]: UPS cyberpower2: OL+DISCHRG",
        ],
    )
    monkeypatch.setattr(
        audit,
        "_list_boots",
        lambda: ["-1 boot-a Thu 2026-06-18 15:11", "0 boot-b Thu 2026-06-18 16:26"],
    )
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot(
            "OL", 100, 1800, 30, 120.0, output_voltage=120.0, realpower_nominal=900
        ),
    )

    result = audit.build_audit(cfg, since="2026-06-18 00:00:00", limit=20, state_path=state_path)

    assert result.power_loss_count == 1
    assert result.ups_event_count == 1
    assert result.shutdown_action_count == 0
    assert "without matching orchestrator shutdown or UPS killpower evidence" in result.text
    assert "shutdowns_sent=none" in result.text
    assert "est. to 0% 30m 0s" in result.text


def test_audit_reports_suspicious_recorder_gap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({"ups1": {"shutdowns_sent": []}}))
    samples = tmp_path / "samples.jsonl"
    samples.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "boot_id": "old-boot",
                        "time": "2026-06-18T17:09:40-04:00",
                        "unix_time": 1000.0,
                        "upses": {
                            "ups1": {
                                "status": "OL",
                                "charge": 100,
                                "runtime_seconds": 1800,
                                "load": 30,
                                "input_voltage": 120.0,
                                "output_voltage": 120.0,
                            }
                        },
                    }
                ),
                json.dumps(
                    {
                        "boot_id": "new-boot",
                        "time": "2026-06-18T17:10:20-04:00",
                        "unix_time": 1040.0,
                        "upses": {},
                    }
                ),
            ]
        )
        + "\n"
    )
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "new-boot")
    monkeypatch.setattr(audit, "_journal_since", lambda _since: [])
    monkeypatch.setattr(audit, "_list_boots", lambda: [])
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 1800, 0, 120.0),
    )

    result = audit.build_audit(cfg, state_path=state_path, sample_path=samples)

    assert "Recorder Boot-Gap Analysis" in result.text
    assert "suspicious for a physical power-path problem" in result.text
    assert "UPSes healthy/online" not in result.text  # wording remains concrete, not vague.


def test_sample_gap_streams_all_numeric_rotations_oldest_first(tmp_path: Path) -> None:
    samples = tmp_path / "samples.jsonl"
    (tmp_path / "samples.jsonl.2").write_text(
        json.dumps({"boot_id": "old-boot", "unix_time": 1000, "time": "old", "upses": {}}) + "\n"
    )
    (tmp_path / "samples.jsonl.1").write_text("not-json\n")
    samples.write_text(
        json.dumps({"boot_id": "new-boot", "unix_time": 1040, "time": "new", "upses": {}}) + "\n"
    )

    result = audit.sample_gap_report(samples, current_boot_id="new-boot")

    assert any(line.startswith("last sample before reboot: old ") for line in result.lines)
    assert any(line.startswith("first sample after reboot: new ") for line in result.lines)


def test_audit_redacts_discord_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    monkeypatch.setattr(
        audit,
        "_journal_since",
        lambda _since: [
            "Jun 18 host app: https://discord.com/api/webhooks/secret/token shutdown sent"
        ],
    )
    monkeypatch.setattr(audit, "_list_boots", lambda: [])
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 1800, 0, 120.0),
    )

    result = audit.build_audit(cfg)

    assert "<discord-webhook>" in result.text
    assert "secret/token" not in result.text


def test_boot_audit_sends_once_for_unclean_boot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    notifier = FakeNotifier()
    marker = tmp_path / "boot-audit.json"
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(
        audit,
        "_journal_current_boot",
        lambda: [
            "Jun 18 host systemd-journald[1]: system.journal corrupted or uncleanly shut down",
        ],
    )
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 1800, 10, 120.0, realpower_nominal=900),
    )

    first = audit.send_boot_audit(cfg, notifier, marker_path=marker)
    second = audit.send_boot_audit(cfg, notifier, marker_path=marker)

    assert first.sent is True
    assert second.sent is False
    assert len(notifier.sent) == 1
    assert notifier.sent[0].level is Level.CRITICAL


def test_boot_audit_retries_after_failed_send(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # A failed delivery must NOT write the marker, so the alert retries next run
    # instead of being suppressed forever (network often down right after boot).
    cfg = Config(webhook_url="", upses={"ups1": make_ups("ups1")})
    notifier = _FlakyNotifier(fail_times=1)
    marker = tmp_path / "boot-audit.json"
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(
        audit,
        "_journal_current_boot",
        lambda: [
            "Jun 18 host systemd-journald[1]: system.journal corrupted or uncleanly shut down"
        ],
    )
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 1800, 10, 120.0, realpower_nominal=900),
    )

    first = audit.send_boot_audit(cfg, notifier, marker_path=marker)
    assert first.sent is False
    assert not marker.exists()  # not marked → will retry

    second = audit.send_boot_audit(cfg, notifier, marker_path=marker)
    assert second.sent is True
    assert marker.exists()  # delivered → now marked

    third = audit.send_boot_audit(cfg, notifier, marker_path=marker)
    assert third.sent is False  # already delivered this boot
    assert notifier.calls == 2  # third short-circuits on the marker, no send
