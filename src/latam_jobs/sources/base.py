"""Shared source contract. Parsing stays inside each adapter."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from latam_jobs.ats_directory import CatalogBoard

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
        timeout: float | None = None,
    ) -> HttpResponse: ...


@dataclass
class FetchContext:
    client: HttpClient
    settings: SourceSettings
    state: SourceSnapshot = field(default_factory=SourceSnapshot)
    search_terms: tuple[str, ...] = ()
    remote_only: bool = True
    catalog: tuple[CatalogBoard, ...] = ()
    now: datetime | None = None
    sleeper: Callable[[float], None] = time.sleep
    pause_seconds: float = 0.25
    pause_jitter_seconds: float = 0.0


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
