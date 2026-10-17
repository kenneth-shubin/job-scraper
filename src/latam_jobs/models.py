"""Normalized job facts. Sources fill these fields; classification comes later."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

DESCRIPTION_LIMIT = 16_000


class RemoteStatus(StrEnum):
    REMOTE = "remote"
    HYBRID = "hybrid"
    ONSITE = "onsite"
    UNKNOWN = "unknown"


class LatamEligibility(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    MAYBE = "maybe"


class KeywordStatus(StrEnum):
    MATCHED = "matched"
    EXCLUDED = "excluded"


class ApplyUrlType(StrEnum):
    DIRECT = "direct"
    JOB_BOARD = "job_board"
    UNKNOWN = "unknown"


@dataclass
class Job:
    source: str
    source_job_id: str
    title: str
    company: str
    url: str
    description: str
    location_text: str
    tags: tuple[str, ...] = ()
    location_countries: tuple[str, ...] = ()
    salary: str | None = None
    published_at: datetime | None = None
    apply_url: str | None = None
    apply_url_type: ApplyUrlType = ApplyUrlType.JOB_BOARD
    remote_status: RemoteStatus = RemoteStatus.UNKNOWN
    latam_eligibility: LatamEligibility | None = None
    eligibility_reason: str = ""
    keyword_status: KeywordStatus | None = None
    keyword_reason: str = ""
    extras: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.source or not self.source_job_id:
            raise ValueError("source and source_job_id are required")
        if not self.title or not self.url:
            raise ValueError("title and url are required")
        if self.published_at is not None and self.published_at.tzinfo is None:
            raise ValueError("published_at must be timezone-aware UTC")
        self.description = self.description[:DESCRIPTION_LIMIT]
        self.tags = tuple(self.tags)
        self.location_countries = tuple(self.location_countries)
        self.remote_status = RemoteStatus(self.remote_status)
        self.apply_url_type = ApplyUrlType(self.apply_url_type)
