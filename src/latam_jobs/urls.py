"""Canonical application URLs for cross-source duplicate detection."""

from __future__ import annotations

import hashlib
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

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


def _is_tracking(key: str) -> bool:
    lowered = key.lower()
    return lowered.startswith("utm_") or lowered in _TRACKING_KEYS
