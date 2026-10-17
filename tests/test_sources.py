import json
from pathlib import Path

import httpx

from latam_jobs.http import HttpClient
from latam_jobs.models import RemoteStatus
from latam_jobs.settings import SourceSettings
from latam_jobs.sources.base import FetchContext, SourceError
from latam_jobs.sources.getonboard import GetOnBoardSource
from latam_jobs.sources.himalayas import HimalayasSource
from latam_jobs.sources.wwr import WwrSource

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


def test_getonboard_maps_modality_and_company() -> None:
    payload = json.loads((FIXTURES / "getonboard_jobs.json").read_text(encoding="utf-8"))
    result = GetOnBoardSource().fetch(_context(_client(payload)))
    by_id = {job.source_job_id: job for job in result.jobs}
    hybrid = by_id["technical-lead-senior-bci-santiago-3e19"]
    remote = by_id["consultor-c4c-lisit-remote"]
    assert hybrid.remote_status is RemoteStatus.HYBRID
    assert hybrid.location_countries == ("chile",)
    assert hybrid.company == "Bci"
    assert remote.remote_status is RemoteStatus.REMOTE
    assert remote.location_text == "Worldwide"
    assert remote.location_countries == ()


def test_getonboard_empty_and_malformed() -> None:
    empty = GetOnBoardSource().fetch(_context(_client({"data": [], "meta": {"total_pages": 0}})))
    assert empty.jobs == []
    payload = {"data": [{"id": "missing-url", "attributes": {"title": "No link"}}]}
    result = GetOnBoardSource().fetch(_context(_client(payload)))
    assert result.jobs == []
    assert result.skipped_items == 1
    try:
        GetOnBoardSource().fetch(_context(_client(["nope"])))
    except SourceError:
        pass
    else:
        raise AssertionError("expected a malformed Get on Board document to fail")


def test_wwr_parses_region_and_company() -> None:
    xml = (FIXTURES / "wwr_programming.xml").read_text(encoding="utf-8")
    result = WwrSource().fetch(
        _context(_client(xml, json_body=False), feeds=["https://weworkremotely.com/example.rss"])
    )
    assert len(result.jobs) == 2
    first = result.jobs[0]
    assert first.company == "Humane World for Animals"
    assert first.title == "Web Designer, eCRM"
    assert first.location_text == "Anywhere in the World"
    assert first.url.startswith("https://weworkremotely.com/")


def test_wwr_empty_and_malformed_entry() -> None:
    empty_xml = "<rss><channel><title>We Work Remotely</title></channel></rss>"
    empty = WwrSource().fetch(_context(_client(empty_xml, json_body=False)))
    assert empty.jobs == []
    broken = """
    <rss><channel>
      <item><title>Example: Engineer</title><region>Worldwide</region></item>
    </channel></rss>
    """
    result = WwrSource().fetch(_context(_client(broken, json_body=False)))
    assert result.jobs == []
    assert result.skipped_items == 1
    try:
        WwrSource().fetch(_context(_client("not a feed", json_body=False)))
    except SourceError:
        pass
    else:
        raise AssertionError("expected a non-feed to fail")
