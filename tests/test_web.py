import argparse
import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from latam_jobs.cli import _cmd_serve
from latam_jobs.models import Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.settings import DatabaseSettings, Settings
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
    )
    return TestClient(app), store, bootstrap


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
    assert "Not fetched yet" in status.text
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
