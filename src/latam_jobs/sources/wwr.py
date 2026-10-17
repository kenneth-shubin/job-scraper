"""We Work Remotely public RSS feeds.

Verified page: https://weworkremotely.com/remote-job-rss-feed
The site allows the public RSS feed when links attribute We Work Remotely.
This adapter reads the programming and DevOps feeds. It does not scrape HTML pages.
The RSS item link is the We Work Remotely listing. Descriptions are not scanned
for application links.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from time import struct_time

import feedparser

from latam_jobs.models import Job
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text

logger = logging.getLogger(__name__)


class WwrSource:
    name = "wwr"
    default_interval_hours = 12
    default_enabled = True
    default_url = "https://weworkremotely.com/categories/remote-programming-jobs.rss"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        feeds = ctx.settings.feeds or (
            [ctx.settings.url] if ctx.settings.url else [self.default_url]
        )
        jobs: list[Job] = []
        seen: set[str] = set()
        skipped = 0
        status_code = None
        for feed_url in feeds:
            response = ctx.client.get(feed_url)
            status_code = response.status_code
            if response.status_code == 304:
                continue
            if "<rss" not in response.text.lower() and "<feed" not in response.text.lower():
                raise SourceError("We Work Remotely returned a document that is not a feed")
            parsed = feedparser.parse(response.text)
            for entry in parsed.entries:
                job = _parse_entry(entry)
                if job is None:
                    skipped += 1
                    continue
                if job.source_job_id in seen:
                    continue
                seen.add(job.source_job_id)
                jobs.append(job)
        if skipped:
            logger.info("wwr skipped %s malformed entries", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _parse_entry(entry: object) -> Job | None:
    title = _text(entry, "title")
    link = _text(entry, "link") or _text(entry, "id")
    if not title or not link:
        return None
    company, job_title = _split_title(title)
    description = _text(entry, "summary") or _text(entry, "description")
    region = _text(entry, "region")
    tags = [term for term in _categories(entry) if term]
    return Job(
        source="wwr",
        source_job_id=link,
        title=job_title,
        company=company,
        url=link,
        description=html_to_text(description),
        tags=tuple(dict.fromkeys(tags)),
        location_text=region,
        published_at=_published(entry),
    )


def _split_title(title: str) -> tuple[str, str]:
    if ": " in title:
        company, job_title = title.split(": ", 1)
        if company.strip() and job_title.strip():
            return company.strip(), job_title.strip()
    return "Unknown", title.strip()


def _categories(entry: object) -> list[str]:
    tags = getattr(entry, "tags", None)
    if not isinstance(tags, list):
        return []
    names: list[str] = []
    for tag in tags:
        term = getattr(tag, "term", None)
        if isinstance(term, str) and term.strip():
            names.append(term.strip())
    return names


def _published(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None)
    if not isinstance(parsed, struct_time):
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def _text(entry: object, name: str) -> str:
    value = entry.get(name) if hasattr(entry, "get") else getattr(entry, name, None)
    if isinstance(value, str):
        return value.strip()
    return ""
