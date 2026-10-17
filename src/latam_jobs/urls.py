"""Canonical application URLs for cross-source duplicate detection."""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from latam_jobs.models import ApplyUrlType, Job

_TRACKING_KEYS = frozenset(
    {"ref", "gh_src", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "yclid"}
)
_URLS = re.compile(r"https?://[^\s<>\"')\]]+")


def canonical_url(url: str) -> str:
    parts = urlsplit(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = parts.netloc.lower()
    path = parts.path.rstrip("/")
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if not _is_tracking(key)
    ]
    query.sort()
    return urlunsplit((scheme, host, path, urlencode(query), ""))


def content_hash(title: str, company: str, canonical: str) -> str:
    payload = "\n".join((title.casefold().strip(), company.casefold().strip(), canonical))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def classify_apply_link(listing_url: str, candidate: str | None) -> tuple[str | None, ApplyUrlType]:
    """Use a structured apply URL only when it is absolute http(s) on another host.

    A recognized ATS domain is not enough. The source has to provide the URL.
    The same host, a missing value, or an invalid value stays a job-board listing.
    """
    if not isinstance(candidate, str) or not candidate.strip():
        return None, ApplyUrlType.JOB_BOARD
    parts = urlsplit(candidate.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None, ApplyUrlType.JOB_BOARD
    listing = urlsplit(listing_url.strip())
    candidate_host = parts.hostname.casefold().removeprefix("www.")
    listing_host = (listing.hostname or "").casefold().removeprefix("www.")
    if not listing_host or candidate_host == listing_host:
        return None, ApplyUrlType.JOB_BOARD
    return candidate.strip(), ApplyUrlType.DIRECT


def direct_ats_url(*texts: str) -> str | None:
    """Return one Greenhouse, Lever, or Ashby URL that identifies a single opening."""
    for text in texts:
        if not text:
            continue
        for raw in _URLS.findall(text):
            opening = _ats_opening(raw.rstrip(".,;"))
            if opening is not None:
                return opening
    return None


def attach_direct_apply(job: Job) -> None:
    """Point an aggregator row at a verified ATS opening when the listing includes one."""
    found = direct_ats_url(job.apply_url or "", job.description, job.url)
    if found is None or canonical_url(found) == canonical_url(job.url):
        return
    job.apply_url = found
    job.apply_url_type = ApplyUrlType.DIRECT


def listing_links(
    listing_url: str, apply_candidate: str | None, *, career: bool
) -> tuple[str | None, ApplyUrlType]:
    """Keep a direct apply link, or mark a company career page.

    Aggregator listings stay job-board links. Those rows do not claim a
    separate original posting.
    """
    apply_url, kind = classify_apply_link(listing_url, apply_candidate)
    if kind is ApplyUrlType.DIRECT:
        return apply_url, kind
    if career:
        return None, ApplyUrlType.CAREER
    return None, ApplyUrlType.JOB_BOARD


def _ats_opening(url: str) -> str | None:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
        return None
    host = parts.hostname.casefold().removeprefix("www.")
    path = [piece for piece in parts.path.split("/") if piece]
    query = dict(parse_qsl(parts.query, keep_blank_values=False))
    if host.endswith(".greenhouse.io") and _greenhouse_opening(path, query):
        return url.strip()
    if host == "jobs.lever.co" and _lever_opening(path):
        return url.strip()
    if host == "jobs.ashbyhq.com" and _ashby_opening(path):
        return url.strip()
    return None


def _greenhouse_opening(path: list[str], query: dict[str, str]) -> bool:
    if len(path) >= 3 and path[1].casefold() == "jobs" and path[2].isdigit():
        return True
    token = query.get("token", "").strip()
    board = query.get("for", "").strip()
    return bool(token and board)


def _lever_opening(path: list[str]) -> bool:
    return len(path) >= 2 and path[1].casefold() not in {"apply", "application"}


def _ashby_opening(path: list[str]) -> bool:
    return len(path) >= 2 and path[1].casefold() not in {"application", "apply"}


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_KEYS
