import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from random import Random

import httpx

from latam_jobs.http import HttpClient, HttpError
from latam_jobs.models import ApplyUrlType, Job, KeywordStatus, LatamEligibility, RemoteStatus
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
    published_at: datetime | None = NOW,
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
        published_at=published_at,
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


def test_sources_fetch_at_the_same_time(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    both_started = threading.Barrier(2, timeout=5)

    class Waits:
        default_interval_hours = 12
        default_enabled = True

        def __init__(self, name: str) -> None:
            self.name = name

        def fetch(self, ctx: FetchContext) -> FetchResult:
            both_started.wait()
            return FetchResult(jobs=[_job(source=self.name)], status_code=200)

    settings = settings_from_mapping(
        {
            "keywords": ["python"],
            "database": {"path": str(database)},
            "scheduler": {"jitter_seconds": 0},
            "sources": {
                "first": {"enabled": True, "interval_hours": 12},
                "second": {"enabled": True, "interval_hours": 12},
            },
        }
    )
    runs = collect(
        settings,
        registry={"first": Waits("first"), "second": Waits("second")},
        client=_client(),
        now=NOW,
        rng=Random(0),
    )
    assert [run.name for run in runs] == ["first", "second"]
    assert all(run.error is None for run in runs)
    store = Store(database)
    assert store.count_jobs() == 2
    store.close()


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


def test_recent_publication_window(tmp_path: Path) -> None:
    store = _store(tmp_path)
    inserted = store.upsert_jobs(
        [
            _job(
                source_job_id="2h",
                url="https://example.com/2",
                title="Two hours",
                published_at=NOW - timedelta(hours=2),
            ),
            _job(
                source_job_id="23h",
                url="https://example.com/23",
                title="Twenty three",
                published_at=NOW - timedelta(hours=23),
            ),
            _job(
                source_job_id="edge",
                url="https://example.com/edge",
                title="Exactly one day",
                published_at=NOW - timedelta(hours=24),
            ),
        ],
        NOW,
    )
    assert inserted == 3
    rejected = _job(
        source_job_id="rejected",
        url="https://example.com/rejected",
        title="Rejected role",
    )
    rejected.latam_eligibility = LatamEligibility.REJECTED
    assert store.upsert_jobs([rejected], NOW) == 0
    assert store.get_job("remotive", "rejected") is not None
    skipped = store.upsert_jobs(
        [
            _job(
                source_job_id="25h",
                url="https://example.com/25",
                published_at=NOW - timedelta(hours=25),
            ),
            _job(
                source_job_id="5d",
                url="https://example.com/5d",
                published_at=NOW - timedelta(days=5),
            ),
            _job(source_job_id="missing", url="https://example.com/missing", published_at=None),
        ],
        NOW,
    )
    assert skipped == 1
    assert store.get_job("remotive", "25h") is not None
    assert store.get_job("remotive", "5d") is None
    then = NOW - timedelta(days=10)
    assert (
        store.upsert_jobs(
            [
                _job(
                    source_job_id="old",
                    url="https://example.com/old",
                    title="Stored earlier",
                    published_at=then,
                )
            ],
            then,
        )
        == 1
    )
    listed = store.list_jobs(eligibility="accepted")
    assert listed[0].source_job_id == "2h"
    assert any(job.title == "Stored earlier" for job in listed)
    recent_only = store.list_jobs(eligibility="accepted", since=NOW - timedelta(hours=24))
    assert [job.source_job_id for job in recent_only] == ["2h", "23h", "edge"]
    store.close()


def test_apply_url_round_trip_and_migration_default(tmp_path: Path) -> None:
    database = tmp_path / "legacy.sqlite"
    connection = sqlite3.connect(database)
    script = Path("src/latam_jobs/migrations/001_initial.sql").read_text(encoding="utf-8")
    connection.executescript(script)
    connection.execute(
        "INSERT INTO schema_migrations (version, applied_at) VALUES (1, ?)",
        (NOW.isoformat(timespec="seconds"),),
    )
    connection.execute(
        """
        INSERT INTO jobs (
            source, source_job_id, title, company, url, canonical_url, description,
            tags_json, location_text, location_countries_json, salary, published_at,
            remote_status, latam_eligibility, eligibility_reason, keyword_status,
            keyword_reason, notification_status, content_hash, discovered_at, last_seen_at
        ) VALUES (
            'remotive', 'legacy', 'Legacy role', 'Example', 'https://remotive.com/jobs/legacy',
            'https://remotive.com/jobs/legacy', 'backend', '[]', 'Worldwide', '[]', NULL, ?,
            'remote', 'accepted', 'reason', 'matched', 'matched', 'pending', 'hash', ?, ?
        )
        """,
        (NOW.isoformat(timespec="seconds"),) * 3,
    )
    connection.commit()
    connection.close()

    store = Store(database)
    store.migrate()
    legacy = store.get_job("remotive", "legacy")
    assert legacy is not None
    assert legacy.apply_url is None
    assert legacy.apply_url_type == "job_board"

    direct = _job(source_job_id="direct", url="https://remotive.com/jobs/direct")
    direct.apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    direct.apply_url_type = ApplyUrlType.DIRECT
    store.upsert_jobs([direct], NOW)
    stored = store.get_job("remotive", "direct")
    assert stored is not None
    assert stored.apply_url == "https://boards.greenhouse.io/acme/jobs/1"
    assert stored.apply_url_type == "direct"
    assert store.list_jobs(application="direct")[0].source_job_id == "direct"
    assert all(
        job.apply_url_type == "job_board" for job in store.list_jobs(application="job_board")
    )
    store.close()


def test_collector_run_records_partial_failure_and_new_jobs(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"

    class Ok:
        name = "ok"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(jobs=[_job()], status_code=200)

    class Bad:
        name = "bad"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            raise HttpError("HTTP 500", status_code=500)

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
    collect(
        settings,
        registry={"ok": Ok(), "bad": Bad()},
        client=_client(),
        now=NOW,
        rng=Random(0),
    )
    store = Store(database)
    partial = store.latest_run()
    assert partial is not None
    assert partial.outcome == "partial"
    assert partial.new_jobs == 1
    assert partial.sources_ok == 1
    assert partial.sources_failed == 1
    assert partial.started_at == NOW

    collect(
        settings,
        only_source="bad",
        registry={"ok": Ok(), "bad": Bad()},
        client=_client(),
        now=LATER,
        rng=Random(0),
    )
    failed = store.latest_run()
    assert failed is not None
    assert failed.outcome == "failed"
    assert failed.new_jobs == 0
    assert store.latest_successful_run() is None

    collect(
        settings,
        only_source="ok",
        registry={"ok": Ok(), "bad": Bad()},
        client=_client(),
        now=LATER,
        rng=Random(0),
    )
    success = store.latest_successful_run()
    assert success is not None
    assert success.outcome == "success"
    assert success.new_jobs == 0

    class LaterFailure:
        name = "ok"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            raise HttpError("HTTP 503", status_code=503)

    after = LATER + timedelta(hours=1)
    collect(
        settings,
        only_source="ok",
        registry={"ok": LaterFailure(), "bad": Bad()},
        client=_client(),
        now=after,
        rng=Random(0),
    )
    kept = store.get_schedule("ok")
    assert kept is not None
    assert kept.last_success_at == LATER
    assert kept.consecutive_failures == 1
    store.close()


def _client() -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={})

    return HttpClient(timeout=5, max_retries=0, transport=httpx.MockTransport(handler))


def test_safe_schedule_migration_drops_wwr_and_moves_intervals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    old = settings_from_mapping(
        {
            "scheduler": {"check_interval_minutes": 60},
            "ats_catalog": {"scan_every_minutes": 60, "rest_minutes": 10},
            "sources": {
                "wwr": {"enabled": True, "interval_hours": 12},
                "remotive": {"enabled": True, "interval_hours": 12},
                "greenhouse": {"enabled": True, "interval_hours": 24},
            },
        }
    )
    store.save_settings(old, bootstrap=old)
    store.upsert_jobs([_job(source="wwr", url="https://weworkremotely.com/1")], NOW)
    store.record_success(
        "wwr", now=NOW, next_fetch_at=LATER, etag=None, last_modified=None, status_code=200
    )
    store.record_success(
        "greenhouse",
        now=NOW,
        next_fetch_at=NOW + timedelta(hours=24),
        etag=None,
        last_modified=None,
        status_code=200,
    )
    script = Path("src/latam_jobs/migrations/007_safe_schedule.sql").read_text(encoding="utf-8")
    store._apply_migration(1007, script)
    loaded = store.load_or_seed(old)
    assert "wwr" not in loaded.sources
    assert loaded.sources["remotive"].interval_hours == 6
    assert loaded.sources["greenhouse"].interval_hours == 2
    assert "himalayas" not in loaded.sources
    assert store.get_job("wwr", "1") is None
    assert store.get_schedule("wwr") is None
    schedule = store.get_schedule("greenhouse")
    assert schedule is not None
    assert schedule.next_fetch_at == NOW + timedelta(hours=2)
    store.close()


def test_new_accepted_jobs_start_unseen_and_others_start_seen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    rejected = _job(source_job_id="2", url="https://example.com/jobs/2")
    rejected.latam_eligibility = LatamEligibility.REJECTED
    duplicate = _job(source="himalayas", source_job_id="3")
    store.upsert_jobs([_job(), rejected, duplicate], NOW)
    assert store.count_unseen_alerts() == 1
    assert [job.source_job_id for job in store.list_unseen_alerts()] == ["1"]
    assert store.get_job("himalayas", "3").alert_seen_at is not None  # type: ignore[union-attr]
    store.close()
