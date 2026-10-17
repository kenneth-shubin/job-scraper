import json
from pathlib import Path

import httpx

from latam_jobs.http import HttpClient
from latam_jobs.models import Job
from latam_jobs.pipeline import collect
from latam_jobs.settings import SourceSettings, settings_from_mapping
from latam_jobs.sources.base import FetchContext, SourceSnapshot
from latam_jobs.sources.remotive import RemotiveSource

FIXTURE = Path("tests/fixtures/remotive_jobs.json")


def _client(payload: object, status: int = 200) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if status == 304:
            return httpx.Response(304, headers={"ETag": "abc"})
        return httpx.Response(status, json=payload)

    return HttpClient(
        timeout=5,
        max_retries=0,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )


def test_parses_real_fixture_fields() -> None:
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    source = RemotiveSource()
    result = source.fetch(
        FetchContext(
            client=_client(payload),
            settings=SourceSettings(enabled=True, url="https://remotive.com/api/remote-jobs"),
        )
    )
    by_id = {job.source_job_id: job for job in result.jobs}
    assert set(by_id) == {"2091140", "2091144", "2091132", "2091045", "2091131"}
    shopify = by_id["2091140"]
    assert shopify.source == "remotive"
    assert shopify.title == "Senior Shopify Developer"
    assert shopify.location_text == "Worldwide"
    assert shopify.salary == "$80k - $150k"
    assert shopify.published_at is not None
    assert shopify.published_at.tzinfo is not None
    assert "<" not in shopify.description
    assert by_id["2091045"].salary is None
    assert by_id["2091144"].location_text == "USA"
    assert "Mexico" in by_id["2091132"].location_text
    assert "python" in by_id["2091132"].tags


def test_skips_malformed_job_and_rejects_malformed_document() -> None:
    source = RemotiveSource()
    payload = {
        "jobs": [
            {
                "id": 1,
                "title": "Backend Engineer",
                "company_name": "Example",
                "url": "https://remotive.com/remote-jobs/backend-1",
                "description": "<p>Python</p>",
                "candidate_required_location": "Worldwide",
                "publication_date": "2026-01-02T03:04:05",
            },
            {"id": 2, "title": "Missing URL"},
        ]
    }
    result = source.fetch(
        FetchContext(client=_client(payload), settings=SourceSettings(enabled=True))
    )
    assert len(result.jobs) == 1
    assert result.skipped_items == 1
    assert result.jobs[0].description == "Python"
    try:
        source.fetch(
            FetchContext(client=_client(["not", "a", "document"]), settings=SourceSettings())
        )
    except Exception as exc:
        assert "jobs list" in str(exc)
    else:
        raise AssertionError("expected a malformed document to fail")


def test_not_modified_returns_no_jobs() -> None:
    source = RemotiveSource()
    result = source.fetch(
        FetchContext(
            client=_client({}, status=304),
            settings=SourceSettings(enabled=True),
            state=SourceSnapshot(etag="abc"),
        )
    )
    assert result.not_modified is True
    assert result.jobs == []
    assert result.etag == "abc"


def test_one_failing_source_does_not_stop_remotive() -> None:
    class Boom:
        name = "boom"
        default_interval_hours = 24
        default_enabled = False

        def fetch(self, ctx: FetchContext) -> None:
            raise RuntimeError("boom")

    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    settings = settings_from_mapping(
        {
            "sources": {
                "boom": {"enabled": True, "interval_hours": 24},
                "remotive": {"enabled": True, "interval_hours": 12},
            }
        }
    )
    runs = collect(
        settings,
        registry={"boom": Boom(), "remotive": RemotiveSource()},
        client=_client(payload),
    )
    by_name = {run.name: run for run in runs}
    assert by_name["boom"].error is not None
    assert by_name["remotive"].jobs
    assert all(isinstance(job, Job) for job in by_name["remotive"].jobs)
