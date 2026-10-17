"""Greenhouse public job board API.

Verified endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs
Board tokens come from the local catalog, settings, and links found on the
other sources. The list call omits job content. A description is requested
only when the title matches a keyword and the later of `updated_at` and
`first_published` is inside 4 days. A board that returns 404 is skipped. The
source fails only when every requested board fails.
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


class GreenhouseSource:
    name = "greenhouse"
    default_interval_hours = 24
    default_enabled = True
    default_url = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    default_feeds = ()
    default_max_pages = 1

    def fetch(self, ctx: FetchContext) -> FetchResult:
        boards = targets("greenhouse", ctx)
        if not boards:
            return FetchResult(jobs=[])
        now = ctx.now or utc_now()
        pacer = Pacer(ctx.sleeper)
        jobs: list[Job] = []
        skipped = 0
        failures = 0
        status_code = None
        for board, url in boards:
            pacer.wait()
            try:
                response = ctx.client.get(url)
            except Exception as exc:
                failures += 1
                logger.warning("greenhouse board %s failed: %s", board, exc)
                continue
            status_code = response.status_code
            try:
                payload = response.json()
            except Exception as exc:
                failures += 1
                logger.warning("greenhouse board %s was not JSON: %s", board, exc)
                continue
            if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
                failures += 1
                logger.warning("greenhouse board %s has no jobs list", board)
                continue
            for item in payload["jobs"]:
                if not isinstance(item, dict) or item.get("id") in (None, ""):
                    skipped += 1
                    continue
                if not _listed_recently(item, now, ctx.search_terms):
                    continue
                pacer.wait()
                try:
                    detail = ctx.client.get(f"{url.rstrip('/')}/{item['id']}")
                    document = _detail_job(detail.json(), item["id"]) or item
                except Exception as exc:
                    skipped += 1
                    logger.warning("greenhouse job %s/%s failed: %s", board, item.get("id"), exc)
                    continue
                parsed = _parse_job(board, document)
                if parsed is None:
                    skipped += 1
                    continue
                jobs.append(parsed)
        if failures and failures == len(boards):
            raise SourceError("Greenhouse boards failed")
        if skipped:
            logger.info("greenhouse skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _parse_job(board: str, item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("title")
    listing = item.get("absolute_url")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(listing, str) or not listing.strip():
        return None
    company = item.get("company_name")
    location = item.get("location")
    location_name = location.get("name") if isinstance(location, dict) else ""
    description = item.get("content")
    apply_url, apply_url_type = listing_links(listing.strip(), None, career=True)
    remote = isinstance(location_name, str) and "remote" in location_name.casefold()
    return Job(
        source="greenhouse",
        source_job_id=f"{board}:{source_id}",
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else board,
        url=listing.strip(),
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        location_text=location_name.strip() if isinstance(location_name, str) else "",
        published_at=_later(item.get("updated_at"), item.get("first_published")),
        remote_status=RemoteStatus.REMOTE if remote else RemoteStatus.UNKNOWN,
    )


def _listed_recently(item: dict, now: datetime, keywords: tuple[str, ...]) -> bool:
    title = item.get("title")
    if not isinstance(title, str) or not title_matches(title, keywords):
        return False
    published = _later(item.get("updated_at"), item.get("first_published"))
    return published is not None and published >= now - timedelta(days=4)


def _detail_job(payload: object, job_id: object) -> dict | None:
    if not isinstance(payload, dict):
        return None
    jobs = payload.get("jobs")
    if isinstance(jobs, list):
        for item in jobs:
            if isinstance(item, dict) and str(item.get("id")) == str(job_id):
                return item
        return None
    if payload.get("id") is not None or isinstance(payload.get("content"), str):
        return payload
    return None


def _later(updated: object, first_published: object) -> datetime | None:
    dates = [item for item in (_iso(updated), _iso(first_published)) if item is not None]
    if not dates:
        return None
    return max(dates)


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
