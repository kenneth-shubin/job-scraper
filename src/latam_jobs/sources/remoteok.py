"""Remote OK public JSON feed.

Verified endpoint: GET https://remoteok.com/api
Official help documents the feed, and a request with the honest latam-jobs
User-Agent returned HTTP 200. The first array element is a legal notice.
Messages must name Remote OK and link to the posting. The source stays disabled
in settings until it is turned on. This adapter does not impersonate a browser.
`apply_url` is stored as a direct link only when its host differs from `url`.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.models import Job
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text
from latam_jobs.urls import classify_apply_link

logger = logging.getLogger(__name__)


class RemoteOkSource:
    name = "remoteok"
    default_interval_hours = 6
    default_enabled = False
    default_url = "https://remoteok.com/api"

    def fetch(self, ctx: FetchContext) -> FetchResult:
        url = ctx.settings.url or self.default_url
        headers: dict[str, str] = {}
        if ctx.state.etag:
            headers["If-None-Match"] = ctx.state.etag
        if ctx.state.last_modified:
            headers["If-Modified-Since"] = ctx.state.last_modified
        response = ctx.client.get(url, headers=headers)
        if response.status_code == 304:
            return FetchResult(
                not_modified=True,
                etag=response.headers.get("etag") or ctx.state.etag,
                last_modified=response.headers.get("last-modified") or ctx.state.last_modified,
                status_code=304,
            )
        try:
            payload = response.json()
        except Exception as exc:
            raise SourceError("Remote OK returned a document that is not JSON") from exc
        if not isinstance(payload, list):
            raise SourceError("Remote OK document is not a list")
        jobs: list[Job] = []
        skipped = 0
        for item in payload:
            if _is_legal_notice(item):
                continue
            parsed = _parse_job(item)
            if parsed is None:
                skipped += 1
                continue
            jobs.append(parsed)
        if skipped:
            logger.info("remoteok skipped %s malformed jobs", skipped)
        return FetchResult(
            jobs=jobs,
            etag=response.headers.get("etag"),
            last_modified=response.headers.get("last-modified"),
            status_code=response.status_code,
            skipped_items=skipped,
        )


def _is_legal_notice(item: object) -> bool:
    return (
        isinstance(item, dict)
        and "position" not in item
        and ("legal" in item or "last_updated" in item)
    )


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("position")
    listing = item.get("url") if isinstance(item.get("url"), str) else ""
    candidate = item.get("apply_url") if isinstance(item.get("apply_url"), str) else None
    url = listing.strip() or (candidate.strip() if isinstance(candidate, str) else "")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not url:
        return None
    apply_url, apply_url_type = classify_apply_link(url, candidate)
    company = item.get("company")
    description = item.get("description")
    location = item.get("location")
    tags = item.get("tags")
    return Job(
        source="remoteok",
        source_job_id=str(source_id),
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else "Unknown",
        url=url,
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        tags=tuple(tag.strip() for tag in tags if isinstance(tag, str) and tag.strip())
        if isinstance(tags, list)
        else (),
        location_text=location.strip() if isinstance(location, str) else "",
        salary=_salary(item),
        published_at=_published(item.get("date")),
    )


def _salary(item: dict[str, object]) -> str | None:
    minimum = item.get("salary_min")
    maximum = item.get("salary_max")
    if minimum in (None, "", 0) and maximum in (None, "", 0):
        return None
    if minimum not in (None, "", 0) and maximum not in (None, "", 0):
        return f"{minimum}-{maximum}"
    value = minimum or maximum
    return None if value in (None, "", 0) else str(value)


def _published(value: object) -> datetime | None:
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
