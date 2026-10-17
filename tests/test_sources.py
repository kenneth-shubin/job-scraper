import json
from pathlib import Path

import httpx

from latam_jobs.http import HttpClient
from latam_jobs.models import RemoteStatus
from latam_jobs.settings import SourceSettings
from latam_jobs.sources.base import FetchContext, SourceError
from latam_jobs.sources.getonboard import GetOnBoardSource
from latam_jobs.sources.himalayas import HimalayasSource
from latam_jobs.sources.remoteok import RemoteOkSource
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


def test_himalayas_does_not_list_every_country() -> None:
    payload = {
        "jobs": [
            {
                "title": "Backend",
                "applicationLink": "https://himalayas.app/jobs/backend",
                "guid": "backend",
                "companyName": "Example",
                "description": "Python",
                "locationRestrictions": ["Argentina", "Brazil", "Chile", "Colombia", "Peru"],
            }
        ]
    }
    result = HimalayasSource().fetch(_context(_client(payload)))
    job = result.jobs[0]
    assert job.location_text == "5 countries"
    assert job.location_countries == ("argentina", "brazil", "chile", "colombia", "peru")


def test_himalayas_keeps_its_page_cap_and_does_not_send_a_date_filter() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        page = request.url.params["page"]
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "title": f"Role {page}",
                        "applicationLink": f"https://himalayas.app/jobs/{page}",
                        "guid": page,
                        "companyName": "Example",
                        "description": "Backend",
                        "pubDate": 1760700000,
                    }
                ]
            },
        )

    client = HttpClient(
        timeout=5, max_retries=0, transport=httpx.MockTransport(handler), sleeper=lambda _s: None
    )
    settings = SourceSettings(enabled=True, max_pages=2, url="https://himalayas.app/jobs/api")
    result = HimalayasSource().fetch(FetchContext(client=client, settings=settings))
    assert [job.source_job_id for job in result.jobs] == ["1", "2"]
    assert [url.params["page"] for url in seen] == ["1", "2"]
    assert {url.params["sort"] for url in seen} == {"recent"}
    assert all("published" not in str(url) for url in seen)


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
    assert (
        hybrid.url
        == "https://www.getonbrd.com/jobs/programming/technical-lead-senior-bci-santiago-3e19"
    )
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


def test_remoteok_skips_legal_notice_and_keeps_location() -> None:
    payload = json.loads((FIXTURES / "remoteok_jobs.json").read_text(encoding="utf-8"))
    result = RemoteOkSource().fetch(_context(_client(payload)))
    locations = {job.location_text for job in result.jobs}
    assert "Worldwide" in locations
    assert "LATAM" in locations
    assert "United States" in locations
    assert all(job.source == "remoteok" for job in result.jobs)
    assert result.skipped_items == 0


def test_remoteok_direct_link_requires_a_different_host() -> None:
    same = RemoteOkSource().fetch(
        _context(
            _client(
                [
                    {
                        "id": 1,
                        "position": "Backend",
                        "company": "Example",
                        "url": "https://remoteok.com/remote-jobs/1",
                        "apply_url": "https://www.remoteok.com/remote-jobs/1",
                        "description": "Python",
                        "date": "2026-10-17T15:00:00Z",
                    }
                ]
            )
        )
    )
    assert same.jobs[0].apply_url is None
    assert same.jobs[0].apply_url_type.value == "job_board"

    direct = RemoteOkSource().fetch(
        _context(
            _client(
                [
                    {
                        "id": 2,
                        "position": "Backend",
                        "company": "Example",
                        "url": "https://remoteok.com/remote-jobs/2",
                        "apply_url": "https://boards.greenhouse.io/acme/jobs/2",
                        "description": "Python",
                        "date": "2026-10-17T15:00:00Z",
                    }
                ]
            )
        )
    )
    assert direct.jobs[0].apply_url == "https://boards.greenhouse.io/acme/jobs/2"
    assert direct.jobs[0].apply_url_type.value == "direct"


def test_remoteok_empty_and_malformed() -> None:
    legal = [{"legal": "link back", "last_updated": 1}]
    empty = RemoteOkSource().fetch(_context(_client(legal)))
    assert empty.jobs == []
    payload = [{"id": 1, "company": "Example"}]
    result = RemoteOkSource().fetch(_context(_client(payload)))
    assert result.jobs == []
    assert result.skipped_items == 1
    try:
        RemoteOkSource().fetch(_context(_client({"jobs": []})))
    except SourceError:
        pass
    else:
        raise AssertionError("expected a non-list Remote OK document to fail")
