"""Shared source contract. Parsing stays inside each adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from latam_jobs.models import Job
from latam_jobs.settings import SourceSettings


@dataclass
class SourceSnapshot:
    """Conditional-request headers saved from the previous successful fetch."""

    etag: str | None = None
    last_modified: str | None = None


class HttpResponse(Protocol):
    status_code: int
    text: str
    headers: dict[str, str]

    def json(self) -> object: ...


class HttpClient(Protocol):
    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> HttpResponse: ...


@dataclass
class FetchContext:
    client: HttpClient
    settings: SourceSettings
    state: SourceSnapshot = field(default_factory=SourceSnapshot)
    search_terms: tuple[str, ...] = ()
    remote_only: bool = True


@dataclass
class FetchResult:
    jobs: list[Job] = field(default_factory=list)
    not_modified: bool = False
    etag: str | None = None
    last_modified: str | None = None
    status_code: int | None = None
    skipped_items: int = 0


class SourceError(Exception):
    """A source failed before any jobs could be parsed."""


class JobSource(Protocol):
    name: str
    default_interval_hours: int
    default_enabled: bool

    def fetch(self, ctx: FetchContext) -> FetchResult: ...
