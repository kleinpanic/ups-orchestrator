from __future__ import annotations

from ups_orchestrator.events import charge_bar
from ups_orchestrator.notify import (
    AuditedNotifier,
    DeliveryResult,
    DiscordWebhookNotifier,
    Level,
    Notification,
    NullNotifier,
    build_notifier,
)


def test_charge_bar() -> None:
    assert charge_bar(100) == "▰" * 10
    assert charge_bar(0) == "▱" * 10
    assert charge_bar(50) == "▰▰▰▰▰▱▱▱▱▱"
    assert charge_bar(150) == "▰" * 10  # clamped


def test_build_notifier_selects_null_when_empty() -> None:
    assert isinstance(build_notifier(""), NullNotifier)
    assert isinstance(build_notifier("https://x"), DiscordWebhookNotifier)


def test_embed_payload_structure() -> None:
    n = DiscordWebhookNotifier("https://x", username="Bot", avatar_url="https://a.png", host="pi")
    note = Notification(
        title="t", body="b", level=Level.CRITICAL, fields=[("Status", "OB"), ("Load", "5%")]
    )
    payload = n._payload(note)
    assert payload["username"] == "Bot"
    assert payload["avatar_url"] == "https://a.png"
    embed = payload["embeds"][0]
    assert embed["title"] == "t"
    assert embed["color"] == Level.CRITICAL.value
    assert embed["footer"]["text"] == "UPS Orchestrator"
    assert embed["author"]["name"] == "⚡ Bot"
    assert "timestamp" in embed
    assert [f["name"] for f in embed["fields"]] == [
        "Severity",
        "Host",
        "Delivery",
        "Status",
        "Load",
    ]
    assert embed["fields"][1]["value"] == "pi"
    assert all(f["inline"] for f in embed["fields"])


def test_embed_truncates_long_title() -> None:
    n = DiscordWebhookNotifier("https://x")
    long_title = "z" * 500
    embed = n._embed(Notification(title=long_title))
    assert len(embed["title"]) == 256
    assert embed["title"].endswith("…")


def test_null_notifier_reports_not_configured() -> None:
    result = NullNotifier().send(Notification(title="test"))
    assert result.configured is False
    assert result.ok is False
    assert result.error == "no notifier configured"


def test_audited_notifier_writes_delivery_result(tmp_path) -> None:
    class Inner:
        def send(self, note: Notification) -> DeliveryResult:
            assert note.title == "delivery"
            return DeliveryResult(configured=True, ok=True, attempts=2, status_code=204)

    path = tmp_path / "notifications.jsonl"
    result = AuditedNotifier(Inner(), path).send(
        Notification(title="delivery", level=Level.SUCCESS)
    )

    assert result.ok is True
    line = path.read_text()
    assert '"title": "delivery"' in line
    assert '"attempts": 2' in line
    assert '"ok": true' in line
