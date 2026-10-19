"""New-job alerts. Each channel announces accepted, keyword-matched jobs found since it last ran.

A new job is stored with an empty ``alert_seen_at``. The dashboard channel lists those
rows until they are marked seen. The Telegram channel sends its own pending queue, so
turning it on is only a settings change.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from latam_jobs.notify import deliver_notifications
from latam_jobs.settings import Settings
from latam_jobs.store import Store, StoredJob

logger = logging.getLogger(__name__)


class AlertChannel(Protocol):
    name: str

    def deliver(self, store: Store, settings: Settings, now: datetime) -> int: ...


@dataclass
class DashboardAlerts:
    count: int
    jobs: list[StoredJob]

    @property
    def newest_id(self) -> int:
        return max((job.id for job in self.jobs), default=0)


class DashboardChannel:
    """The dashboard reads unseen rows on each poll, so delivery has nothing to send."""

    name = "dashboard"

    def deliver(self, store: Store, settings: Settings, now: datetime) -> int:
        return 0

    def unseen(self, store: Store, limit: int = 10) -> DashboardAlerts:
        return DashboardAlerts(
            count=store.count_unseen_alerts(),
            jobs=store.list_unseen_alerts(limit),
        )

    def mark_seen(self, store: Store, now: datetime, *, up_to_id: int) -> int:
        return store.mark_alerts_seen(now, up_to_id=up_to_id)


class TelegramChannel:
    name = "telegram"

    def deliver(self, store: Store, settings: Settings, now: datetime) -> int:
        return deliver_notifications(store, settings, now=now)


CHANNELS: tuple[AlertChannel, ...] = (DashboardChannel(), TelegramChannel())


def deliver_alerts(store: Store, settings: Settings, now: datetime) -> None:
    for channel in CHANNELS:
        try:
            channel.deliver(store, settings, now)
        except Exception:
            logger.exception("%s alert delivery failed", channel.name)


def telegram_turned_on(store: Store, before: Settings, after: Settings) -> None:
    """When Telegram is switched on, skip the old backlog so the chat gets only new finds."""
    if after.notifications.enabled and not before.notifications.enabled:
        store.skip_pending_notifications()
