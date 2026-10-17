"""Canonical application URLs for cross-source duplicate detection."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from latam_jobs.models import ApplyUrlType

_TRACKING_KEYS = frozenset(
    {"ref", "gh_src", "fbclid", "gclid", "mc_cid", "mc_eid", "igshid", "yclid"}
)


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


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_KEYS
