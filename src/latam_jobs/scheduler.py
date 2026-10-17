"""Per-source due times, failure backoff, and the process lock."""

from __future__ import annotations

import fcntl
import signal
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random
from typing import IO, Any

_WEEK = timedelta(days=7)


class LockError(Exception):
    """Another collector run already holds the lock."""


@dataclass
class SourceSchedule:
    source_name: str
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    next_fetch_at: datetime | None = None
    etag: str | None = None
    last_modified: str | None = None
    consecutive_failures: int = 0
    backoff_until: datetime | None = None
    last_http_status: int | None = None
    last_error: str | None = None


def earliest_next_fetch(schedules: list[SourceSchedule], enabled: set[str]) -> datetime | None:
    moments = [
        item.next_fetch_at
        for item in schedules
        if item.source_name in enabled and item.next_fetch_at is not None
    ]
    return min(moments) if moments else None


def is_due(schedule: SourceSchedule | None, now: datetime) -> bool:
    if schedule is None:
        return True
    if schedule.backoff_until is not None and now < schedule.backoff_until:
        return False
    if schedule.next_fetch_at is not None and now < schedule.next_fetch_at:
        return False
    return True


def next_success_at(
    now: datetime,
    interval_hours: int,
    jitter_seconds: int,
    rng: Random,
) -> datetime:
    jitter = rng.randint(0, jitter_seconds) if jitter_seconds else 0
    return now + timedelta(hours=interval_hours, seconds=jitter)


def failure_backoff(now: datetime, consecutive_failures: int, status_code: int | None) -> datetime:
    base = timedelta(hours=24) if status_code in {403, 429} else timedelta(hours=1)
    multiplier = 2 ** max(0, consecutive_failures - 1)
    delay = base * multiplier
    if delay > _WEEK:
        delay = _WEEK
    return now + delay


class RunLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[str] | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LockError(f"Another run holds {self.path}") from exc
        self._handle = handle

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
        self._handle = None


class StopFlag:
    """Remember SIGTERM and let the current source finish."""

    def __init__(self) -> None:
        self.stopped = False
        self._previous: Any = None

    def install(self) -> None:
        self._previous = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, self._handle)

    def close(self) -> None:
        if self._previous is not None:
            signal.signal(signal.SIGTERM, self._previous)

    def _handle(self, signum: int, frame: object) -> None:
        self.stopped = True


def utc_now() -> datetime:
    return datetime.now(UTC)


def to_iso(moment: datetime | None) -> str | None:
    if moment is None:
        return None
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
