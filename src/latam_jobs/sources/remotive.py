"""Remotive public jobs API.

Verified endpoint: GET https://remotive.com/api/remote-jobs
Documentation: https://github.com/remotive-com/remote-jobs-api
Access: no API key. Remotive asks for at most four requests per day and blocks
more than two per minute. Jobs are delayed by 24 hours, so a fresh run can
legitimately store nothing under the 24-hour publication window. The verified
payload has no separate application URL; `url` is the Remotive listing.
Messages that display these jobs must name Remotive and use the Remotive URL.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.models import Job
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text

logger = logging.getLogger(__name__)


class RemotiveSource:
    name = "remotive"
    default_interval_hours = 6
    default_enabled = True
    default_url = "https://remotive.com/api/remote-jobs"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        url = ctx.settings.url or self.default_url
        headers: dict[str, str] = {}
        if ctx.state.etag:
            headers["If-None-Match"] = ctx.state.etag
        if ctx.state.last_modified:
            headers["If-Modified-Since"] = ctx.state.last_modified
        response = ctx.client.get(url, params={"category": "software-dev"}, headers=headers)
        etag = response.headers.get("etag")
        last_modified = response.headers.get("last-modified")
        if response.status_code == 304:
            return FetchResult(
                not_modified=True,
                etag=etag or ctx.state.etag,
                last_modified=last_modified or ctx.state.last_modified,
                status_code=304,
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise SourceError("Remotive returned a document that is not JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
            raise SourceError("Remotive document is missing a jobs list")
        jobs: list[Job] = []
        skipped = 0
        for item in payload["jobs"]:
            parsed = _parse_job(item)
            if parsed is None:
                skipped += 1
                continue
            jobs.append(parsed)
        if skipped:
            logger.info("remotive skipped %s malformed jobs", skipped)
        return FetchResult(
            jobs=jobs,
            etag=etag,
            last_modified=last_modified,
            status_code=response.status_code,
            skipped_items=skipped,
        )


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("title")
    url = item.get("url")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(url, str) or not url.strip():
        return None
    company = item.get("company_name")
    company_name = company.strip() if isinstance(company, str) and company.strip() else "Unknown"
    description = item.get("description")
    description_text = html_to_text(description) if isinstance(description, str) else ""
    location = item.get("candidate_required_location")
    location_text = location.strip() if isinstance(location, str) else ""
    salary = item.get("salary")
    salary_text = salary.strip() if isinstance(salary, str) and salary.strip() else None
    tags = _tags(item.get("tags"), item.get("category"))
    return Job(
        source="remotive",
        source_job_id=str(source_id),
        title=title.strip(),
        company=company_name,
        url=url.strip(),
        description=description_text,
        tags=tags,
        location_text=location_text,
        salary=salary_text,
        published_at=_published_at(item.get("publication_date")),
    )


def _tags(raw_tags: object, category: object) -> tuple[str, ...]:
    tags: list[str] = []
    if isinstance(category, str) and category.strip():
        tags.append(category.strip())
    if isinstance(raw_tags, list):
        for tag in raw_tags:
            if isinstance(tag, str) and tag.strip():
                tags.append(tag.strip())
    return tuple(dict.fromkeys(tags))


def _published_at(value: object) -> datetime | None:
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
