import argparse
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from latam_jobs.cli import _cmd_serve
from latam_jobs.models import ApplyUrlType, Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.settings import DatabaseSettings, Settings, SourceSettings
from latam_jobs.sources import SOURCES
from latam_jobs.sources.remotive import RemotiveSource
from latam_jobs.store import Store
from latam_jobs.web.app import create_app

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
        published_at=NOW,
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
    assert "restricted to brazil" in rejected.text
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
    status = client.get("/status")
    assert "example" in settings.text
    assert "remotive" in settings.text
    assert "example" in status.text
    assert "Never" in status.text
    assert "Disabled" in status.text
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
    direct = _job(title="Direct backend", url="https://remotive.com/jobs/direct")
    direct.apply_url = "https://boards.greenhouse.io/acme/jobs/1"
    direct.apply_url_type = ApplyUrlType.DIRECT
    board = _job(
        source_job_id="board",
        title="Board backend",
        url="https://remotive.com/jobs/board",
    )
    store.upsert_jobs([direct, board], NOW)

    quiet = client.get("/")
    assert quiet.status_code == 200
    assert "Job Scraper Status" in quiet.text
    assert 'hx-trigger="every 60s"' in quiet.text
    assert "New jobs found: 0" in quiet.text
    assert "Scraper status: Never" in quiet.text
    assert "Apply Directly" in quiet.text
    assert "Apply on Job Board" in quiet.text
    assert "View Original Job" in quiet.text
    assert 'target="_blank"' in quiet.text
    assert 'rel="noopener noreferrer"' in quiet.text
    assert "https://boards.greenhouse.io/acme/jobs/1" in quiet.text

    only_direct = client.get("/?application=direct")
    assert "Direct backend" in only_direct.text
    assert "Board backend" not in only_direct.text
    only_board = client.get("/jobs/rows?application=job_board")
    assert "Board backend" in only_board.text
    assert "Direct backend" not in only_board.text

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
    assert "Scraper status: Success" in status.text
    assert "Oct 17, 2026, 10:00 AM" in status.text
    assert "Oct 17, 2026, 12:00 PM" in status.text
    example = re.search(r"<td>example</td>(.*?)</tr>", status.text, re.S)
    assert example is not None
    assert "<td>Disabled</td>" in example.group(1)
    remotive = re.search(r"<td>remotive</td>(.*?)</tr>", status.text, re.S)
    assert remotive is not None
    assert "<td>Success</td>" in remotive.group(1)

    store.record_failure(
        "remotive",
        now=NOW + timedelta(hours=3),
        backoff_until=NOW + timedelta(days=1),
        status_code=500,
        error="HTTP 500",
        previous=store.get_schedule("remotive"),
    )
    store.record_run(
        started_at=NOW + timedelta(hours=3),
        finished_at=NOW + timedelta(hours=3),
        outcome="failed",
        new_jobs=0,
        sources_ok=0,
        sources_failed=1,
    )
    failed = client.get("/")
    failed_row = re.search(r"<td>remotive</td>(.*?)</tr>", failed.text, re.S)
    assert failed_row is not None
    assert "<td>Failed</td>" in failed_row.group(1)
    assert "Scraper status: Failed" in failed.text
    assert "Last successful collection:" in failed.text
    assert "Oct 17, 2026, 10:00 AM" in failed.text
    store.close()


def test_unknown_display_timezone_is_rejected(tmp_path: Path) -> None:
    client, store, bootstrap = _open(tmp_path)
    response = _save(client, display_timezone="Mars/Base")
    assert response.status_code == 400
    assert "Unknown timezone" in response.text
    assert store.load_or_seed(bootstrap).display_timezone == "America/Bogota"
    store.close()
