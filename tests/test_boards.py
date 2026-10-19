from datetime import UTC, datetime

import httpx
import pytest

from latam_jobs.ats_directory import Pacer
from latam_jobs.http import HttpClient, HttpError
from latam_jobs.models import ApplyUrlType, Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.pipeline import collect
from latam_jobs.settings import SourceSettings, settings_from_mapping
from latam_jobs.sources.arbeitnow import ArbeitnowSource
from latam_jobs.sources.ashby import ASHBY_TIMEOUT_SECONDS, AshbySource
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.sources.greenhouse import GreenhouseSource
from latam_jobs.sources.jobicy import JobicySource
from latam_jobs.sources.lever import LeverSource
from latam_jobs.store import Store


def _client(handler) -> HttpClient:
    return HttpClient(
        timeout=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def _context(client: HttpClient, **kwargs: object) -> FetchContext:
    values: dict[str, object] = {"enabled": True, "max_pages": 1}
    values.update(kwargs)
    settings = SourceSettings(**values)  # type: ignore[arg-type]
    return FetchContext(client=client, settings=settings)


def test_jobicy_maps_a_board_listing_and_follows_the_cursor() -> None:
    pages = {
        None: {
            "jobs": [
                {
                    "id": 1,
                    "url": "https://jobicy.com/jobs/backend",
                    "jobTitle": "Backend Engineer",
                    "companyName": "Acme",
                    "jobDescription": "<p>Python</p>",
                    "jobGeo": "Worldwide",
                    "pubDate": "2026-10-17T12:00:00+00:00",
                    "salaryMin": 100000,
                    "salaryMax": 120000,
                    "salaryCurrency": "USD",
                    "salaryPeriod": "yearly",
                    "applyUrl": "https://jobs.acme.test/backend",
                }
            ],
            "hasMore": True,
            "nextCursor": "page-2",
        },
        "page-2": {
            "jobs": [
                {"id": "bad"},
                {"id": 1, "url": "https://jobicy.com/jobs/backend", "jobTitle": "dup"},
            ],
            "hasMore": False,
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cursor = request.url.params.get("cursor")
        return httpx.Response(200, json=pages[cursor])

    result = JobicySource().fetch(_context(_client(handler), max_pages=2))
    assert result.skipped_items == 1
    assert len(result.jobs) == 1
    job = result.jobs[0]
    assert job.apply_url == "https://jobs.acme.test/backend"
    assert job.apply_url_type is ApplyUrlType.DIRECT
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.salary == "USD 100000-120000 yearly"
    assert job.description == "Python"


def test_jobicy_makes_one_latam_pass_whatever_the_keywords() -> None:
    seen: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(dict(request.url.params))
        cursor = request.url.params.get("cursor")
        return httpx.Response(
            200,
            json={
                "jobs": [{"id": cursor or "first", "url": "https://jobicy.com/j", "jobTitle": "A"}],
                "hasMore": True,
                "nextCursor": f"{cursor or ''}x",
            },
        )

    JobicySource().fetch(
        FetchContext(
            client=_client(handler),
            settings=SourceSettings(enabled=True, max_pages=20),
            search_terms=("python", "django", "react"),
        )
    )
    assert seen == [
        {"count": "50", "geo": "latam"},
        {"count": "50", "geo": "latam", "cursor": "x"},
        {"count": "50", "geo": "latam", "cursor": "xx"},
    ]


def test_jobicy_without_an_external_url_stays_a_job_board() -> None:
    payload = {
        "jobs": [
            {
                "id": 2,
                "url": "https://jobicy.com/jobs/frontend",
                "jobTitle": "Frontend Engineer",
                "companyName": "Acme",
            }
        ],
        "hasMore": False,
    }
    result = JobicySource().fetch(
        _context(_client(lambda _request: httpx.Response(200, json=payload)))
    )
    assert result.jobs[0].apply_url is None
    assert result.jobs[0].apply_url_type is ApplyUrlType.JOB_BOARD


def test_arbeitnow_reads_remote_and_the_next_page() -> None:
    first = {
        "data": [
            {
                "slug": "backend-berlin",
                "title": "Backend Engineer",
                "url": "https://www.arbeitnow.com/jobs/backend-berlin",
                "company_name": "Acme",
                "description": "<p>Python</p>",
                "tags": ["python"],
                "location": "Berlin",
                "remote": False,
                "created_at": 1_792_267_236,
            }
        ],
        "links": {"next": "https://www.arbeitnow.com/api/job-board-api?page=2"},
    }
    second = {
        "data": [
            {
                "slug": "backend-remote",
                "title": "Remote Backend",
                "url": "https://www.arbeitnow.com/jobs/backend-remote",
                "company_name": "Acme",
                "remote": True,
                "created_at": 1_792_267_236,
            }
        ],
        "links": {"next": None},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        body = second if request.url.params.get("page") == "2" else first
        return httpx.Response(200, json=body)

    result = ArbeitnowSource().fetch(_context(_client(handler), max_pages=2))
    assert [job.source_job_id for job in result.jobs] == ["backend-berlin", "backend-remote"]
    assert result.jobs[0].remote_status is RemoteStatus.ONSITE
    assert result.jobs[0].apply_url_type is ApplyUrlType.JOB_BOARD
    assert result.jobs[1].remote_status is RemoteStatus.REMOTE
    assert result.jobs[0].published_at is not None


def test_greenhouse_slows_down_and_stops_when_rate_limited() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        return httpx.Response(429, text="slow down")

    pauses: list[float] = []
    result = GreenhouseSource().fetch(
        FetchContext(
            client=_client(handler),
            settings=SourceSettings(enabled=True, feeds=["a", "b", "c", "d", "e"]),
            now=datetime(2026, 10, 17, 22, tzinfo=UTC),
            sleeper=pauses.append,
            pause_seconds=1.0,
        )
    )
    assert len(requested) == 3
    assert pauses == [2.0, 4.0]
    assert result.jobs == []


def test_pacer_slows_down_after_server_errors_and_resets_after_success() -> None:
    pauses: list[float] = []
    pacer = Pacer(pauses.append, 1.0)
    pacer.wait()
    pacer.record(HttpError("HTTP 503", status_code=503))
    pacer.wait()
    pacer.record(HttpError("HTTP 502", status_code=502))
    pacer.wait()
    assert pauses == [2.0, 4.0]
    assert not pacer.exhausted
    pacer.record(HttpError("HTTP 500", status_code=500))
    assert pacer.exhausted
    pacer.record(None)
    assert not pacer.exhausted
    pacer.record(HttpError("HTTP 404", status_code=404))
    pacer.wait()
    assert pauses[-1] == 8.0


def test_ashby_waits_thirty_seconds_for_a_slow_board() -> None:
    timeouts: list[object] = []

    def handler(request: httpx.Request) -> httpx.Response:
        timeouts.append(request.extensions["timeout"]["read"])
        return httpx.Response(200, json={"jobs": []})

    AshbySource().fetch(
        FetchContext(
            client=_client(handler),
            settings=SourceSettings(enabled=True, feeds=["linear"]),
            sleeper=lambda _seconds: None,
        )
    )
    assert timeouts == [ASHBY_TIMEOUT_SECONDS]


def test_board_pause_adds_random_jitter() -> None:
    pauses: list[float] = []
    pacer = Pacer(pauses.append, 1.0, 1.0)
    for _ in range(20):
        pacer.wait()
    assert len(pauses) == 19
    assert all(1.0 <= pause <= 2.0 for pause in pauses)
    assert len(set(pauses)) > 1


def test_greenhouse_keeps_a_good_board_when_another_is_missing() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if "missing" in str(request.url):
            return httpx.Response(404, text="missing")
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": 9,
                        "title": "Backend Engineer",
                        "absolute_url": "https://job-boards.greenhouse.io/gitlab/jobs/9",
                        "company_name": "GitLab",
                        "content": "<p>Python</p>",
                        "location": {"name": "Remote, Colombia"},
                        "first_published": "2026-05-08T11:54:53-04:00",
                        "updated_at": "2026-10-14T14:00:02-04:00",
                    },
                    {"id": None},
                ]
            },
        )

    pauses: list[float] = []
    result = GreenhouseSource().fetch(
        FetchContext(
            client=_client(handler),
            settings=SourceSettings(enabled=True, feeds=["gitlab", "missing"]),
            now=datetime(2026, 10, 17, 22, tzinfo=UTC),
            sleeper=pauses.append,
        )
    )
    assert pauses == [0.25, 0.25]
    assert result.skipped_items == 1
    job = result.jobs[0]
    assert job.source_job_id == "gitlab:9"
    assert job.apply_url_type is ApplyUrlType.CAREER
    assert job.apply_url is None
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.company == "GitLab"
    assert job.published_at == datetime(2026, 10, 14, 18, 0, 2, tzinfo=UTC)


def test_greenhouse_fails_when_every_board_fails() -> None:
    client = _client(lambda _request: httpx.Response(404, text="missing"))
    with pytest.raises(SourceError, match="Greenhouse"):
        GreenhouseSource().fetch(_context(client, feeds=["missing"]))


def test_lever_same_host_apply_url_is_a_career_page() -> None:
    payload = [
        {
            "id": "abc",
            "text": "Backend Engineer",
            "hostedUrl": "https://jobs.lever.co/spotify/abc",
            "applyUrl": "https://jobs.lever.co/spotify/abc/apply",
            "descriptionPlain": "Python",
            "workplaceType": "remote",
            "categories": {"location": "Remote"},
            "createdAt": 1_791_566_108_174,
        }
    ]
    result = LeverSource().fetch(
        FetchContext(
            client=_client(lambda _request: httpx.Response(200, json=payload)),
            settings=SourceSettings(enabled=True, feeds=["spotify"]),
            now=datetime.fromtimestamp(1_791_566_108.174, UTC),
        )
    )
    job = result.jobs[0]
    assert job.url == "https://jobs.lever.co/spotify/abc"
    assert job.apply_url is None
    assert job.apply_url_type is ApplyUrlType.CAREER
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.published_at is not None


def test_ashby_uses_a_different_host_apply_url() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/missing"):
            return httpx.Response(404, text="missing")
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "id": "job-1",
                        "title": "Backend Engineer",
                        "jobUrl": "https://jobs.ashbyhq.com/linear/job-1",
                        "applyUrl": "https://linear.app/careers/job-1",
                        "descriptionPlain": "Python",
                        "isRemote": True,
                        "location": "Remote",
                        "publishedAt": "2026-10-17T16:00:00.000+00:00",
                    }
                ]
            },
        )

    result = AshbySource().fetch(
        FetchContext(
            client=_client(handler),
            settings=SourceSettings(enabled=True, feeds=["linear", "missing"]),
            now=datetime(2026, 10, 17, 18, tzinfo=UTC),
            sleeper=lambda _seconds: None,
        )
    )
    job = result.jobs[0]
    assert job.apply_url == "https://linear.app/careers/job-1"
    assert job.apply_url_type is ApplyUrlType.DIRECT
    assert job.remote_status is RemoteStatus.REMOTE
    assert job.source_job_id == "linear:job-1"


def test_greenhouse_without_boards_makes_no_requests() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(str(request.url))

    result = GreenhouseSource().fetch(_context(_client(handler), feeds=[]))
    assert result.jobs == []


def test_collect_checks_a_greenhouse_board_found_on_an_aggregator(tmp_path) -> None:
    moment = datetime(2026, 10, 17, 15, tzinfo=UTC)

    class Aggregator:
        name = "jobicy"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                jobs=[
                    Job(
                        source="jobicy",
                        source_job_id="1",
                        title="Senior Rust Engineer",
                        company="Acme",
                        url="https://jobicy.com/jobs/1",
                        description="Apply at https://boards.greenhouse.io/acme/jobs/42",
                        location_text="Worldwide",
                        published_at=moment,
                        latam_eligibility=LatamEligibility.ACCEPTED,
                        keyword_status=KeywordStatus.MATCHED,
                    )
                ]
            )

    class Board:
        name = "greenhouse"
        default_interval_hours = 24
        default_enabled = True
        feeds: list[str] = []

        def fetch(self, ctx: FetchContext) -> FetchResult:
            self.feeds = list(ctx.settings.feeds)
            return FetchResult(jobs=[])

    board = Board()
    database = tmp_path / "jobs.sqlite"
    settings = settings_from_mapping(
        {
            "keywords": ["rust"],
            "database": {"path": str(database)},
            "scheduler": {"jitter_seconds": 0},
            "sources": {
                "jobicy": {"enabled": True, "interval_hours": 12},
                "greenhouse": {"enabled": True, "interval_hours": 24, "feeds": []},
            },
        }
    )
    collect(
        settings,
        registry={"jobicy": Aggregator(), "greenhouse": board},
        now=moment,
        ignore_schedule=True,
    )
    assert board.feeds == ["acme"]


def test_collect_stores_the_ats_opening_on_the_aggregator_job(tmp_path) -> None:
    moment = datetime(2026, 10, 17, 15, tzinfo=UTC)

    class Aggregator:
        name = "jobicy"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(
                jobs=[
                    Job(
                        source="jobicy",
                        source_job_id="1",
                        title="Senior Rust Engineer",
                        company="Acme",
                        url="https://jobicy.com/jobs/1",
                        description=(
                            '<a href="https://boards.greenhouse.io/acme/jobs/42">Apply</a>'
                        ),
                        location_text="Worldwide",
                        published_at=moment,
                        latam_eligibility=LatamEligibility.ACCEPTED,
                        keyword_status=KeywordStatus.MATCHED,
                    )
                ]
            )

    class Board:
        name = "greenhouse"
        default_interval_hours = 24
        default_enabled = True
        called = False

        def fetch(self, ctx: FetchContext) -> FetchResult:
            self.called = True
            return FetchResult(jobs=[])

    board = Board()
    database = tmp_path / "jobs.sqlite"
    settings = settings_from_mapping(
        {
            "keywords": ["rust"],
            "database": {"path": str(database)},
            "scheduler": {"jitter_seconds": 0},
            "sources": {
                "jobicy": {"enabled": True, "interval_hours": 12},
                "greenhouse": {"enabled": False, "interval_hours": 24},
            },
        }
    )
    collect(
        settings,
        registry={"jobicy": Aggregator(), "greenhouse": board},
        now=moment,
        ignore_schedule=True,
    )
    assert board.called is False
    store = Store(database)
    stored = store.get_job("jobicy", "1")
    assert stored is not None
    assert stored.apply_url == "https://boards.greenhouse.io/acme/jobs/42"
    assert stored.apply_url_type == "direct"
    assert store.count_jobs() == 1
    store.close()
