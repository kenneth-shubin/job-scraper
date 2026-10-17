"""Greenhouse public job board API.

Verified endpoint: GET https://boards-api.greenhouse.io/v1/boards/{board}/jobs
Board tokens come from settings and from Greenhouse links found on the other
sources. `absolute_url` is that company's Greenhouse posting. A repost uses the
later of `updated_at` and `first_published`. A board that returns 404 is
skipped. The source fails only when every requested board fails.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.models import Job, RemoteStatus
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text
from latam_jobs.urls import listing_links

logger = logging.getLogger(__name__)


class GreenhouseSource:
    name = "greenhouse"
    default_interval_hours = 24
    default_enabled = True
    default_url = "https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    default_feeds = (
        "goodwaygroup",
        "gitlab",
        "cloudflare",
        "discord",
        "figma",
        "intercom",
        "airtable",
    )
    default_max_pages = 1

    def fetch(self, ctx: FetchContext) -> FetchResult:
        boards = _boards(ctx)
        if not boards:
            return FetchResult(jobs=[])
        template = ctx.settings.url or self.default_url
        jobs: list[Job] = []
        skipped = 0
        failures = 0
        status_code = None
        for board in boards:
            url = template.format(board=board)
            try:
                response = ctx.client.get(url, params={"content": "true"})
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
                parsed = _parse_job(board, item)
                if parsed is None:
                    skipped += 1
                    continue
                jobs.append(parsed)
        if failures and failures == len(boards):
            raise SourceError("Greenhouse boards failed")
        if skipped:
            logger.info("greenhouse skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _boards(ctx: FetchContext) -> list[str]:
    return [item.strip() for item in ctx.settings.feeds if item.strip()]


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
