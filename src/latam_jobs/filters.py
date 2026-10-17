"""Technology keyword matching. Inclusion is OR. Any exclusion keyword rejects."""

from __future__ import annotations

import re

from latam_jobs.models import Job, KeywordStatus
from latam_jobs.settings import Settings


def match_keywords(job: Job, settings: Settings) -> tuple[KeywordStatus, str]:
    excluded = _first_hit(job, settings.exclude_keywords)
    if excluded is not None:
        keyword, field = excluded
        return KeywordStatus.EXCLUDED, f"excluded by {keyword!r} in {field}"
    if not settings.keywords:
        return KeywordStatus.MATCHED, "no inclusion keywords configured"
    matched = _first_hit(job, settings.keywords)
    if matched is not None:
        keyword, field = matched
        return KeywordStatus.MATCHED, f"matched {keyword!r} in {field}"
    return KeywordStatus.EXCLUDED, "no inclusion keyword matched"


def _first_hit(job: Job, keywords: list[str]) -> tuple[str, str] | None:
    for keyword in keywords:
        cleaned = " ".join(keyword.split())
        if not cleaned:
            continue
        for field, text in _search_fields(job, cleaned):
            if _pattern(cleaned).search(text):
                return cleaned, field
    return None


def title_matches(title: str, keywords: tuple[str, ...] | list[str]) -> bool:
    """True when no keywords are configured, or one keyword is a whole word in the title."""
    cleaned = [item.strip() for item in keywords if item.strip()]
    if not cleaned:
        return True
    for keyword in cleaned:
        if _pattern(keyword).search(title):
            return True
    return False


def _search_fields(job: Job, keyword: str) -> list[tuple[str, str]]:
    fields = [("title", job.title), ("tags", " ".join(job.tags))]
    if len(keyword) > 2:
        fields.append(("description", job.description))
    return fields


def _pattern(keyword: str) -> re.Pattern[str]:
    parts = [re.escape(part) for part in keyword.split()]
    return re.compile(rf"\b{r'\s+'.join(parts)}\b", re.IGNORECASE)
