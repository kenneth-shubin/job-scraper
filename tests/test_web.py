import argparse
import re
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from latam_jobs.cli import _cmd_serve
from latam_jobs.models import ApplyUrlType, Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.settings import DatabaseSettings, Settings, SourceSettings
from latam_jobs.sources import SOURCES
from latam_jobs.sources.base import FetchContext, FetchResult
from latam_jobs.sources.remotive import RemotiveSource
from latam_jobs.store import Store
from latam_jobs.web.app import create_app
from latam_jobs.web.timefmt import format_posted

NOW = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)


class ExampleSource:
    name = "example"
    default_interval_hours = 6
    default_enabled = False
    default_url = "https://example.com/jobs"


def _job(
    *,
    source: str = "remotive",
    source_job_id: str = "1",
    title: str = "Backend engineer",
    url: str = "https://example.com/jobs/1",
    location_text: str = "Worldwide",
    location_countries: tuple[str, ...] = (),
    eligibility: LatamEligibility = LatamEligibility.ACCEPTED,
    published_at: datetime | None = None,
) -> Job:
    return Job(
        source=source,
        source_job_id=source_job_id,
        title=title,
        company="Example",
        url=url,
        description="A backend role.",
        location_text=location_text,
        location_countries=location_countries,
        remote_status=RemoteStatus.REMOTE,
        latam_eligibility=eligibility,
        eligibility_reason="stored before the latest settings",
        keyword_status=KeywordStatus.MATCHED,
        keyword_reason="matched 'backend' in title",
        published_at=published_at or datetime.now(UTC),
    )


def _open(
    tmp_path: Path, registry: dict[str, object] | None = None
) -> tuple[TestClient, Store, Settings]:
    database = tmp_path / "jobs.sqlite"
    bootstrap = Settings(
        database=DatabaseSettings(path=str(database)),
        residence_country="colombia",
        keywords=["backend"],
        remote_only=True,
    )
    store = Store(database)
    store.migrate()
    store.load_or_seed(bootstrap)
    app = create_app(
        bootstrap=bootstrap,
        store=store,
        registry=registry or SOURCES,
        secret="test-secret",
        password="test-password",
    )
    client = TestClient(app)
    _unlock(client)
    return client, store, bootstrap


def _unlock(client: TestClient, password: str = "test-password") -> None:
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    client.post(
        "/login",
        data={"csrf_token": match.group(1), "password": password},
        follow_redirects=True,
    )


def _csrf(client: TestClient) -> str:
    page = client.get("/settings")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def _save(client: TestClient, **fields: str) -> object:
    payload = {
        "csrf_token": _csrf(client),
        "residence_country": "colombia",
        "keywords": "backend",
        "exclude_keywords": "",
        "remote_only": "on",
    }
    payload.update(fields)
    return client.post("/settings", data=payload, follow_redirects=True)


def test_jobs_page_defaults_to_accepted_and_filters(tmp_path: Path) -> None:
    client, store, _bootstrap = _open(tmp_path)
    store.upsert_jobs(
        [
            _job(title="Worldwide backend", url="https://example.com/jobs/world"),
            _job(
                source="himalayas",
                source_job_id="br",
                title="Brazil only backend",
                url="https://example.com/jobs/brazil",
                location_text="Brazil",
                location_countries=("brazil",),
                eligibility=LatamEligibility.REJECTED,
            ),
        ],
        NOW,
    )
    accepted = client.get("/")
    assert accepted.status_code == 200
    assert "Worldwide backend" in accepted.text
    assert "Brazil only backend" not in accepted.text
    assert "<th>Eligibility</th>" not in accepted.text
    assert ">Eligibility<" not in accepted.text

    rejected = client.get("/?eligibility=rejected")
    assert "Brazil only backend" in rejected.text
    assert "Worldwide backend" not in rejected.text

    by_source = client.get("/?source=himalayas&eligibility=all")
    assert "Brazil only backend" in by_source.text
    assert "Worldwide backend" not in by_source.text

    search = client.get("/?q=Worldwide&eligibility=all")
    assert "Worldwide backend" in search.text
    assert "Brazil only backend" not in search.text

    rows = client.get("/jobs/rows?eligibility=rejected")
    assert rows.status_code == 200
    assert "<html" not in rows.text.lower()
    assert "Brazil only backend" in rows.text
    store.close()


def test_settings_save_reclassifies_and_invalid_country_is_kept(tmp_path: Path) -> None:
    client, store, bootstrap = _open(tmp_path)
    store.upsert_jobs(
        [
            _job(
                title="Brazil only backend",
                url="https://example.com/jobs/brazil",
                location_text="Brazil",
                location_countries=("brazil",),
            )
        ],
        NOW,
    )
    saved = _save(
        client,
        residence_country="brazil",
        keywords="django, react",
        **{"notifications-enabled": "on"},
    )
    assert saved.status_code == 200
    assert "Settings saved" in saved.text
    assert "Send Telegram alerts" in saved.text
    reloaded = store.load_or_seed(bootstrap)
    assert reloaded.residence_country == "brazil"
    assert reloaded.keywords == ["django", "react"]
    assert reloaded.notifications.enabled is True

    _save(client, residence_country="colombia", keywords="backend")
    rejected = client.get("/?eligibility=rejected")
    assert "Brazil only backend" in rejected.text
    assert "restricted to brazil" not in rejected.text
    assert "<th>Reason</th>" not in rejected.text
    accepted = client.get("/")
    assert "Brazil only backend" not in accepted.text

    invalid = _save(client, residence_country="spain", keywords="rust")
    assert invalid.status_code == 400
    assert "Unknown residence" in invalid.text
    unchanged = store.load_or_seed(bootstrap)
    assert unchanged.residence_country == "colombia"
    assert unchanged.keywords == ["backend"]
    store.close()


def test_registry_source_appears_without_a_template_change(tmp_path: Path) -> None:
    registry = {"remotive": RemotiveSource(), "example": ExampleSource()}
    client, store, _bootstrap = _open(tmp_path, registry)
    settings = client.get("/settings")
    assert "example" in settings.text
    assert "remotive" in settings.text
    assert client.get("/status", follow_redirects=False).headers["location"] == "/"
    store.close()


def test_settings_post_without_csrf_does_not_write(tmp_path: Path) -> None:
    client, store, bootstrap = _open(tmp_path)
    response = client.post(
        "/settings",
        data={"residence_country": "brazil", "keywords": "django"},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert store.load_or_seed(bootstrap).residence_country == "colombia"
    store.close()


def test_show_filter_is_one_or_three_days_and_older_jobs_are_removed(tmp_path: Path) -> None:
    client, store, _bootstrap = _open(tmp_path)
    now = datetime.now(UTC)
    old_at = now - timedelta(days=6)
    recent_at = now - timedelta(days=2)
    store.upsert_jobs(
        [
            _job(
                source_job_id="old",
                title="Six day backend",
                url="https://example.com/jobs/old",
                published_at=old_at,
            )
        ],
        old_at,
    )
    store.upsert_jobs(
        [
            _job(
                source_job_id="recent",
                title="Two day backend",
                url="https://example.com/jobs/recent",
                published_at=recent_at,
            )
        ],
        recent_at,
    )
    store.upsert_jobs(
        [_job(source_job_id="fresh", title="Fresh backend", url="https://example.com/jobs/fresh")],
        now,
    )
    page = client.get("/")
    assert "Last 24 hours" in page.text
    assert "Last 3 days" in page.text
    assert "Fresh backend" in page.text
    assert "Two day backend" not in page.text
    assert "Six day backend" not in page.text
    assert store.get_job("remotive", "old") is None
    three_days = client.get("/jobs/rows?posted=3d")
    assert "Fresh backend" in three_days.text
    assert "Two day backend" in three_days.text
    assert "Six day backend" not in three_days.text
    store.close()


def test_many_countries_are_not_listed_in_the_location_column(tmp_path: Path) -> None:
    client, store, _bootstrap = _open(tmp_path)
    store.upsert_jobs(
        [
            _job(
                title="Regional backend",
                url="https://example.com/jobs/region",
                location_text="Argentina, Brazil, Chile, Colombia, Peru",
                location_countries=("argentina", "brazil", "chile", "colombia", "peru"),
            )
        ],
        NOW,
    )
    page = client.get("/")
    assert "5 countries" in page.text
    assert "Argentina, Brazil, Chile, Colombia, Peru" not in page.text
    store.close()


def test_serve_requires_web_secret(monkeypatch: object) -> None:
    monkeypatch.delenv("WEB_SECRET", raising=False)  # type: ignore[attr-defined]
    assert _cmd_serve(argparse.Namespace(config=Path("settings.yaml"))) == 2


def test_jobs_page_shows_apply_links_and_run_status(tmp_path: Path) -> None:
    database = tmp_path / "jobs.sqlite"
    bootstrap = Settings(
        database=DatabaseSettings(path=str(database)),
        residence_country="colombia",
        keywords=["backend"],
        sources={"remotive": SourceSettings(enabled=True, interval_hours=12)},
    )
    store = Store(database)
    store.migrate()
    store.load_or_seed(bootstrap)
    registry = {"remotive": RemotiveSource(), "example": ExampleSource()}
    client = TestClient(
        create_app(
            bootstrap=bootstrap,
            store=store,
            registry=registry,
            secret="test-secret",
            password="test-password",
        )
    )
    _unlock(client)
    posted = datetime.now(UTC)
    direct = _job(
        title="Direct backend",
        url="https://remotive.com/jobs/direct",
        published_at=posted,
    )
    direct.apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    direct.apply_url_type = ApplyUrlType.DIRECT
    board = _job(
        source_job_id="board",
        title="Board backend",
        url="https://remotive.com/jobs/board",
    )
    career = _job(
        source_job_id="career",
        title="Career backend",
        url="https://jobs.lever.co/spotify/abc",
    )
    career.apply_url_type = ApplyUrlType.CAREER
    store.upsert_jobs([direct, board, career], posted)

    quiet = client.get("/")
    assert quiet.status_code == 200
    assert "Job Scraper Status" not in quiet.text
    assert "Next scheduled check" not in quiet.text
    assert "Check for jobs" in quiet.text
    assert 'hx-trigger="every 15s"' in quiet.text
    assert "Last checked: Never" in quiet.text
    assert "New jobs found: 0" in quiet.text
    assert "<th>Posted</th>" in quiet.text
    assert format_posted(posted, "America/Bogota", datetime.now(UTC)) in quiet.text
    assert "Oct 17, 2026, 10:00 AM" not in quiet.text
    assert "Apply Directly" in quiet.text
    assert "Apply on Job Board" in quiet.text
    assert "View Original Job" in quiet.text
    assert "https://boards.greenhouse.io/acme/jobs/1" in quiet.text
    assert 'target="_blank"' not in quiet.text

    only_direct = client.get("/?application=direct")
    assert "Direct backend" in only_direct.text
    assert "Board backend" not in only_direct.text
    only_board = client.get("/jobs/rows?application=job_board")
    assert "Board backend" in only_board.text
    assert "Direct backend" not in only_board.text
    assert "View Original Job" not in only_board.text
    career_row = client.get("/jobs/rows?q=Career")
    assert "View Original Job" in career_row.text
    assert "https://jobs.lever.co/spotify/abc" in career_row.text
    assert "Apply on Job Board" not in career_row.text
    assert "Apply Directly" not in career_row.text

    store.record_success(
        "remotive",
        now=NOW,
        next_fetch_at=NOW + timedelta(hours=2),
        etag=None,
        last_modified=None,
        status_code=200,
    )
    store.record_success(
        "example",
        now=NOW,
        next_fetch_at=NOW + timedelta(hours=1),
        etag=None,
        last_modified=None,
        status_code=200,
    )
    store.record_run(
        started_at=NOW,
        finished_at=NOW,
        outcome="success",
        new_jobs=2,
        sources_ok=1,
        sources_failed=0,
    )
    status = client.get("/status/summary")
    assert "New jobs found: 2" in status.text
    assert "Last checked:" in status.text
    assert "Oct 17, 2026, 10:00 AM" in status.text
    assert "Scraper status" not in status.text
    assert "Next scheduled check" not in status.text
    store.close()


def test_check_button_runs_enabled_sources_now(tmp_path: Path) -> None:
    started = threading.Event()
    release = threading.Event()
    calls: list[str] = []

    class Probe:
        name = "probe"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            calls.append("probe")
            started.set()
            assert release.wait(timeout=5)
            return FetchResult(jobs=[], status_code=200)

    client, store, bootstrap = _open(tmp_path, {"probe": Probe()})
    current = store.load_or_seed(bootstrap)
    store.save_settings(
        current.model_copy(
            update={"sources": {"probe": SourceSettings(enabled=True, interval_hours=12)}}
        ),
        bootstrap=bootstrap,
    )
    denied = client.post("/jobs/run", data={}, follow_redirects=False)
    assert denied.status_code == 403

    first = client.post("/jobs/run", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    assert first.status_code == 200
    assert "Checking for jobs." in first.text
    assert started.wait(timeout=5)
    second = client.post("/jobs/run", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    assert "A check is already running." in second.text
    release.set()
    thread = client.app.state.collector_thread
    assert thread is not None
    thread.join(timeout=5)
    assert calls == ["probe"]
    client.post("/jobs/run", data={"csrf_token": _csrf(client)}, follow_redirects=False)
    follow = client.app.state.collector_thread
    assert follow is not None
    follow.join(timeout=5)
    assert calls == ["probe", "probe"]
    store.close()


def test_unknown_display_timezone_is_rejected(tmp_path: Path) -> None:
    client, store, bootstrap = _open(tmp_path)
    response = _save(client, display_timezone="Mars/Base")
    assert response.status_code == 400
    assert "Unknown timezone" in response.text
    assert store.load_or_seed(bootstrap).display_timezone == "America/Bogota"
    store.close()
