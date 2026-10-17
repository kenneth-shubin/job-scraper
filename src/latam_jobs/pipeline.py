"""Fetch enabled sources and print the jobs they return."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from latam_jobs.eligibility import classify_eligibility, resolve_remote_status
from latam_jobs.filters import match_keywords
from latam_jobs.http import HttpClient
from latam_jobs.models import Job, KeywordStatus
from latam_jobs.settings import ConfigError, Settings, validate_registered_sources
from latam_jobs.sources import SOURCES
from latam_jobs.sources.base import FetchContext, JobSource, SourceSnapshot

logger = logging.getLogger(__name__)


@dataclass
class SourceRun:
    name: str
    jobs: list[Job] = field(default_factory=list)
    error: str | None = None
    not_modified: bool = False
    skipped_items: int = 0


def collect(
    settings: Settings,
    *,
    only_source: str | None = None,
    registry: dict[str, JobSource] | None = None,
    client: HttpClient | None = None,
) -> list[SourceRun]:
    adapters = registry if registry is not None else SOURCES
    selected = _select_sources(settings, adapters, only_source)
    owns_client = client is None
    http = client or HttpClient(
        timeout=settings.http.timeout_seconds,
        max_retries=settings.http.max_retries,
    )
    runs: list[SourceRun] = []
    try:
        for name, adapter in selected:
            source_settings = settings.sources.get(name)
            if source_settings is None:
                continue
            try:
                result = adapter.fetch(
                    FetchContext(
                        client=http,
                        settings=source_settings,
                        state=SourceSnapshot(),
                    )
                )
            except Exception as exc:
                logger.exception("source %s failed", name)
                runs.append(SourceRun(name=name, error=str(exc)))
                continue
            runs.append(
                SourceRun(
                    name=name,
                    jobs=[
                        job
                        for job in (annotate(item, settings) for item in result.jobs)
                        if _keep(job)
                    ],
                    not_modified=result.not_modified,
                    skipped_items=result.skipped_items,
                )
            )
    finally:
        if owns_client:
            http.close()
    return runs


def annotate(job: Job, settings: Settings) -> Job:
    job.remote_status = resolve_remote_status(job)
    keyword_status, keyword_reason = match_keywords(job, settings)
    job.keyword_status = keyword_status
    job.keyword_reason = keyword_reason
    if keyword_status is not KeywordStatus.MATCHED:
        return job
    eligibility, reason = classify_eligibility(job, settings)
    job.latam_eligibility = eligibility
    job.eligibility_reason = reason
    return job


def format_job(job: Job) -> str:
    salary = f"\nSalary: {job.salary}" if job.salary else ""
    location = job.location_text or "Location not provided"
    eligibility = job.latam_eligibility.value if job.latam_eligibility else "unclassified"
    return (
        f"[{job.source}] {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {location}{salary}\n"
        f"Eligibility: {eligibility}\n"
        f"Reason: {job.eligibility_reason}\n"
        f"Keywords: {job.keyword_reason}\n"
        f"Apply: {job.url}"
    )


def _keep(job: Job) -> bool:
    return job.keyword_status is KeywordStatus.MATCHED


def _select_sources(
    settings: Settings,
    registry: dict[str, JobSource],
    only_source: str | None,
) -> list[tuple[str, JobSource]]:
    if only_source is not None:
        source_settings = settings.sources.get(only_source)
        if source_settings is None or not source_settings.enabled:
            raise ConfigError(f"Source is not enabled in settings: {only_source}")
        validate_registered_sources(
            settings.model_copy(update={"sources": {only_source: source_settings}}),
            registry,
        )
        return [(only_source, registry[only_source])]
    validate_registered_sources(settings, registry)
    selected: list[tuple[str, JobSource]] = []
    for name, adapter in registry.items():
        source_settings = settings.sources.get(name)
        if source_settings is None or not source_settings.enabled:
            continue
        selected.append((name, adapter))
    return selected
