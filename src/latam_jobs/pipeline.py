"""Fetch enabled sources, classify them, and store the jobs that match."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from random import Random

from latam_jobs.ats_directory import load_catalog
from latam_jobs.eligibility import classify_eligibility, resolve_remote_status
from latam_jobs.filters import match_keywords
from latam_jobs.http import HttpClient, HttpError
from latam_jobs.locations import location_label
from latam_jobs.models import Job, KeywordStatus
from latam_jobs.notify import deliver_notifications
from latam_jobs.scheduler import (
    SourceSchedule,
    StopFlag,
    failure_backoff,
    is_due,
    next_success_at,
    utc_now,
)
from latam_jobs.settings import (
    ConfigError,
    Settings,
    merge_registered_sources,
    validate_registered_sources,
)
from latam_jobs.sources import SOURCES
from latam_jobs.sources.base import FetchContext, JobSource, SourceSnapshot
from latam_jobs.store import Store, should_store
from latam_jobs.urls import attach_direct_apply, greenhouse_boards

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
    dry_run: bool = False,
    ignore_schedule: bool = False,
    registry: dict[str, JobSource] | None = None,
    client: HttpClient | None = None,
    now: datetime | None = None,
    rng: Random | None = None,
    stop: StopFlag | None = None,
) -> list[SourceRun]:
    adapters = registry if registry is not None else SOURCES
    moment = now or utc_now()
    randomizer = rng or Random()
    store: Store | None = None
    effective = settings
    if not dry_run:
        store = Store(Path(settings.database.path))
        store.migrate()
        effective = merge_registered_sources(store.load_or_seed(settings), adapters)
    selected = _select_sources(effective, adapters, only_source)
    owns_client = client is None
    http = client or HttpClient(
        timeout=effective.http.timeout_seconds,
        max_retries=effective.http.max_retries,
    )
    runs: list[SourceRun] = []
    inserted = 0
    try:
        if store is not None:
            store.delete_expired_jobs(moment)
        catalog = ()
        if any(name in {"greenhouse", "lever", "ashby"} for name, _adapter in selected):
            catalog = load_catalog(
                effective,
                now=moment,
                client=http,
                working_path=Path(effective.database.path).parent / "ats-companies.json",
            )
        for name, adapter in selected:
            if stop is not None and stop.stopped:
                logger.info("stopping before source %s", name)
                break
            source_settings = effective.sources.get(name)
            if source_settings is None:
                continue
            schedule = store.get_schedule(name) if store is not None else None
            if (
                only_source is None
                and not ignore_schedule
                and store is not None
                and not is_due(schedule, moment)
            ):
                logger.info("source %s is not due", name)
                continue
            if name == "greenhouse" and store is not None:
                feeds = list(
                    dict.fromkeys([*source_settings.feeds, *store.list_boards("greenhouse")])
                )
                source_settings = source_settings.model_copy(update={"feeds": feeds})
            try:
                result = adapter.fetch(
                    FetchContext(
                        client=http,
                        settings=source_settings,
                        state=_snapshot(schedule),
                        search_terms=tuple(
                            term.strip() for term in effective.keywords if term.strip()
                        ),
                        remote_only=effective.remote_only,
                        catalog=catalog,
                        now=moment,
                    )
                )
            except Exception as exc:
                logger.exception("source %s failed", name)
                if store is not None:
                    _record_failure(store, name, moment, exc, schedule)
                runs.append(SourceRun(name=name, error=str(exc)))
                continue
            if store is not None and name != "greenhouse":
                found: set[str] = set()
                for item in result.jobs:
                    found.update(
                        greenhouse_boards(item.url, item.apply_url or "", item.description)
                    )
                if found:
                    store.remember_boards({"greenhouse": found}, moment)
            for item in result.jobs:
                attach_direct_apply(item)
            jobs = [annotate(item, effective) for item in result.jobs]
            visible = [job for job in jobs if _keep(job)]
            if store is not None:
                inserted += store.upsert_jobs(
                    [job for job in visible if should_store(job, effective)], moment
                )
                store.record_success(
                    name,
                    now=moment,
                    next_fetch_at=next_success_at(
                        moment,
                        source_settings.interval_hours,
                        effective.scheduler.jitter_seconds,
                        randomizer,
                    ),
                    etag=result.etag,
                    last_modified=result.last_modified,
                    status_code=result.status_code,
                )
            runs.append(
                SourceRun(
                    name=name,
                    jobs=visible,
                    not_modified=result.not_modified,
                    skipped_items=result.skipped_items,
                )
            )
        if store is not None:
            try:
                deliver_notifications(store, effective, now=moment)
            except Exception:
                logger.exception("notification delivery failed")
            failed = sum(1 for run in runs if run.error)
            succeeded = len(runs) - failed
            if failed and succeeded:
                outcome = "partial"
            elif failed:
                outcome = "failed"
            else:
                outcome = "success"
            store.record_run(
                started_at=moment,
                finished_at=utc_now(),
                outcome=outcome,
                new_jobs=inserted,
                sources_ok=succeeded,
                sources_failed=failed,
            )
    finally:
        if owns_client:
            http.close()
        if store is not None:
            store.close()
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
    location = location_label(job.location_text, job.location_countries)
    if location == "—":
        location = "Location not provided"
    eligibility = job.latam_eligibility.value if job.latam_eligibility else "unclassified"
    return (
        f"[{job.source}] {job.title}\n"
        f"Company: {job.company}\n"
        f"Location: {location}{salary}\n"
        f"Eligibility: {eligibility}\n"
        f"Reason: {job.eligibility_reason}\n"
        f"Keywords: {job.keyword_reason}\n"
        f"Apply: {job.apply_url or job.url}"
    )


def _keep(job: Job) -> bool:
    return job.keyword_status is KeywordStatus.MATCHED


def _snapshot(schedule: SourceSchedule | None) -> SourceSnapshot:
    if schedule is None:
        return SourceSnapshot()
    return SourceSnapshot(etag=schedule.etag, last_modified=schedule.last_modified)


def _record_failure(
    store: Store,
    name: str,
    now: datetime,
    exc: Exception,
    schedule: SourceSchedule | None,
) -> None:
    status = exc.status_code if isinstance(exc, HttpError) else None
    failures = (schedule.consecutive_failures if schedule else 0) + 1
    store.record_failure(
        name,
        now=now,
        backoff_until=failure_backoff(now, failures, status),
        status_code=status,
        error=str(exc),
        previous=schedule,
    )


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
