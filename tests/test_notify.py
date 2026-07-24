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


# --- delivery retry/backoff -------------------------------------------------
import io  # noqa: E402
import urllib.error  # noqa: E402

import ups_orchestrator.notify as notify_mod  # noqa: E402


class _Resp:
    def __init__(self, status: int) -> None:
        self._status = status

    def __enter__(self) -> _Resp:
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def getcode(self) -> int:
        return self._status


def _http_error(code: int, headers: dict[str, str] | None = None) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "err", headers or {}, io.BytesIO(b"{}"))


def _patch_urlopen(monkeypatch, seq):
    calls = {"n": 0}

    def fake_urlopen(_req, timeout=0.0):
        item = seq[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify_mod.time, "sleep", lambda _s: None)
    return calls


def test_notify_retries_5xx_then_succeeds(monkeypatch) -> None:
    _patch_urlopen(monkeypatch, [_http_error(503), _http_error(502), _Resp(204)])
    res = DiscordWebhookNotifier("https://x", max_attempts=3).send(Notification(title="t"))
    assert res.ok is True
    assert res.status_code == 204
    assert res.attempts == 3


def test_notify_429_honors_retry_after_header(monkeypatch) -> None:
    slept: list[float] = []
    calls = {"n": 0}
    seq = [_http_error(429, {"Retry-After": "2"}), _Resp(204)]

    def fake_urlopen(_req, timeout=0.0):
        item = seq[calls["n"]]
        calls["n"] += 1
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(notify_mod.time, "sleep", lambda s: slept.append(s))
    res = DiscordWebhookNotifier("https://x", max_attempts=3).send(Notification(title="t"))
    assert res.ok is True
    assert slept == [2.0]  # honored the header, not the default backoff


def test_notify_gives_up_after_max_attempts(monkeypatch) -> None:
    calls = _patch_urlopen(monkeypatch, [urllib.error.URLError("down")] * 3)
    res = DiscordWebhookNotifier("https://x", max_attempts=3).send(Notification(title="t"))
    assert res.ok is False
    assert res.attempts == 3
    assert calls["n"] == 3


def test_notify_4xx_fails_without_retry(monkeypatch) -> None:
    calls = _patch_urlopen(monkeypatch, [_http_error(400), _Resp(204)])
    res = DiscordWebhookNotifier("https://x", max_attempts=3).send(Notification(title="t"))
    assert res.ok is False
    assert res.status_code == 400
    assert res.attempts == 1
    assert calls["n"] == 1  # no retry on 4xx
