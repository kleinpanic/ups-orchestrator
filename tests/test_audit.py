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

    first = audit.send_boot_audit(cfg, notifier, marker_path=marker, clean_shutdown=False)
    second = audit.send_boot_audit(cfg, notifier, marker_path=marker, clean_shutdown=False)

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

    first = audit.send_boot_audit(cfg, notifier, marker_path=marker, clean_shutdown=False)
    assert first.sent is False
    assert not marker.exists()  # not marked → will retry

    second = audit.send_boot_audit(cfg, notifier, marker_path=marker, clean_shutdown=False)
    assert second.sent is True
    assert marker.exists()  # delivered → now marked

    third = audit.send_boot_audit(cfg, notifier, marker_path=marker, clean_shutdown=False)
    assert third.sent is False  # already delivered this boot
    assert notifier.calls == 2  # third short-circuits on the marker, no send


# --- boot-audit false-positive gates -----------------------------------------
#
# Every line below is copied verbatim from eulerpi5's journal for boot
# 8ed8db4a (2026-07-27 22:15), the boot that produced a bogus
# "recovered from abrupt power loss" alert on a host the operator had powered
# down on purpose to redo cabling.
_REAL_BOOT_NOISE = [
    "1785204918.0 eulerpi5 kernel: EXT4-fs (nvme0n1p2): orphan cleanup on readonly fs",
    "1785204918.1 eulerpi5 systemd-fsck[394]: Dirty bit is set. Fs was not properly"
    " unmounted and some data may be corrupt.",
    "1785204919.0 eulerpi5 systemd-fsck[520]: disk3: recovering journal",
]


def _boot_cfg() -> Config:
    return Config(webhook_url="", upses={"ups1": make_ups("ups1")})


def test_orphan_cleanup_alone_is_not_power_loss_evidence(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # ext4 logs orphan cleanup during the read-only root mount after CLEAN
    # shutdowns too. On its own it must never raise a critical alert.
    notifier = FakeNotifier()
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(
        audit,
        "_journal_current_boot",
        lambda: [_REAL_BOOT_NOISE[0]],
    )

    result = audit.send_boot_audit(
        _boot_cfg(),
        notifier,
        marker_path=tmp_path / "m.json",
        clean_shutdown=False,
    )

    assert result.power_loss_count == 0
    assert result.sent is False
    assert notifier.sent == []


def test_clean_previous_shutdown_suppresses_alert_despite_fsck_noise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The dirty-bit and recovering-journal lines DO match the power-loss
    # patterns, but the previous boot logged systemd's shutdown sequence, so
    # this was a clean reboot and no alert may go out.
    notifier = FakeNotifier()
    marker = tmp_path / "m.json"
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(audit, "_journal_current_boot", lambda: _REAL_BOOT_NOISE)

    result = audit.send_boot_audit(_boot_cfg(), notifier, marker_path=marker, clean_shutdown=True)

    assert result.power_loss_count > 0  # evidence WAS present...
    assert result.sent is False  # ...and was correctly overruled
    assert notifier.sent == []
    assert marker.exists()  # evaluated, so don't re-evaluate this boot


def test_open_maintenance_window_suppresses_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    notifier = FakeNotifier()
    maintenance = tmp_path / "maintenance.json"
    audit.write_maintenance(maintenance, until=2_000.0, reason="recabling the rack")
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(audit, "_journal_current_boot", lambda: _REAL_BOOT_NOISE)

    result = audit.send_boot_audit(
        _boot_cfg(),
        notifier,
        marker_path=tmp_path / "m.json",
        maintenance_path=maintenance,
        clean_shutdown=False,
        now=1_000.0,
    )

    assert result.sent is False
    assert "recabling the rack" in result.text
    assert notifier.sent == []


def test_expired_maintenance_window_does_not_suppress_alert(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The whole point of an expiry: a window the operator forgot to close must
    # stop silencing real outages rather than silencing them forever.
    notifier = FakeNotifier()
    maintenance = tmp_path / "maintenance.json"
    audit.write_maintenance(maintenance, until=500.0, reason="recabling the rack")
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(audit, "_journal_current_boot", lambda: _REAL_BOOT_NOISE)
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _name: UpsSnapshot("OL", 100, 1800, 10, 120.0, realpower_nominal=900),
    )

    result = audit.send_boot_audit(
        _boot_cfg(),
        notifier,
        marker_path=tmp_path / "m.json",
        maintenance_path=maintenance,
        clean_shutdown=False,
        now=1_000.0,
    )

    assert result.sent is True
    assert len(notifier.sent) == 1


def test_within_boot_window_drops_late_journald_rotation_noise() -> None:
    # journald rotated a stale user journal an HOUR into a healthy boot and the
    # line was counted as boot evidence. Only the opening seconds count.
    lines = [
        "1785204918.0 eulerpi5 kernel: mounting root",
        "1785204919.0 eulerpi5 systemd-fsck[520]: disk3: recovering journal",
        "1785208550.0 eulerpi5 systemd-journald[270]: File user-1001.journal"
        " corrupted or uncleanly shut down, renaming and replacing.",
    ]
    kept = audit.within_boot_window(lines, window=120.0)
    assert kept == [
        "eulerpi5 kernel: mounting root",
        "eulerpi5 systemd-fsck[520]: disk3: recovering journal",
    ]


def test_within_boot_window_keeps_lines_with_unparseable_timestamps() -> None:
    # Losing real evidence to a format surprise is worse than an extra line.
    lines = ["Jun 18 host kernel: something", "1785204918.0 host kernel: other"]
    assert audit.within_boot_window(lines) == lines[:1] + ["host kernel: other"]


def test_orphan_cleanup_is_not_a_power_loss_pattern() -> None:
    # Signature-independent guard on the pattern list itself: ext4 emits this on
    # clean shutdowns, so it must never rejoin _POWER_LOSS_PATTERNS.
    line = "eulerpi5 kernel: EXT4-fs (nvme0n1p2): orphan cleanup on readonly fs"
    assert not audit._contains_any(line, audit._POWER_LOSS_PATTERNS)


def test_real_dirty_bit_line_is_still_power_loss_evidence() -> None:
    # The complement: tightening the list must not blind it to genuine evidence.
    line = "systemd-fsck[394]: Dirty bit is set. Fs was not properly unmounted."
    assert audit._contains_any(line, audit._POWER_LOSS_PATTERNS)


# --- maintenance marker hardening (T-03-04 / T-03-06) -------------------------


def test_forged_far_future_window_is_ignored(tmp_path: Path) -> None:
    # /var/lib/ups-orchestrator is group-writable and owned by uid `nut`, which
    # runs the LAN-facing upsd. A marker planted there must not be able to
    # silence outage alerting forever, so the READER caps the horizon.
    marker = tmp_path / "maintenance.json"
    marker.write_text(json.dumps({"until": 9_000_000_000.0, "reason": "pwned"}))

    window = audit.read_maintenance(marker, now=1_000.0)

    assert window.active is False


def test_window_inside_the_cap_is_honoured(tmp_path: Path) -> None:
    # The complement: capping must not break a legitimate declaration.
    marker = tmp_path / "maintenance.json"
    audit.write_maintenance(marker, until=1_000.0 + audit.MAX_MAINTENANCE_SECONDS - 1, reason="ok")

    assert audit.read_maintenance(marker, now=1_000.0).active is True


def test_symlinked_marker_is_ignored_on_read(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere.json"
    real.write_text(json.dumps({"until": 2_000.0, "reason": "planted"}))
    marker = tmp_path / "maintenance.json"
    marker.symlink_to(real)

    assert audit.read_maintenance(marker, now=1_000.0).active is False


def test_write_maintenance_refuses_to_follow_a_symlink(tmp_path: Path) -> None:
    # The metadata-preserving writer resolves symlinks on purpose; that is right
    # for a root-owned /etc config and wrong for a marker in a directory a
    # less-privileged account can write to.
    victim = tmp_path / "victim.json"
    victim.write_text("original")
    marker = tmp_path / "maintenance.json"
    marker.symlink_to(victim)

    with pytest.raises(OSError):
        audit.write_maintenance(marker, until=2_000.0, reason="x")
    assert victim.read_text() == "original"


def test_clear_maintenance_raises_rather_than_reporting_no_window(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Swallowing the error returned the same False as "nothing was open", so the
    # CLI told the operator alerts were ARMED while suppression was still live.
    # The failure is injected rather than staged with chmod, because root ignores
    # a read-only parent directory and the test would invert under `sudo pytest`.
    marker = tmp_path / "maintenance.json"
    marker.write_text(json.dumps({"until": 2_000.0}))

    def _boom(_self: Path) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(Path, "unlink", _boom)
    with pytest.raises(OSError):
        audit.clear_maintenance(marker)


def test_clear_maintenance_returns_false_when_absent(tmp_path: Path) -> None:
    assert audit.clear_maintenance(tmp_path / "nope.json") is False


def test_boolean_until_is_not_treated_as_a_timestamp(tmp_path: Path) -> None:
    # bool is a subclass of int; True would otherwise read as until=1.0.
    marker = tmp_path / "maintenance.json"
    marker.write_text(json.dumps({"until": True}))

    assert audit.read_maintenance(marker, now=0.0).active is False


def test_shutdown_evidence_is_not_limited_to_the_boot_window(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The 120s window scopes FILESYSTEM evidence. The orchestrator's own shutdown
    # actions happen at arbitrary uptime, so windowing them pinned the count to
    # zero — and with it pinned the alert body to "no killpower evidence found"
    # no matter what had actually happened.
    monkeypatch.setattr(audit, "_current_boot_id", lambda: "boot-1")
    monkeypatch.setattr(
        audit,
        "_journal_current_boot",
        lambda: [
            "1785204918.0 host systemd-fsck[1]: Dirty bit is set.",
            "1785299999.0 host ups-orchestrator: shutdown sent to spark",
        ],
    )
    monkeypatch.setattr(
        audit,
        "read_snapshot",
        lambda _n: UpsSnapshot("OL", 100, 1800, 10, 120.0, realpower_nominal=900),
    )

    result = audit.send_boot_audit(
        _boot_cfg(),
        FakeNotifier(),
        marker_path=tmp_path / "m.json",
        clean_shutdown=False,
    )

    assert result.power_loss_count == 1  # windowed
    assert result.shutdown_action_count == 1  # NOT windowed


def test_previous_boot_ended_cleanly_reads_the_real_journal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Every other test injects clean_shutdown=, so the detector itself had no
    # coverage at all and would survive being replaced with `return False`.
    captured: dict[str, list[str]] = {}

    def _fake_run(args: list[str], timeout: float = 0.0) -> list[str]:
        captured["args"] = args
        return ["Jul 27 host systemd-shutdown[1]: Syncing filesystems and block devices."]

    monkeypatch.setattr(audit, "_run", _fake_run)
    assert audit.previous_boot_ended_cleanly() is True
    assert "-b" in captured["args"] and "-1" in captured["args"]


def test_previous_boot_ended_cleanly_is_false_on_an_abrupt_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An abrupt cut leaves the journal simply stopping mid-line: ordinary
    # traffic, no shutdown sequence.
    monkeypatch.setattr(
        audit, "_run", lambda *_a, **_k: ["Jul 27 host NetworkManager: device usb0 activated"]
    )
    assert audit.previous_boot_ended_cleanly() is False


def test_write_maintenance_rejects_a_non_finite_end(tmp_path: Path) -> None:
    # --hours nan passed both the <=0 and >max guards, wrote the marker, then
    # crashed in time.localtime(nan) — a traceback on a safety control.
    with pytest.raises(ValueError):
        audit.write_maintenance(tmp_path / "m.json", until=float("nan"), reason="x")


def test_write_maintenance_creates_its_parent_directory(tmp_path: Path) -> None:
    # The writer this replaced did mkdir(parents=True); a relocated
    # $UPS_ORCH_MAINTENANCE_STATE must keep working.
    marker = tmp_path / "fresh" / "maintenance.json"
    audit.write_maintenance(marker, until=2_000.0, reason="x")
    assert audit.read_maintenance(marker, now=1_000.0).active is True
