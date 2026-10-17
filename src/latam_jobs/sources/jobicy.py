"""Jobicy public remote-jobs API.

Verified endpoint: GET https://jobicy.com/api/v2/remote-jobs
Each keyword is requested with geo=latam. The API needs no key. Jobicy asks users
of the feed to link back to the job URL in this response. That URL is the Jobicy
listing. A separate company apply
URL is stored only when the payload provides one on another host.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from latam_jobs.models import Job, RemoteStatus
from latam_jobs.sources.base import FetchContext, FetchResult, SourceError
from latam_jobs.text import html_to_text
from latam_jobs.urls import listing_links

logger = logging.getLogger(__name__)


class JobicySource:
    name = "jobicy"
    default_interval_hours = 12
    default_enabled = True
    default_url = "https://jobicy.com/api/v2/remote-jobs"
    default_max_pages = 2

    def fetch(self, ctx: FetchContext) -> FetchResult:
        url = ctx.settings.url or self.default_url
        jobs: list[Job] = []
        seen: set[str] = set()
        skipped = 0
        status_code = None
        terms = _search_terms(ctx.search_terms) or (None,)
        for term in terms:
            cursor: str | None = None
            for _page in range(ctx.settings.max_pages):
                params = {"count": "50", "geo": "latam"}
                if term:
                    params["tag"] = term
                if cursor:
                    params["cursor"] = cursor
                response = ctx.client.get(url, params=params)
                status_code = response.status_code
                try:
                    payload = response.json()
                except Exception as exc:
                    raise SourceError("Jobicy returned a document that is not JSON") from exc
                if not isinstance(payload, dict):
                    raise SourceError("Jobicy document is not an object")
                batch = payload.get("jobs")
                if not isinstance(batch, list):
                    raise SourceError("Jobicy document is missing a jobs list")
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
                next_cursor = payload.get("nextCursor")
                if (
                    payload.get("hasMore") is not True
                    or not isinstance(next_cursor, str)
                    or not next_cursor
                ):
                    break
                cursor = next_cursor
        if skipped:
            logger.info("jobicy skipped %s malformed jobs", skipped)
        return FetchResult(jobs=jobs, status_code=status_code, skipped_items=skipped)


def _search_terms(terms: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(term.strip() for term in terms if term.strip()))


def _parse_job(item: object) -> Job | None:
    if not isinstance(item, dict):
        return None
    source_id = item.get("id")
    title = item.get("jobTitle")
    listing = item.get("url")
    if source_id in (None, "") or not isinstance(title, str) or not title.strip():
        return None
    if not isinstance(listing, str) or not listing.strip():
        return None
    company = item.get("companyName")
    description = item.get("jobDescription")
    location = item.get("jobGeo")
    candidate = _first_text(item, ("applyUrl", "applicationUrl", "jobUrl"))
    apply_url, apply_url_type = listing_links(listing.strip(), candidate, career=False)
    return Job(
        source="jobicy",
        source_job_id=str(source_id),
        title=title.strip(),
        company=company.strip() if isinstance(company, str) and company.strip() else "Unknown",
        url=listing.strip(),
        apply_url=apply_url,
        apply_url_type=apply_url_type,
        description=html_to_text(description) if isinstance(description, str) else "",
        location_text=location.strip() if isinstance(location, str) else "",
        salary=_salary(item),
        published_at=_iso(item.get("pubDate")),
        remote_status=RemoteStatus.REMOTE,
    )


def _first_text(item: dict[str, object], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip() and value.strip() != str(item.get("url") or ""):
            return value
    return None


def _salary(item: dict[str, object]) -> str | None:
    minimum = item.get("salaryMin")
    maximum = item.get("salaryMax")
    if minimum in (None, "", 0) and maximum in (None, "", 0):
        return None
    currency = item.get("salaryCurrency") if isinstance(item.get("salaryCurrency"), str) else ""
    period = item.get("salaryPeriod") if isinstance(item.get("salaryPeriod"), str) else ""
    if minimum not in (None, "", 0) and maximum not in (None, "", 0):
        amount = f"{minimum}-{maximum}"
    else:
        amount = str(minimum or maximum)
    return " ".join(part for part in (currency, amount, period) if part).strip() or None


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
