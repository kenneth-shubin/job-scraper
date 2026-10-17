"""Arbeitnow public job-board API.

Verified endpoint: GET https://www.arbeitnow.com/api/job-board-api
The API needs no key. Each page links to the next page. The job URL in the
payload is an Arbeitnow listing, not a company career page.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.models import Job, RemoteStatus
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text
from latam_jobs.urls import listing_links

logger = logging.getLogger(__name__)


class ArbeitnowSource:
    name = "arbeitnow"
    default_interval_hours = 12
    default_enabled = True
    default_url = "https://www.arbeitnow.com/api/job-board-api"
    default_max_pages = 1

    def fetch(self, ctx: FetchContext) -> FetchResult:
        url: str | None = ctx.settings.url or self.default_url
        jobs: list[Job] = []
        seen: set[str] = set()
        skipped = 0
        status_code = None
        for _page in range(ctx.settings.max_pages):
            if not url:
                break
            response = ctx.client.get(url)
            status_code = response.status_code
            try:
                payload = response.json()
            except Exception as exc:
                raise SourceError("Arbeitnow returned a document that is not JSON") from exc
            if not isinstance(payload, dict):
                raise SourceError("Arbeitnow document is not an object")
            batch = payload.get("data")
            if not isinstance(batch, list):
                raise SourceError("Arbeitnow document is missing a data list")
            for item in batch:
                parsed = _parse_job(item)
                if parsed is None:
                    skipped += 1
                    continue
                if parsed.source_job_id in seen:
                    continue
                seen.add(parsed.source_job_id)
                jobs.append(parsed)
            links = payload.get("links")
            nxt = links.get("next") if isinstance(links, dict) else None
            url = nxt if isinstance(nxt, str) and nxt.strip() else None
        if skipped:
            logger.info("arbeitnow skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    slug = item.get("slug")
    title = item.get("title")
    listing = item.get("url")
    if not isinstance(slug, str) or not slug.strip():
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(listing, str) or not listing.strip():
        return None
    company = item.get("company_name")
    description = item.get("description")
    location = item.get("location")
    apply_url, apply_url_type = listing_links(listing.strip(), None, career=False)
    remote = item.get("remote") is True
    return Job(
        source="arbeitnow",
        source_job_id=slug.strip(),
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else "Unknown",
        url=listing.strip(),
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        tags=_tags(item.get("tags")),
        location_text=location.strip() if isinstance(location, str) else "",
        published_at=_unix(item.get("created_at")),
        remote_status=RemoteStatus.REMOTE if remote else RemoteStatus.ONSITE,
    )


def _tags(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _unix(value: object) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, UTC)
