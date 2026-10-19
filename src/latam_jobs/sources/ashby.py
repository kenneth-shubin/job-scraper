"""Ashby public job-board API.

Verified endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{board}
Board tokens come from the local catalog. `jobUrl` is the company's Ashby
posting. `applyUrl` stays on Ashby unless its host differs. A missing board is
skipped. Only a job posted inside 4 days whose title matches a keyword is
returned. The source fails only when every requested board fails.
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

# Ashby board responses often take 10 to 25 seconds.
ASHBY_TIMEOUT_SECONDS = 30.0

_WORKPLACE = {
    "remote": RemoteStatus.REMOTE,
    "hybrid": RemoteStatus.HYBRID,
    "onsite": RemoteStatus.ONSITE,
    "on-site": RemoteStatus.ONSITE,
}


class AshbySource:
    name = "ashby"
    default_interval_hours = 6
    default_enabled = True
    default_url = "https://api.ashbyhq.com/posting-api/job-board/{board}"
    default_feeds = ()
    default_max_pages = 1

    def fetch(self, ctx: FetchContext) -> FetchResult:
        boards = targets("ashby", ctx)
        if not boards:
            return FetchResult(jobs=[])
        now = ctx.now or utc_now()
        pacer = Pacer.for_context(ctx)
        jobs: list[Job] = []
        skipped = 0
        failures = 0
        status_code = None
        for board, url in boards:
            if pacer.exhausted:
                logger.warning("ashby is rate limiting; stopping before ashby board %s", board)
                break
            pacer.wait()
            try:
                response = ctx.client.get(url, timeout=ASHBY_TIMEOUT_SECONDS)
            except Exception as exc:
                pacer.record(exc)
                failures += 1
                logger.warning("ashby board %s failed: %s", board, exc)
                continue
            pacer.record(None)
            status_code = response.status_code
            try:
                payload = response.json()
            except Exception as exc:
                failures += 1
                logger.warning("ashby board %s was not JSON: %s", board, exc)
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                failures += 1
                logger.warning("ashby board %s has no jobs list", board)
                continue
            for item in payload["jobs"]:
                parsed = _parse_job(board, item)
                if parsed is None:
                    skipped += 1
                    continue
                if not _current(parsed, now, ctx.search_terms):
                    continue
                jobs.append(parsed)
        if failures and failures == len(boards):
            raise SourceError("Ashby boards failed")
        if skipped:
            logger.info("ashby skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _parse_job(board: str, item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("title")
    listing = item.get("jobUrl")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(listing, str) or not listing.strip():
        return None
    location = item.get("location")
    description = item.get("descriptionPlain") or item.get("descriptionHtml")
    candidate = item.get("applyUrl") if isinstance(item.get("applyUrl"), str) else None
    apply_url, apply_url_type = listing_links(listing.strip(), candidate, career=True)
    workplace = item.get("workplaceType")
    if item.get("isRemote") is True:
        remote_status = RemoteStatus.REMOTE
    elif isinstance(workplace, str):
        remote_status = _WORKPLACE.get(workplace.casefold(), RemoteStatus.UNKNOWN)
    else:
        remote_status = RemoteStatus.UNKNOWN
    return Job(
        source="ashby",
        source_job_id=f"{board}:{source_id}",
        title=title.strip(),
        company=board,
        url=listing.strip(),
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        location_text=location.strip() if isinstance(location, str) else "",
        published_at=_iso(item.get("publishedAt")),
        remote_status=remote_status,
    )


def _current(job: Job, now: datetime, keywords: tuple[str, ...]) -> bool:
    if job.published_at is None or job.published_at < now - timedelta(days=4):
        return False
    return title_matches(job.title, keywords)


def _iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
