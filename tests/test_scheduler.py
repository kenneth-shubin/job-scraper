from datetime import UTC, datetime, timedelta
from random import Random

from latam_jobs.scheduler import (
    SourceSchedule,
    failure_backoff,
    is_due,
    next_success_at,
)

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)


def test_never_run_source_is_due() -> None:
    assert is_due(None, NOW) is True


def test_future_next_fetch_and_backoff_are_not_due() -> None:
    waiting = SourceSchedule(source_name="remotive", next_fetch_at=NOW + timedelta(hours=1))
    backed_off = SourceSchedule(source_name="remotive", backoff_until=NOW + timedelta(hours=2))
    ready = SourceSchedule(
        source_name="remotive",
        next_fetch_at=NOW - timedelta(minutes=1),
        backoff_until=NOW - timedelta(minutes=1),
    )
    assert is_due(waiting, NOW) is False
    assert is_due(backed_off, NOW) is False
    assert is_due(ready, NOW) is True


def test_success_interval_uses_utc_and_jitter() -> None:
    scheduled = next_success_at(NOW, interval_hours=12, jitter_seconds=120, rng=Random(1))
    assert scheduled.tzinfo is UTC
    assert NOW + timedelta(hours=12) <= scheduled <= NOW + timedelta(hours=12, seconds=120)


def test_backoff_doubles_and_caps_at_one_week() -> None:
    first_forbidden = failure_backoff(NOW, consecutive_failures=1, status_code=403)
    second_forbidden = failure_backoff(NOW, consecutive_failures=2, status_code=429)
    capped = failure_backoff(NOW, consecutive_failures=10, status_code=403)
    other = failure_backoff(NOW, consecutive_failures=1, status_code=500)
    assert first_forbidden - NOW == timedelta(hours=24)
    assert second_forbidden - NOW == timedelta(hours=48)
    assert capped - NOW == timedelta(days=7)
    assert other - NOW == timedelta(hours=1)
