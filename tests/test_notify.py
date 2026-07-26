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


# --- F1: `send` never raises, and the webhook token never leaves this module ---
#
# The webhook URL's last path segment is bearer-equivalent: anyone who reads one
# can post to that channel indefinitely. Every assertion below checks BOTH halves —
# that nothing escaped, and that the token is not in what was logged or returned.

_SECRET = "TOKENabcdef0123456789"
_TYPO_URL = f"https//discord.com/api/webhooks/1/{_SECRET}"  # missing colon
_FILE_URL = "file:///etc/hostname"


def test_send_does_not_raise_on_a_malformed_url(caplog) -> None:
    """`urllib.request.Request` raises ValueError on a scheme-less URL.

    ValueError is not an OSError, so the loop's `(URLError, OSError)` clause never
    saw it. `_notify_degraded` is the one `send` on the daemon's startup path
    outside every guard — and it fires ONLY when the config is degraded — so this
    turned a config typo into a `watch` that never reached its poll loop.
    """
    n = DiscordWebhookNotifier(_TYPO_URL, max_attempts=1)
    with caplog.at_level("WARNING"):
        result = n.send(Notification(title="t"))
    assert result.ok is False
    assert result.configured is True
    assert _SECRET not in caplog.text
    assert _SECRET not in result.error


def test_send_does_not_raise_on_a_file_url(caplog, tmp_path) -> None:
    """`file://` does not raise in urlopen — it SUCCEEDS.

    `resp.getcode()` then returns None and `200 <= None` is a TypeError, which is
    likewise not an OSError. Reported as undelivered rather than escaping.
    """
    probe = tmp_path / "probe.txt"
    probe.write_text("x")
    n = DiscordWebhookNotifier(f"file://{probe}", max_attempts=1)
    with caplog.at_level("WARNING"):
        result = n.send(Notification(title="t"))
    assert result.ok is False
    assert "not an HTTP endpoint" in result.error


def test_send_never_raises_whatever_urllib_does(caplog, monkeypatch) -> None:
    """The catch-all backstop: the promise must not rest on an exception list.

    Enumerating classes is exactly how ValueError and TypeError were missed, so a
    surprise from a future urllib must still not reach the daemon.
    """
    import ups_orchestrator.notify as notify_mod

    def _surprise(*_a: object, **_k: object) -> None:
        raise RecursionError(f"something new about {_SECRET}")

    monkeypatch.setattr(notify_mod.urllib.request, "Request", _surprise)
    n = DiscordWebhookNotifier(f"https://discord.com/api/webhooks/1/{_SECRET}", max_attempts=3)

    with caplog.at_level("WARNING"):
        result = n.send(Notification(title="t"))

    assert result.ok is False
    assert "RecursionError" in result.error
    assert _SECRET not in caplog.text, "the exception message must not be logged verbatim"
    assert _SECRET not in result.error


def test_send_redacts_the_token_from_a_transport_error(caplog, monkeypatch) -> None:
    """`DeliveryResult.error` is PERSISTED to notifications.jsonl by AuditedNotifier.

    An unredacted transport error therefore puts the token on disk as well as in
    the journal.
    """
    import urllib.error

    import ups_orchestrator.notify as notify_mod

    url = f"https://discord.com/api/webhooks/1/{_SECRET}"

    def _boom(*_a: object, **_k: object) -> None:
        raise urllib.error.URLError(f"failed to open {url}")

    monkeypatch.setattr(notify_mod.urllib.request, "urlopen", _boom)
    monkeypatch.setattr(notify_mod.time, "sleep", lambda _s: None)
    n = DiscordWebhookNotifier(url, max_attempts=1)

    with caplog.at_level("WARNING"):
        result = n.send(Notification(title="t"))

    assert result.ok is False
    assert _SECRET not in result.error
    assert _SECRET not in caplog.text
    assert "redacted" in result.error


def test_build_notifier_fails_closed_on_an_unusable_url(caplog) -> None:
    with caplog.at_level("ERROR"):
        notifier = build_notifier(_TYPO_URL)
    assert isinstance(notifier, NullNotifier)
    assert _SECRET not in caplog.text, "the log line reporting it must not echo the credential"


def test_build_notifier_fails_closed_on_a_non_http_scheme(caplog) -> None:
    with caplog.at_level("ERROR"):
        assert isinstance(build_notifier(_FILE_URL), NullNotifier)
    assert isinstance(build_notifier("ftp://example.com/x"), NullNotifier)
    assert isinstance(build_notifier("notascheme"), NullNotifier)
    # ...and a real one is still a real one.
    real = build_notifier("https://discord.com/api/webhooks/1/x")
    assert isinstance(real, DiscordWebhookNotifier)


def test_usable_webhook_url_predicate() -> None:
    from ups_orchestrator.notify import usable_webhook_url

    assert usable_webhook_url("https://discord.com/api/webhooks/1/abc")
    assert usable_webhook_url("http://127.0.0.1:8080/hook")
    assert not usable_webhook_url("")
    assert not usable_webhook_url("https//discord.com/x")  # the missing-colon typo
    assert not usable_webhook_url("file:///etc/passwd")
    assert not usable_webhook_url("https://")  # no netloc
