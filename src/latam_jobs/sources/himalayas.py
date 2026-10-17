"""Himalayas public jobs API.

Verified endpoint: GET https://himalayas.app/jobs/api/search
Documentation: https://himalayas.app/docs/remote-jobs-api
The feed refreshes every 24 hours. Page size is 20. Each keyword is sent as q.
This adapter requests at most max_pages recent pages per keyword and does not
walk the full catalog. The verified search
payload's applicationLink is a Himalayas page, not a company or ATS URL, so no
direct apply link is stored. This adapter does not request job detail pages.
Messages must credit Himalayas and use the Himalayas URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.geo import country_slug
from latam_jobs.locations import country_list_label
from latam_jobs.models import Job
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text

logger = logging.getLogger(__name__)

_EXTRA_SLUGS = {
    "united states": "united-states",
    "united states of america": "united-states",
    "usa": "united-states",
    "us": "united-states",
    "united kingdom": "united-kingdom",
    "uk": "united-kingdom",
    "canada": "canada",
}


class HimalayasSource:
    name = "himalayas"
    default_interval_hours = 24
    default_enabled = True
    default_url = "https://himalayas.app/jobs/api/search"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        url = ctx.settings.url or self.default_url
        jobs: list[Job] = []
        seen: set[str] = set()
        skipped = 0
        etag = ctx.state.etag
        last_modified = ctx.state.last_modified
        status_code = None
        queries = _search_terms(ctx.search_terms) or (None,)
        for query in queries:
            for page in range(1, ctx.settings.max_pages + 1):
                headers: dict[str, str] = {}
                if page == 1 and query is None and ctx.state.etag:
                    headers["If-None-Match"] = ctx.state.etag
                if page == 1 and query is None and ctx.state.last_modified:
                    headers["If-Modified-Since"] = ctx.state.last_modified
                api_key = _api_key()
                if api_key:
                    headers["Authorization"] = f"Bearer {api_key}"
                params = {"sort": "recent", "page": str(page)}
                if query:
                    params["q"] = query
                response = ctx.client.get(url, params=params, headers=headers)
                status_code = response.status_code
                etag = response.headers.get("etag") or etag
                last_modified = response.headers.get("last-modified") or last_modified
                if response.status_code == 304:
                    return FetchResult(
                        not_modified=True,
                        etag=etag,
                        last_modified=last_modified,
                        status_code=304,
                    )
                payload = _payload(response.json())
                batch = payload.get("jobs")
                if not isinstance(batch, list):
                    raise SourceError("Himalayas document is missing a jobs list")
                if not batch:
                    break
                added = 0
                for item in batch:
                    parsed = _parse_job(item)
                    if parsed is None:
                        skipped += 1
                        continue
                    if parsed.source_job_id in seen:
                        continue
                    seen.add(parsed.source_job_id)
                    jobs.append(parsed)
                    added += 1
                if added == 0:
                    break
        if skipped:
            logger.info("himalayas skipped %s malformed jobs", skipped)
        return FetchResult(
            jobs=jobs,
            etag=etag,
            last_modified=last_modified,
            status_code=status_code,
            skipped_items=skipped,
        )


def _search_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _api_key() -> str:
    import os

    return os.environ.get("HIMALAYAS_API_KEY", "").strip()


def _payload(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        raise SourceError("Himalayas returned a document that is not an object")
    return value


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    title = item.get("title")
    url = item.get("applicationLink")
    guid = item.get("guid")
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    if not isinstance(guid, str) or not guid.strip():
        return None
    company = item.get("companyName")
    company_name = company.strip() if isinstance(company, str) and company.strip() else "Unknown"
    description = item.get("description")
    location_text, countries = _location(item.get("locationRestrictions"))
    tags = _strings(item.get("categories")) + _strings(item.get("parentCategories"))
    return Job(
        source="himalayas",
        source_job_id=guid.strip(),
        title=title.strip(),
        company=company_name,
        url=url.strip(),
        description=html_to_text(description) if isinstance(description, str) else "",
        tags=tuple(dict.fromkeys(tags)),
        location_text=location_text,
        location_countries=countries,
        salary=_salary(item),
        published_at=_unix_time(item.get("pubDate")),
    )


def _location(raw: object) -> tuple[str, tuple[str, ...]]:
    if not isinstance(raw, list) or not raw:
        return "Worldwide", ()
    names: list[str] = []
    slugs: list[str] = []
    for entry in raw:
        name = _restriction_name(entry)
        if not name:
            continue
        names.append(name)
        slugs.append(_slug(name))
    if not names:
        return "Worldwide", ()
    return country_list_label(names), tuple(dict.fromkeys(slugs))


def _restriction_name(entry: object) -> str:
    if isinstance(entry, str):
        return entry.strip()
    if isinstance(entry, dict):
        for key in ("name", "slug", "alpha2"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _slug(name: str) -> str:
    known = country_slug(name)
    if known:
        return known
    folded = " ".join(name.casefold().split())
    if folded in _EXTRA_SLUGS:
        return _EXTRA_SLUGS[folded]
    return folded.replace(" ", "-")


def _salary(item: dict[str, object]) -> str | None:
    minimum = item.get("minSalary")
    maximum = item.get("maxSalary")
    if minimum in (None, "") and maximum in (None, ""):
        return None
    currency = item.get("currency") if isinstance(item.get("currency"), str) else ""
    period = item.get("salaryPeriod") if isinstance(item.get("salaryPeriod"), str) else ""
    amount = (
        f"{minimum}-{maximum}"
        if minimum not in (None, "") and maximum not in (None, "")
        else str(minimum or maximum)
    )
    return " ".join(part for part in (currency, amount, period) if part).strip() or None


def _strings(value: object) -> list[str]:
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def _unix_time(value: object) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, UTC)
