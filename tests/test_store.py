from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import httpx

from latam_jobs.http import HttpClient, HttpError
from latam_jobs.models import Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.pipeline import collect
from latam_jobs.scheduler import RunLock
from latam_jobs.settings import settings_from_mapping
from latam_jobs.sources.base import FetchContext, FetchResult
from latam_jobs.store import Store

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)


def _job(
    source: str = "remotive",
    source_job_id: str = "1",
    url: str = "https://example.com/jobs/1?utm_source=newsletter",
    title: str = "Python Developer",
) -> Job:
    return Job(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Example",
        url=url,
        description="Remote python role.",
        location_text="Worldwide",
        remote_status=RemoteStatus.REMOTE,
        latam_eligibility=LatamEligibility.ACCEPTED,
        eligibility_reason="accepted:worldwide:colombia: worldwide includes the residence country",
        keyword_status=KeywordStatus.MATCHED,
        keyword_reason="matched 'python' in title",
    )


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "jobs.sqlite")
    store.migrate()
    return store


def test_upsert_is_idempotent_and_cross_source_url_is_skipped(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs([_job()], NOW)
    store.upsert_jobs([_job(title="Python Developer II")], LATER)
    stored = store.get_job("remotive", "1")
    assert stored is not None
    assert store.count_jobs() == 1
    assert stored.discovered_at == NOW.isoformat(timespec="seconds")
    assert stored.last_seen_at == LATER.isoformat(timespec="seconds")
    assert stored.title == "Python Developer II"
    assert stored.notification_status == "pending"
    assert stored.canonical_url == "https://example.com/jobs/1"

    store.upsert_jobs(
        [_job(source="himalayas", source_job_id="9", url="https://example.com/jobs/1?ref=abc")],
        LATER,
    )
    duplicate = store.get_job("himalayas", "9")
    assert duplicate is not None
    assert duplicate.notification_status == "skipped"
    assert store.get_job("remotive", "1").notification_status == "pending"  # type: ignore[union-attr]
    store.close()


def test_sent_notification_is_not_reset(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs([_job()], NOW)
    stored = store.get_job("remotive", "1")
    assert stored is not None
    store.mark_notified([stored.id], NOW)
    rejected = _job()
    rejected.latam_eligibility = LatamEligibility.REJECTED
    rejected.eligibility_reason = "rejected:country_allow_list:colombia: restricted to brazil"
    store.upsert_jobs([rejected], LATER)
    again = store.get_job("remotive", "1")
    assert again is not None
    assert again.notification_status == "sent"
    assert again.latam_eligibility == "rejected"
    store.close()


def test_becoming_accepted_returns_to_pending(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rejected = _job()
    rejected.latam_eligibility = LatamEligibility.REJECTED
    rejected.eligibility_reason = (
        "rejected:region:colombia: Central America does not include the residence country"
    )
    store.upsert_jobs([rejected], NOW)
    assert store.get_job("remotive", "1").notification_status == "skipped"  # type: ignore[union-attr]
    store.upsert_jobs([_job()], LATER)
    assert store.get_job("remotive", "1").notification_status == "pending"  # type: ignore[union-attr]
    store.close()


def test_upsert_rolls_back(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = store._upsert_one

    def explode(job: Job, now: datetime) -> None:
        if job.source_job_id == "2":
            raise RuntimeError("fail")
        original(job, now)

    store._upsert_one = explode  # type: ignore[method-assign]
    try:
        store.upsert_jobs(
            [_job(source_job_id="1"), _job(source_job_id="2", url="https://example.com/jobs/2")],
            NOW,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("expected rollback")
    assert store.count_jobs() == 0
    store.close()


def test_settings_seed_once(tmp_path: Path) -> None:
    store = _store(tmp_path)
    original = settings_from_mapping(
        {"residence_country": "colombia", "database": {"path": str(tmp_path / "jobs.sqlite")}}
    )
    changed = settings_from_mapping(
        {"residence_country": "brazil", "database": {"path": str(tmp_path / "other.sqlite")}}
    )
    assert store.load_or_seed(original).residence_country == "colombia"
    loaded = store.load_or_seed(changed)
    assert loaded.residence_country == "colombia"
    assert loaded.database.path == str(tmp_path / "other.sqlite")
    store.close()


def test_failed_source_does_not_change_another_success(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    calls = {"ok": 0, "bad": 0}

    class Ok:
        name = "ok"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            calls["ok"] += 1
            return FetchResult(jobs=[_job()], status_code=200)

    class Bad:
        name = "bad"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            calls["bad"] += 1
            raise HttpError("HTTP 403 for https://example.com/jobs", status_code=403)

    settings = settings_from_mapping(
        {
            "keywords": ["python"],
            "database": {"path": str(database)},
            "scheduler": {"jitter_seconds": 0},
            "sources": {
                "ok": {"enabled": True, "interval_hours": 12},
                "bad": {"enabled": True, "interval_hours": 12},
            },
        }
    )
    runs = collect(
        settings,
        registry={"ok": Ok(), "bad": Bad()},
        client=_client(),
        now=NOW,
        rng=Random(0),
    )
    assert {run.name for run in runs} == {"ok", "bad"}
    store = Store(database)
    ok_state = store.get_schedule("ok")
    bad_state = store.get_schedule("bad")
    assert ok_state is not None and ok_state.last_success_at == NOW
    assert ok_state.consecutive_failures == 0
    assert bad_state is not None and bad_state.last_success_at is None
    assert bad_state.consecutive_failures == 1
    assert bad_state.backoff_until == NOW + timedelta(hours=24)
    assert store.count_jobs() == 1
    store.close()

    collect(
        settings,
        registry={"ok": Ok(), "bad": Bad()},
        client=_client(),
        now=NOW,
        rng=Random(0),
    )
    assert calls == {"ok": 1, "bad": 1}


def test_second_run_updates_last_seen(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"

    class Ok:
        name = "ok"
        default_interval_hours = 1
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(jobs=[_job()], status_code=200)

    settings = settings_from_mapping(
        {
            "keywords": ["python"],
            "database": {"path": str(database)},
            "scheduler": {"jitter_seconds": 0},
            "sources": {"ok": {"enabled": True, "interval_hours": 1}},
        }
    )
    collect(settings, registry={"ok": Ok()}, client=_client(), now=NOW, rng=Random(0))
    collect(
        settings,
        only_source="ok",
        registry={"ok": Ok()},
        client=_client(),
        now=LATER,
        rng=Random(0),
    )
    store = Store(database)
    stored = store.get_job("remotive", "1")
    assert stored is not None
    assert stored.discovered_at == NOW.isoformat(timespec="seconds")
    assert stored.last_seen_at == LATER.isoformat(timespec="seconds")
    store.close()


def test_lock_prevents_overlap(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    first = RunLock(path)
    second = RunLock(path)
    first.acquire()
    try:
        try:
            second.acquire()
        except Exception as exc:
            assert "Another run" in str(exc)
        else:
            raise AssertionError("expected the second lock to fail")
    finally:
        first.release()


def _client() -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return HttpClient(timeout=5, max_retries=0, transport=httpx.MockTransport(handler))
