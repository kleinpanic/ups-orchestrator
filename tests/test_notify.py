from __future__ import annotations

from ups_orchestrator.events import charge_bar
from ups_orchestrator.notify import (
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
    assert embed["footer"]["text"] == "pi"
    assert embed["author"]["name"] == "⚡ Bot"
    assert "timestamp" in embed
    assert [f["name"] for f in embed["fields"]] == ["Status", "Load"]
    assert all(f["inline"] for f in embed["fields"])


def test_embed_truncates_long_title() -> None:
    n = DiscordWebhookNotifier("https://x")
    long_title = "z" * 500
    embed = n._embed(Notification(title=long_title))
    assert len(embed["title"]) == 256
    assert embed["title"].endswith("…")
