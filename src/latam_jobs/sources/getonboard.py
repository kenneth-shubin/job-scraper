"""Get on Board public jobs API.

Verified from https://www.getonbrd.com/api-doc.html and a live response:
GET https://www.getonbrd.com/api/v0/categories/programming/jobs
The public API needs no key. Reads are limited to 20 every 5 seconds per IP, so this
adapter sends at most one request a second and requests at most max_pages with
per_page=100. Each configured keyword is searched
on the search endpoint, with remote=true when remote_only is on. With no keywords
it reads the programming category. The payload exposes links.public_url and no
separate company or ATS URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.ats_directory import Pacer
from latam_jobs.geo import country_slug
from latam_jobs.locations import country_list_label
from latam_jobs.models import Job, RemoteStatus
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text

logger = logging.getLogger(__name__)
PAUSE_SECONDS = 1.0
_SEARCH_URL = "https://www.getonbrd.com/api/v0/search/jobs"

_MODALITY = {
    "fully_remote": RemoteStatus.REMOTE,
    "remote_local": RemoteStatus.REMOTE,
    "temporarily_remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "no_remote": RemoteStatus.ONSITE,
}


class GetOnBoardSource:
    name = "getonboard"
    default_interval_hours = 3
    default_enabled = True
    default_url = "https://www.getonbrd.com/api/v0/categories/programming/jobs"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        category_url = ctx.settings.url or self.default_url
        jobs: list[Job] = []
        seen: set[str] = set()
        skipped = 0
        status_code = None
        pacer = Pacer(ctx.sleeper, PAUSE_SECONDS)
        terms = _search_terms(ctx.search_terms) or (None,)
        for term in terms:
            for page in range(1, ctx.settings.max_pages + 1):
                pacer.wait()
                params = {
                    "page": str(page),
                    "per_page": "100",
                    "lang": "en",
                    "expand[]": "company",
                }
                if term:
                    params["query"] = term
                    if ctx.remote_only:
                        params["remote"] = "true"
                response = ctx.client.get(_SEARCH_URL if term else category_url, params=params)
                status_code = response.status_code
                payload = _payload(response.json())
                batch = payload.get("data")
                if not isinstance(batch, list):
                    raise SourceError("Get on Board document is missing a data list")
                if not batch:
                    break
                for item in batch:
                    parsed = _parse_job(item)
                    if parsed is None:
                        skipped += 1
                        continue
                    if parsed.source_job_id in seen:
                        continue
                    seen.add(parsed.source_job_id)
                    jobs.append(parsed)
                meta = payload.get("meta")
                total_pages = meta.get("total_pages") if isinstance(meta, dict) else None
                if isinstance(total_pages, int) and page >= total_pages:
                    break
        if skipped:
            logger.info("getonboard skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _search_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SourceError("Get on Board returned a document that is not an object")
    return value


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    attributes = item.get("attributes")
    links = item.get("links")
    if not isinstance(source_id, str) or not source_id.strip():
        return None
    if not isinstance(attributes, dict):
        return None
    title = attributes.get("title")
    public_url = links.get("public_url") if isinstance(links, dict) else None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(public_url, str) or not public_url.strip():
        return None
    public_url = _listing_url(public_url.strip(), attributes.get("category_name"))
    description = attributes.get("description")
    location_text, countries = _location(attributes)
    modality = attributes.get("remote_modality")
    remote_status = (
        _MODALITY.get(modality, RemoteStatus.UNKNOWN)
        if isinstance(modality, str)
        else RemoteStatus.UNKNOWN
    )
    return Job(
        source="getonboard",
        source_job_id=source_id.strip(),
        title=title.strip(),
        company=_company(attributes.get("company")),
        url=public_url,
        description=html_to_text(description) if isinstance(description, str) else "",
        location_text=location_text,
        location_countries=countries,
        salary=_salary(attributes),
        published_at=_unix_time(attributes.get("published_at")),
        remote_status=remote_status,
    )


def _listing_url(public_url: str, category_name: object) -> str:
    if not isinstance(category_name, str) or not category_name.strip():
        return public_url
    category = category_name.strip().casefold().replace(" ", "-")
    marker = "/jobs/"
    if f"{marker}{category}/" in public_url or marker not in public_url:
        return public_url
    return public_url.replace(marker, f"{marker}{category}/", 1)


def _company(value: object) -> str:
    if not isinstance(value, dict):
        return "Unknown"
    data = value.get("data")
    if not isinstance(data, dict):
        return "Unknown"
    attributes = data.get("attributes")
    if isinstance(attributes, dict) and isinstance(attributes.get("name"), str):
        name = attributes["name"].strip()
        if name:
            return name
    return "Unknown"


def _location(attributes: dict[str, object]) -> tuple[str, tuple[str, ...]]:
    raw_countries = attributes.get("countries")
    names = []
    if isinstance(raw_countries, list):
        for country in raw_countries:
            if (
                isinstance(country, str)
                and country.strip()
                and country.strip().casefold() != "remote"
            ):
                names.append(country.strip())
    zone = _zone(attributes.get("remote_zone"))
    slugs = tuple(dict.fromkeys(_slug(name) for name in names))
    modality = attributes.get("remote_modality")
    if modality == "fully_remote" and not slugs:
        return zone or "Worldwide", ()
    if names:
        return country_list_label(names, zone=zone), slugs
    return zone or "Remote", ()


def _zone(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("name", "label"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return item.strip()
    return ""


def _slug(name: str) -> str:
    return country_slug(name) or name.casefold().replace(" ", "-")


def _salary(attributes: dict[str, object]) -> str | None:
    minimum = attributes.get("min_salary")
    maximum = attributes.get("max_salary")
    if minimum in (None, "") and maximum in (None, ""):
        return None
    if minimum not in (None, "") and maximum not in (None, ""):
        return f"{minimum}-{maximum}"
    return str(minimum or maximum)


def _unix_time(value: object) -> datetime | None:
    if isinstance(value, str) and value.strip():
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if not isinstance(value, int | float):
        return None
    seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, UTC)
