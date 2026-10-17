"""Lever public postings API.

Verified endpoint: GET https://api.lever.co/v0/postings/{site}?mode=json
Site tokens come from the local catalog. An EU row uses api.eu.lever.co.
`hostedUrl` is the company's Lever job page. `applyUrl` stays on Lever, so it
is not a separate direct link. A site that returns 404 is skipped. Only a job
posted inside 4 days whose title matches a keyword is returned.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from latam_jobs.ats_directory import Pacer, targets
from latam_jobs.filters import title_matches
from latam_jobs.models import Job, RemoteStatus
from latam_jobs.scheduler import utc_now
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text
from latam_jobs.urls import listing_links

logger = logging.getLogger(__name__)

_WORKPLACE = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on-site": RemoteStatus.ONSITE,
}


class LeverSource:
    name = "lever"
    default_interval_hours = 24
    default_enabled = True
    default_url = "https://api.lever.co/v0/postings/{site}"
    default_feeds = ()
    default_max_pages = 1

    def fetch(self, ctx: FetchContext) -> FetchResult:
        sites = targets("lever", ctx)
        if not sites:
            return FetchResult(jobs=[])
        now = ctx.now or utc_now()
        pacer = Pacer(ctx.sleeper)
        jobs: list[Job] = []
        skipped = 0
        failures = 0
        status_code = None
        for site, url in sites:
            pacer.wait()
            try:
                response = ctx.client.get(url)
            except Exception as exc:
                failures += 1
                logger.warning("lever site %s failed: %s", site, exc)
                continue
            status_code = response.status_code
            try:
                payload = response.json()
            except Exception as exc:
                failures += 1
                logger.warning("lever site %s was not JSON: %s", site, exc)
                continue
            if not isinstance(payload, list):
                failures += 1
                logger.warning("lever site %s is not a list", site)
                continue
            for item in payload:
                parsed = _parse_job(site, item)
                if parsed is None:
                    skipped += 1
                    continue
                if not _current(parsed, now, ctx.search_terms):
                    continue
                jobs.append(parsed)
        if failures and failures == len(sites):
            raise SourceError("Lever sites failed")
        if skipped:
            logger.info("lever skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _parse_job(site: str, item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("text")
    listing = item.get("hostedUrl")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(listing, str) or not listing.strip():
        return None
    categories = item.get("categories")
    location = categories.get("location") if isinstance(categories, dict) else ""
    description = item.get("descriptionPlain") or item.get("description")
    candidate = item.get("applyUrl") if isinstance(item.get("applyUrl"), str) else None
    apply_url, apply_url_type = listing_links(listing.strip(), candidate, career=True)
    workplace = item.get("workplaceType")
    if isinstance(workplace, str):
        remote_status = _WORKPLACE.get(workplace.casefold(), RemoteStatus.UNKNOWN)
    else:
        remote_status = RemoteStatus.UNKNOWN
    return Job(
        source="lever",
        source_job_id=f"{site}:{source_id}",
        title=title.strip(),
        company=site,
        url=listing.strip(),
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        location_text=location.strip() if isinstance(location, str) else "",
        published_at=_millis(item.get("createdAt")),
        remote_status=remote_status,
    )


def _current(job: Job, now: datetime, keywords: tuple[str, ...]) -> bool:
    if job.published_at is None or job.published_at < now - timedelta(days=4):
        return False
    return title_matches(job.title, keywords)


def _millis(value: object) -> datetime | None:
    if not isinstance(value, int | float):
        return None
    seconds = float(value) / 1000 if value > 10_000_000_000 else float(value)
    return datetime.fromtimestamp(seconds, UTC)
