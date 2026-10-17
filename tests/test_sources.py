import json
from pathlib import Path

import httpx

from latam_jobs.http import HttpClient
from latam_jobs.settings import SourceSettings
from latam_jobs.sources.base import FetchContext, SourceError
from latam_jobs.sources.himalayas import HimalayasSource

FIXTURES = Path("tests/fixtures")


def _client(body: object, *, json_body: bool = True) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if json_body:
            return httpx.Response(200, json=body)
        return httpx.Response(200, text=str(body))

    return HttpClient(
        timeout=5, max_retries=0, transport=httpx.MockTransport(handler), sleeper=lambda _s: None
    )


def _context(client: HttpClient, **kwargs: object) -> FetchContext:
    settings = SourceSettings(enabled=True, max_pages=1, **kwargs)  # type: ignore[arg-type]
    return FetchContext(client=client, settings=settings)


def test_himalayas_maps_locations_and_skips_bad_items() -> None:
    payload = json.loads((FIXTURES / "himalayas_jobs.json").read_text(encoding="utf-8"))
    payload["jobs"].append({"title": "", "applicationLink": "https://example.com", "guid": "x"})
    result = HimalayasSource().fetch(_context(_client(payload)))
    by_location = {job.location_text: job for job in result.jobs}
    assert "Worldwide" in by_location
    assert by_location["Worldwide"].location_countries == ()
    assert by_location["Brazil"].location_countries == ("brazil",)
    assert by_location["United States"].location_countries == ("united-states",)
    assert result.jobs[0].url.startswith("https://himalayas.app/")
    assert result.skipped_items == 1
    assert "<" not in result.jobs[0].description


def test_himalayas_empty_and_malformed() -> None:
    empty = HimalayasSource().fetch(_context(_client({"jobs": []})))
    assert empty.jobs == []
    try:
        HimalayasSource().fetch(_context(_client({"updatedAt": 1})))
    except SourceError as exc:
        assert "jobs list" in str(exc)
    else:
        raise AssertionError("expected a malformed Himalayas document to fail")
