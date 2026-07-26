"""Notification delivery, abstracted so the transport can change later.

Today this ships a Discord **webhook** notifier built on stdlib ``urllib`` (zero
runtime dependencies) that renders rich, branded embeds. The ``Notifier``
protocol and the structured ``Notification`` dataclass exist so a future Discord
**bot** (or any other sink) can be dropped in without touching the event logic
in ``events.py`` — implement ``Notifier.send`` and wire it up in ``cli.py``.

Embed construction follows the Discord message/webhook spec: per-message
``username``/``avatar_url`` override, an ``author`` line, severity ``color``,
inline ``fields`` (Discord lays out up to 3 per row), a ``footer`` + native
ISO-8601 ``timestamp``, and the documented length limits (title 256,
description 4096, field value 1024, ≤25 fields, ≤6000 combined chars).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Protocol

from ups_orchestrator.jsonlog import append_jsonl, base_record

LOG = logging.getLogger("ups_orchestrator.notify")

# Discord embed limits (https://docs.discord.com/developers/resources/message)
_MAX_TITLE = 256
_MAX_DESC = 4096
_MAX_FIELD_NAME = 256
_MAX_FIELD_VALUE = 1024
_MAX_FOOTER = 2048
_MAX_FIELDS = 25


class Level(Enum):
    """Severity of a notification, mapped to a Discord embed colour."""

    INFO = 0x5865F2  # blurple
    SUCCESS = 0x57F287  # green
    WARNING = 0xFEE75C  # yellow
    CRITICAL = 0xED4245  # red


@dataclass
class Notification:
    """Transport-agnostic message. Notifiers render this however they like."""

    title: str
    body: str = ""
    level: Level = Level.INFO
    fields: list[tuple[str, str]] = field(default_factory=list)
    footer: str | None = None


@dataclass(frozen=True)
class DeliveryResult:
    """Observable result for one notification delivery attempt."""

    configured: bool
    ok: bool
    attempts: int = 0
    status_code: int | None = None
    error: str = ""


class Notifier(Protocol):
    """Anything that can deliver a :class:`Notification`."""

    def send(self, note: Notification) -> DeliveryResult: ...


class NullNotifier:
    """Used when no webhook is configured — logs and drops the message."""

    def send(self, note: Notification) -> DeliveryResult:
        LOG.info("[no notifier configured] %s — %s", note.title, note.body)
        return DeliveryResult(configured=False, ok=False, error="no notifier configured")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


# F1. The webhook URL's last path segment IS the credential — a Discord webhook URL
# is bearer-equivalent, and anyone who reads one can post to that channel forever.
# It must never reach a log line, an exception message, a traceback, or
# `DeliveryResult.error` (which `AuditedNotifier` persists to notifications.jsonl).
_REDACTED = "<webhook url redacted>"

# Only these two schemes are ever handed to `urlopen`. Everything else is refused
# at CONSTRUCTION, which is what makes the failure a quiet `configured=False`
# instead of an exception on the daemon's startup path:
#
#  * a scheme-less or malformed URL (`https//…` — a missing colon, i.e. the typo)
#    makes `urllib.request.Request` raise **ValueError**, which the send loop did
#    not catch, and whose message embeds the whole URL including the token;
#  * `file://` does not raise at all — `urlopen` succeeds, `resp.getcode()` returns
#    None, and `200 <= None` raises **TypeError**.
#
# Neither is an `OSError`, so neither was caught, and `_notify_degraded` is the one
# `send` on the daemon path outside every guard — and it fires ONLY when the config
# is degraded. So a degraded config killed `watch` before it reached the poll loop,
# `Restart=always` respawned it, and the box monitored nothing in a permanent
# restart loop. RA-01 replaced hard-fail with degrade-and-disarm precisely so a bad
# config could not stop monitoring; this reintroduced it through the notifier.
_ALLOWED_SCHEMES = frozenset({"http", "https"})


def usable_webhook_url(url: str) -> bool:
    """True iff ``url`` is something ``urlopen`` can POST to. PURE, never raises."""
    if not url:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme.lower() in _ALLOWED_SCHEMES and bool(parts.netloc)


class DiscordWebhookNotifier:
    """Delivers notifications as rich Discord embeds via an incoming webhook."""

    def __init__(
        self,
        webhook_url: str,
        *,
        username: str = "UPS Orchestrator",
        avatar_url: str = "",
        host: str = "",
        timeout: float = 5.0,
        max_attempts: int = 3,
    ) -> None:
        self.webhook_url = webhook_url
        self.username = username
        self.avatar_url = avatar_url
        self.host = host
        self.timeout = timeout
        self.max_attempts = max(1, max_attempts)

    def _redact(self, text: str) -> str:
        """Strip the webhook URL and its token out of any text about to escape.

        The URL's last path segment is the credential, so it is removed on its own
        as well: an error string may carry the token without the full URL around it.
        """
        if not text:
            return text
        out = text.replace(self.webhook_url, _REDACTED)
        token = self.webhook_url.rsplit("/", 1)[-1]
        if len(token) >= 8:
            out = out.replace(token, _REDACTED)
        return out

    def _embed(self, note: Notification) -> dict[str, object]:
        embed: dict[str, object] = {
            "type": "rich",
            "title": _clip(note.title, _MAX_TITLE),
            "color": note.level.value,
            "timestamp": datetime.now(UTC).isoformat(),
            "author": {"name": _clip(f"⚡ {self.username}", _MAX_FIELD_NAME)},
        }
        if note.body:
            embed["description"] = _clip(note.body, _MAX_DESC)
        if note.fields:
            embed["fields"] = [
                {
                    "name": "Severity",
                    "value": note.level.name,
                    "inline": True,
                },
                {
                    "name": "Host",
                    "value": self.host or "unknown",
                    "inline": True,
                },
                {
                    "name": "Delivery",
                    "value": "Discord webhook",
                    "inline": True,
                },
                *[
                    {
                        "name": _clip(name, _MAX_FIELD_NAME),
                        "value": _clip(value, _MAX_FIELD_VALUE),
                        "inline": True,
                    }
                    for name, value in note.fields[: max(0, _MAX_FIELDS - 3)]
                ],
            ]
        else:
            embed["fields"] = [
                {
                    "name": "Severity",
                    "value": note.level.name,
                    "inline": True,
                },
                {
                    "name": "Host",
                    "value": self.host or "unknown",
                    "inline": True,
                },
            ]
        footer_text = note.footer or "UPS Orchestrator"
        if footer_text:
            embed["footer"] = {"text": _clip(footer_text, _MAX_FOOTER)}
        return embed

    def _payload(self, note: Notification) -> dict[str, object]:
        payload: dict[str, object] = {"embeds": [self._embed(note)]}
        if self.username:
            payload["username"] = self.username
        if self.avatar_url:
            payload["avatar_url"] = self.avatar_url
        return payload

    def send(self, note: Notification) -> DeliveryResult:
        """POST the notification. NEVER raises — a down webhook must not break NUT.

        Retries transient failures and honours Discord ``retry_after`` on 429.

        F1: this promise used to be aspirational. The loop caught ``HTTPError`` and
        ``(URLError, OSError)``, and neither covers ``urllib.request.Request``'s
        ``ValueError`` on a malformed URL nor the ``TypeError`` a ``file://`` URL
        produces from ``resp.getcode()`` returning None. Both escaped, and
        ``_notify_degraded`` — the one ``send`` on the daemon's startup path outside
        every guard, fired ONLY when the config is degraded — turned a config typo
        into a permanent `watch` restart loop monitoring nothing.

        Two independent defences, because this must not depend on enumerating the
        exception classes correctly: the scheme is validated once at construction
        (``build_notifier`` falls back to ``NullNotifier``), and the loop body has a
        catch-all below. Every error string is redacted before it leaves this
        method — ``DeliveryResult.error`` is persisted to notifications.jsonl by
        ``AuditedNotifier``, so an unredacted one puts the token on disk as well as
        in the journal.
        """
        data = json.dumps(self._payload(note)).encode("utf-8")
        last_error = ""
        for attempt in range(1, self.max_attempts + 1):
            try:
                req = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "ups-orchestrator (+https://github.com/kleinpanic/ups-orchestrator)",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310
                    status = resp.getcode()
                if status is None:
                    # A non-HTTP handler answered (file://, data://). There is no
                    # status to compare, and `200 <= None` is the TypeError.
                    LOG.warning(
                        "Discord webhook: the configured URL is not an HTTP endpoint "
                        "(no status code returned); treating as undelivered"
                    )
                    return DeliveryResult(
                        configured=True,
                        ok=False,
                        attempts=attempt,
                        error="webhook URL is not an HTTP endpoint",
                    )
                LOG.info("Discord webhook delivered: status=%s attempts=%d", status, attempt)
                return DeliveryResult(
                    configured=True,
                    ok=200 <= status < 300,
                    attempts=attempt,
                    status_code=status,
                )
            except urllib.error.HTTPError as exc:
                last_error = self._redact(f"HTTP {exc.code}: {exc.reason}")
                if exc.code == 429 and attempt < self.max_attempts:
                    time.sleep(_retry_after(exc))
                    continue
                if 500 <= exc.code <= 599 and attempt < self.max_attempts:
                    time.sleep(_backoff(attempt))
                    continue
                LOG.warning("Discord webhook HTTP %s: %s", exc.code, self._redact(str(exc.reason)))
                return DeliveryResult(
                    configured=True,
                    ok=False,
                    attempts=attempt,
                    status_code=exc.code,
                    error=last_error,
                )
            except (urllib.error.URLError, OSError) as exc:
                last_error = self._redact(str(exc))
                if attempt < self.max_attempts:
                    time.sleep(_backoff(attempt))
                    continue
                LOG.warning(
                    "Discord webhook delivery failed after %d attempts: %s", attempt, last_error
                )
                return DeliveryResult(
                    configured=True,
                    ok=False,
                    attempts=attempt,
                    error=last_error,
                )
            except Exception as exc:  # noqa: BLE001 — the promise in the docstring
                # Deliberately unconditional and deliberately NOT retried. This is
                # the backstop that makes "never raises" true whatever urllib grows
                # next; a bad URL will not become good on attempt 2, and the daemon
                # must reach its poll loop. The class name is logged rather than the
                # message, because a urllib ValueError's message embeds the URL.
                LOG.warning(
                    "Discord webhook delivery raised %s; notification dropped "
                    "(check the webhook URL — it is not echoed here on purpose)",
                    type(exc).__name__,
                )
                return DeliveryResult(
                    configured=True,
                    ok=False,
                    attempts=attempt,
                    error=f"delivery raised {type(exc).__name__}",
                )
        return DeliveryResult(
            configured=True,
            ok=False,
            attempts=self.max_attempts,
            error=last_error or "delivery failed",
        )


class AuditedNotifier:
    """Wrap a notifier and persist delivery outcomes to JSONL."""

    def __init__(self, inner: Notifier, path: Path) -> None:
        self.inner = inner
        self.path = path

    def send(self, note: Notification) -> DeliveryResult:
        started = time.monotonic()
        result = self.inner.send(note)
        duration_ms = round((time.monotonic() - started) * 1000)
        record = base_record("notification")
        record.update(
            {
                "title": note.title,
                "level": note.level.name,
                "field_names": [name for name, _value in note.fields],
                "configured": result.configured,
                "ok": result.ok,
                "attempts": result.attempts,
                "status_code": result.status_code,
                "error": result.error,
                "duration_ms": duration_ms,
            }
        )
        try:
            append_jsonl(self.path, record)
        except OSError as exc:
            LOG.warning("Failed to write notification log %s: %s", self.path, exc)
        return result


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Best-effort parse of Discord's 429 ``retry_after`` (seconds); capped."""
    header = exc.headers.get("Retry-After")
    if header:
        try:
            return min(float(header), 10.0)
        except ValueError:
            pass
    try:
        body = json.loads(exc.read().decode("utf-8"))
        retry_after = body.get("retry_after", 1.0) if isinstance(body, dict) else 1.0
        return min(float(retry_after), 10.0)
    except (ValueError, OSError):
        return 1.0


def _backoff(attempt: int) -> float:
    """Short bounded retry delay; NUT callbacks must not hang for long."""
    delay: float = min(0.5 * (2 ** max(0, attempt - 1)), 2.0)
    return delay


def build_notifier(
    webhook_url: str,
    *,
    username: str = "UPS Orchestrator",
    avatar_url: str = "",
    host: str = "",
    delivery_log_path: Path | None = None,
) -> Notifier:
    """Return a webhook notifier if a USABLE URL is set, else a no-op notifier.

    F1: "set" is not enough. `https//…` (a missing colon) and `file:///…` are both
    non-empty, and both used to reach `send` and escape it as an uncaught
    ValueError/TypeError — on the daemon's startup path, where the only caller
    fires when the config is ALREADY degraded. Validated once here so the failure
    mode is a quiet `configured=False` and one log line, not a restart loop.

    The URL is never echoed: it is bearer-equivalent, so the log line that reports
    it as unusable is the last place it should appear.
    """
    if not webhook_url:
        notifier: Notifier = NullNotifier()
    elif not usable_webhook_url(webhook_url):
        LOG.error(
            "webhook_url is set but is not a usable http(s) URL (scheme and host are "
            "both required) — notifications are DISABLED for this run. The value is "
            "not logged: a Discord webhook URL is a credential. Fix 'webhook_url' in "
            "the config; monitoring is unaffected."
        )
        notifier = NullNotifier()
    else:
        notifier = DiscordWebhookNotifier(
            webhook_url, username=username, avatar_url=avatar_url, host=host
        )
    if delivery_log_path is not None:
        return AuditedNotifier(notifier, delivery_log_path)
    return notifier
