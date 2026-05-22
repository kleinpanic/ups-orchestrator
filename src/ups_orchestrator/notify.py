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
from typing import Protocol

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


class Notifier(Protocol):
    """Anything that can deliver a :class:`Notification`."""

    def send(self, note: Notification) -> None: ...


class NullNotifier:
    """Used when no webhook is configured — logs and drops the message."""

    def send(self, note: Notification) -> None:
        LOG.info("[no notifier configured] %s — %s", note.title, note.body)


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
        timeout: float = 8.0,
    ) -> None:
        self.webhook_url = webhook_url
        self.username = username
        self.avatar_url = avatar_url
        self.host = host
        self.timeout = timeout

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
                    "name": _clip(name, _MAX_FIELD_NAME),
                    "value": _clip(value, _MAX_FIELD_VALUE),
                    "inline": True,
                }
                for name, value in note.fields[:_MAX_FIELDS]
            ]
        footer_text = note.footer or self.host
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

    def send(self, note: Notification) -> None:
        """POST the notification. Never raises — a down webhook must not break NUT.

        Retries once on HTTP 429 honouring ``retry_after``.
        """
        data = json.dumps(self._payload(note)).encode("utf-8")
        for attempt in range(2):
            try:
                req = urllib.request.Request(
                    self.webhook_url,
                    data=data,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "ups-orchestrator (+https://github.com/kleinpanic93/ups-orchestrator)",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=self.timeout)  # noqa: S310 (trusted webhook)
                return
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and attempt == 0:
                    time.sleep(_retry_after(exc))
                    continue
                LOG.warning("Discord webhook HTTP %s: %s", exc.code, exc.reason)
                return
            except (urllib.error.URLError, OSError) as exc:
                LOG.warning("Discord webhook delivery failed: %s", exc)
                return


def _retry_after(exc: urllib.error.HTTPError) -> float:
    """Best-effort parse of Discord's 429 ``retry_after`` (seconds); capped."""
    try:
        body = json.loads(exc.read().decode("utf-8"))
        return min(float(body.get("retry_after", 1.0)), 5.0)
    except (ValueError, OSError):
        return 1.0


def build_notifier(
    webhook_url: str, *, username: str = "UPS Orchestrator", avatar_url: str = "", host: str = ""
) -> Notifier:
    """Return a webhook notifier if a URL is set, else a no-op notifier."""
    if not webhook_url:
        return NullNotifier()
    return DiscordWebhookNotifier(webhook_url, username=username, avatar_url=avatar_url, host=host)
