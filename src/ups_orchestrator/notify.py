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
        """POST the notification. Never raises — a down webhook must not break NUT.

        Retries transient failures and honours Discord ``retry_after`` on 429.
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
                LOG.info("Discord webhook delivered: status=%s attempts=%d", status, attempt)
                return DeliveryResult(
                    configured=True,
                    ok=200 <= status < 300,
                    attempts=attempt,
                    status_code=status,
                )
            except urllib.error.HTTPError as exc:
                last_error = f"HTTP {exc.code}: {exc.reason}"
                if exc.code == 429 and attempt < self.max_attempts:
                    time.sleep(_retry_after(exc))
                    continue
                if 500 <= exc.code <= 599 and attempt < self.max_attempts:
                    time.sleep(_backoff(attempt))
                    continue
                LOG.warning("Discord webhook HTTP %s: %s", exc.code, exc.reason)
                return DeliveryResult(
                    configured=True,
                    ok=False,
                    attempts=attempt,
                    status_code=exc.code,
                    error=last_error,
                )
            except (urllib.error.URLError, OSError) as exc:
                last_error = str(exc)
                if attempt < self.max_attempts:
                    time.sleep(_backoff(attempt))
                    continue
                LOG.warning("Discord webhook delivery failed after %d attempts: %s", attempt, exc)
                return DeliveryResult(
                    configured=True,
                    ok=False,
                    attempts=attempt,
                    error=last_error,
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
    """Return a webhook notifier if a URL is set, else a no-op notifier."""
    if not webhook_url:
        notifier: Notifier = NullNotifier()
    else:
        notifier = DiscordWebhookNotifier(
            webhook_url, username=username, avatar_url=avatar_url, host=host
        )
    if delivery_log_path is not None:
        return AuditedNotifier(notifier, delivery_log_path)
    return notifier
