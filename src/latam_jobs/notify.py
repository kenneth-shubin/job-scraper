"""Send pending accepted jobs to Telegram in small plain-text batches."""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from datetime import datetime

import httpx

from latam_jobs.locations import location_label
from latam_jobs.scheduler import utc_now
from latam_jobs.settings import Settings
from latam_jobs.store import Store, StoredJob

logger = logging.getLogger(__name__)

BATCH_LIMIT = 3500
MAX_ATTEMPTS = 5


def deliver_notifications(
    store: Store,
    settings: Settings,
    *,
    now: datetime | None = None,
    token: str | None = None,
    chat_id: str | None = None,
    sender: Callable[[str], int] | None = None,
) -> int:
    """Mark a batch sent only after Telegram accepts it. Stop on the first failure."""
    if not settings.notifications.enabled:
        return 0
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip() if token is None else token.strip()
    destination = (
        os.environ.get("TELEGRAM_CHAT_ID", "").strip() if chat_id is None else chat_id.strip()
    )
    if not bot_token or not destination:
        logger.info("telegram credentials are missing; leaving jobs unsent")
        return 0
    moment = now or utc_now()
    post = sender or _sender(bot_token, destination, settings.http.timeout_seconds)
    sent = 0
    while True:
        pending = store.list_pending_notifications(limit=50)
        if not pending:
            return sent
        for batch in split_batches(pending):
            text = "\n\n".join(_item(job) for job in batch)
            try:
                status = post(text)
            except Exception:
                logger.warning("telegram delivery failed")
                _record_failure(store, batch)
                return sent
            if status != 200:
                logger.warning("telegram delivery returned HTTP %s", status)
                _record_failure(store, batch)
                return sent
            store.mark_notified([job.id for job in batch], moment)
            sent += len(batch)
        if len(pending) < 50:
            return sent


def split_batches(jobs: list[StoredJob]) -> list[list[StoredJob]]:
    batches: list[list[StoredJob]] = []
    current: list[StoredJob] = []
    size = 0
    for job in jobs:
        text = _item(job)
        extra = len(text) if not current else len(text) + 2
        if current and size + extra > BATCH_LIMIT:
            batches.append(current)
            current = [job]
            size = len(text)
            continue
        current.append(job)
        size += extra
    if current:
        batches.append(current)
    return batches


def _record_failure(store: Store, batch: list[StoredJob]) -> None:
    give_up = [job.id for job in batch if job.notification_attempts + 1 >= MAX_ATTEMPTS]
    retry = [job.id for job in batch if job.id not in set(give_up)]
    store.mark_notification_failed(retry, give_up=False)
    store.mark_notification_failed(give_up, give_up=True)
    if give_up:
        logger.warning("telegram gave up on %s jobs after %s attempts", len(give_up), MAX_ATTEMPTS)


def _item(job: StoredJob) -> str:
    location = location_label(job.location_text, job.location_countries)
    if location == "—":
        location = "Location not provided"
    return f"{job.title}\n{job.company}\n{location}\n{job.source}\n{job.url}"


def _sender(token: str, chat_id: str, timeout: float) -> Callable[[str], int]:
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    def post(text: str) -> int:
        with httpx.Client(timeout=timeout) as client:
            response = client.post(url, json={"chat_id": chat_id, "text": text})
        return response.status_code

    return post
