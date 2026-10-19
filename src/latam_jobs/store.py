"""SQLite storage for jobs, source schedules, and editable settings."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from latam_jobs.models import Job, KeywordStatus, LatamEligibility
from latam_jobs.scheduler import SourceSchedule, from_iso, to_iso, utc_now
from latam_jobs.settings import Settings, settings_from_mapping
from latam_jobs.urls import canonical_url, content_hash

_MIGRATIONS = Path(__file__).parent / "migrations"
_UNSEEN_ALERT = (
    "alert_seen_at IS NULL AND latam_eligibility = 'accepted' AND keyword_status = 'matched'"
)


@dataclass
class StoredJob:
    id: int
    source: str
    source_job_id: str
    title: str
    company: str
    url: str
    canonical_url: str
    description: str
    tags: tuple[str, ...]
    location_text: str
    location_countries: tuple[str, ...]
    salary: str | None
    published_at: str | None
    discovered_at: str
    last_seen_at: str
    remote_status: str
    latam_eligibility: str
    eligibility_reason: str
    keyword_status: str
    keyword_reason: str
    notification_status: str
    notification_attempts: int
    notified_at: str | None
    content_hash: str
    apply_url: str | None = None
    apply_url_type: str = "job_board"
    alert_seen_at: str | None = None


@dataclass
class CollectorRun:
    id: int
    started_at: datetime
    finished_at: datetime
    outcome: str
    new_jobs: int
    sources_ok: int
    sources_failed: int


class Store:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._lock = threading.RLock()
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.execute("PRAGMA foreign_keys=ON")

    def close(self) -> None:
        self._conn.close()

    def migrate(self) -> None:
        applied = (
            {
                int(row["version"])
                for row in self._conn.execute("SELECT version FROM schema_migrations").fetchall()
            }
            if self._has_migrations_table()
            else set()
        )
        for path in sorted(_MIGRATIONS.glob("*.sql")):
            version = int(path.name.split("_", 1)[0])
            if version in applied:
                continue
            self._apply_migration(version, path.read_text(encoding="utf-8"))

    def load_or_seed(self, bootstrap: Settings) -> Settings:
        row = self._conn.execute("SELECT document FROM app_settings WHERE id = 1").fetchone()
        if row is None:
            self.save_settings(bootstrap, bootstrap=bootstrap)
            return bootstrap
        stored = settings_from_mapping(json.loads(row["document"]))
        return stored.model_copy(update={"database": bootstrap.database, "web": bootstrap.web})

    def save_settings(self, settings: Settings, *, bootstrap: Settings) -> None:
        document = settings.model_dump(mode="json")
        document["database"] = bootstrap.database.model_dump(mode="json")
        document["web"] = bootstrap.web.model_dump(mode="json")
        payload = json.dumps(document, sort_keys=True)
        now = to_iso(utc_now())
        self._conn.execute("BEGIN")
        try:
            self._conn.execute(
                """
                INSERT INTO app_settings (id, document, updated_at)
                VALUES (1, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    document = excluded.document,
                    updated_at = excluded.updated_at
                """,
                (payload, now),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def upsert_jobs(self, jobs: list[Job], now: datetime) -> int:
        """Store jobs and return how many accepted jobs were newly inserted."""
        inserted = 0
        self._conn.execute("BEGIN")
        try:
            for job in jobs:
                if (
                    self._upsert_one(job, now)
                    and job.latam_eligibility is LatamEligibility.ACCEPTED
                ):
                    inserted += 1
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return inserted

    def delete_expired_jobs(self, now: datetime, *, days: int = 4) -> int:
        """Remove jobs posted more than `days` ago."""
        cutoff = to_iso(now - timedelta(days=days))
        cursor = self._conn.execute(
            """
            DELETE FROM jobs
            WHERE COALESCE(published_at, discovered_at) < ?
            """,
            (cutoff,),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def remember_boards(self, boards: dict[str, set[str]], now: datetime) -> None:
        seen = to_iso(now)
        for platform, names in boards.items():
            for name in sorted(names):
                self._conn.execute(
                    """
                    INSERT INTO ats_boards (platform, board, first_seen_at, last_seen_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(platform, board) DO UPDATE SET
                        last_seen_at = excluded.last_seen_at
                    """,
                    (platform, name, seen, seen),
                )
        self._conn.commit()

    def list_boards(self, platform: str) -> list[str]:
        rows = self._conn.execute(
            "SELECT board FROM ats_boards WHERE platform = ? ORDER BY board",
            (platform,),
        ).fetchall()
        return [str(row["board"]) for row in rows]

    def count_jobs(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS count FROM jobs").fetchone()
        return int(row["count"])

    def get_job(self, source: str, source_job_id: str) -> StoredJob | None:
        row = self._conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (source, source_job_id),
        ).fetchone()
        return _stored_job(row) if row is not None else None

    def list_jobs(
        self,
        *,
        eligibility: str | None = None,
        source: str | None = None,
        query: str | None = None,
        application: str | None = None,
        since: datetime | None = None,
        limit: int = 100,
    ) -> list[StoredJob]:
        clauses = ["keyword_status = 'matched'"]
        params: list[object] = []
        if since is not None:
            clauses.append("published_at >= ?")
            params.append(to_iso(since))
        if application in {"direct", "job_board"}:
            clauses.append("apply_url_type = ?")
            params.append(application)
        if eligibility:
            clauses.append("latam_eligibility = ?")
            params.append(eligibility)
        if source:
            clauses.append("source = ?")
            params.append(source)
        if query:
            clauses.append("(title LIKE ? OR company LIKE ? OR description LIKE ?)")
            like = f"%{query}%"
            params.extend((like, like, like))
        params.append(limit)
        rows = self._conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {" AND ".join(clauses)}
            ORDER BY published_at IS NULL, published_at DESC, discovered_at DESC, id DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        return [_stored_job(row) for row in rows]

    def list_all_jobs(self) -> list[StoredJob]:
        rows = self._conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()
        return [_stored_job(row) for row in rows]

    def list_pending_notifications(self, limit: int = 50) -> list[StoredJob]:
        rows = self._conn.execute(
            """
            SELECT * FROM jobs
            WHERE latam_eligibility = 'accepted'
              AND keyword_status = 'matched'
              AND notification_status = 'pending'
            ORDER BY discovered_at ASC, id ASC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_stored_job(row) for row in rows]

    def mark_notified(self, job_ids: list[int], now: datetime) -> None:
        if not job_ids:
            return
        moment = to_iso(now)
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                """
                UPDATE jobs
                SET notification_status = 'sent', notified_at = ?
                WHERE id = ? AND notification_status = 'pending'
                """,
                [(moment, job_id) for job_id in job_ids],
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def skip_pending_notifications(self) -> int:
        """Leave the current Telegram backlog unsent, so only later finds are sent."""
        cursor = self._conn.execute(
            "UPDATE jobs SET notification_status = 'backlog' WHERE notification_status = 'pending'"
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def list_unseen_alerts(self, limit: int = 10) -> list[StoredJob]:
        rows = self._conn.execute(
            f"""
            SELECT * FROM jobs
            WHERE {_UNSEEN_ALERT}
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [_stored_job(row) for row in rows]

    def count_unseen_alerts(self) -> int:
        row = self._conn.execute(
            f"SELECT COUNT(*) AS count FROM jobs WHERE {_UNSEEN_ALERT}"
        ).fetchone()
        return int(row["count"])

    def mark_alerts_seen(self, now: datetime, *, up_to_id: int) -> int:
        cursor = self._conn.execute(
            "UPDATE jobs SET alert_seen_at = ? WHERE alert_seen_at IS NULL AND id <= ?",
            (to_iso(now), up_to_id),
        )
        self._conn.commit()
        return int(cursor.rowcount)

    def count_new_accepted(self, since: datetime) -> int:
        row = self._conn.execute(
            """
            SELECT COUNT(*) AS count FROM jobs
            WHERE latam_eligibility = 'accepted'
              AND keyword_status = 'matched'
              AND discovered_at >= ?
            """,
            (to_iso(since),),
        ).fetchone()
        return int(row["count"])

    def mark_notification_failed(self, job_ids: list[int], *, give_up: bool) -> None:
        if not job_ids:
            return
        status = "failed" if give_up else "pending"
        self._conn.execute("BEGIN")
        try:
            self._conn.executemany(
                """
                UPDATE jobs
                SET notification_status = ?,
                    notification_attempts = notification_attempts + 1
                WHERE id = ? AND notification_status = 'pending'
                """,
                [(status, job_id) for job_id in job_ids],
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def update_classification(self, stored: StoredJob, job: Job) -> None:
        duplicate = self._canonical_exists(
            stored.canonical_url, stored.source, stored.source_job_id
        )
        status = _notification_status(job, duplicate=duplicate, previous=stored.notification_status)
        self._conn.execute(
            """
            UPDATE jobs
            SET remote_status = ?,
                latam_eligibility = ?,
                eligibility_reason = ?,
                keyword_status = ?,
                keyword_reason = ?,
                notification_status = ?
            WHERE id = ?
            """,
            (
                job.remote_status.value,
                job.latam_eligibility.value if job.latam_eligibility else stored.latam_eligibility,
                job.eligibility_reason,
                job.keyword_status.value if job.keyword_status else stored.keyword_status,
                job.keyword_reason,
                status,
                stored.id,
            ),
        )
        self._conn.commit()

    def get_schedule(self, source_name: str) -> SourceSchedule | None:
        row = self._conn.execute(
            "SELECT * FROM source_state WHERE source_name = ?",
            (source_name,),
        ).fetchone()
        if row is None:
            return None
        return SourceSchedule(
            source_name=row["source_name"],
            last_attempt_at=from_iso(row["last_attempt_at"]),
            last_success_at=from_iso(row["last_success_at"]),
            next_fetch_at=from_iso(row["next_fetch_at"]),
            etag=row["etag"],
            last_modified=row["last_modified"],
            consecutive_failures=int(row["consecutive_failures"]),
            backoff_until=from_iso(row["backoff_until"]),
            last_http_status=row["last_http_status"],
            last_error=row["last_error"],
        )

    def list_schedules(self) -> list[SourceSchedule]:
        rows = self._conn.execute(
            "SELECT source_name FROM source_state ORDER BY source_name"
        ).fetchall()
        return [schedule for name in rows if (schedule := self.get_schedule(name["source_name"]))]

    def record_success(
        self,
        source_name: str,
        *,
        now: datetime,
        next_fetch_at: datetime,
        etag: str | None,
        last_modified: str | None,
        status_code: int | None,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO source_state (
                source_name, last_attempt_at, last_success_at, next_fetch_at, etag,
                last_modified, consecutive_failures, backoff_until, last_http_status, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, 0, NULL, ?, NULL)
            ON CONFLICT(source_name) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                last_success_at = excluded.last_success_at,
                next_fetch_at = excluded.next_fetch_at,
                etag = excluded.etag,
                last_modified = excluded.last_modified,
                consecutive_failures = 0,
                backoff_until = NULL,
                last_http_status = excluded.last_http_status,
                last_error = NULL
            """,
            (
                source_name,
                to_iso(now),
                to_iso(now),
                to_iso(next_fetch_at),
                etag,
                last_modified,
                status_code,
            ),
        )
        self._conn.commit()

    def record_failure(
        self,
        source_name: str,
        *,
        now: datetime,
        backoff_until: datetime,
        status_code: int | None,
        error: str,
        previous: SourceSchedule | None,
    ) -> None:
        failures = (previous.consecutive_failures if previous else 0) + 1
        etag = previous.etag if previous else None
        last_modified = previous.last_modified if previous else None
        last_success = to_iso(previous.last_success_at) if previous else None
        self._conn.execute(
            """
            INSERT INTO source_state (
                source_name, last_attempt_at, last_success_at, next_fetch_at, etag,
                last_modified, consecutive_failures, backoff_until, last_http_status, last_error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name) DO UPDATE SET
                last_attempt_at = excluded.last_attempt_at,
                next_fetch_at = excluded.next_fetch_at,
                consecutive_failures = excluded.consecutive_failures,
                backoff_until = excluded.backoff_until,
                last_http_status = excluded.last_http_status,
                last_error = excluded.last_error
            """,
            (
                source_name,
                to_iso(now),
                last_success,
                to_iso(backoff_until),
                etag,
                last_modified,
                failures,
                to_iso(backoff_until),
                status_code,
                error[:500],
            ),
        )
        self._conn.commit()

    def record_run(
        self,
        *,
        started_at: datetime,
        finished_at: datetime,
        outcome: str,
        new_jobs: int,
        sources_ok: int,
        sources_failed: int,
    ) -> None:
        self._conn.execute(
            """
            INSERT INTO collector_runs (
                started_at, finished_at, outcome, new_jobs, sources_ok, sources_failed
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                to_iso(started_at),
                to_iso(finished_at),
                outcome,
                new_jobs,
                sources_ok,
                sources_failed,
            ),
        )
        self._conn.commit()

    def latest_run(self) -> CollectorRun | None:
        row = self._conn.execute("SELECT * FROM collector_runs ORDER BY id DESC LIMIT 1").fetchone()
        return _collector_run(row) if row is not None else None

    def latest_successful_run(self) -> CollectorRun | None:
        row = self._conn.execute(
            """
            SELECT * FROM collector_runs
            WHERE outcome = 'success'
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
        return _collector_run(row) if row is not None else None

    def record_login_attempt(self, now: datetime, *, succeeded: bool) -> None:
        self._prune_login_attempts(now)
        self._conn.execute(
            "INSERT INTO login_attempts (attempted_at, succeeded) VALUES (?, ?)",
            (to_iso(now), 1 if succeeded else 0),
        )
        self._conn.commit()

    def recent_login_failures(self, now: datetime) -> list[datetime]:
        self._prune_login_attempts(now)
        window = to_iso(now - timedelta(minutes=15))
        rows = self._conn.execute(
            """
            SELECT attempted_at FROM login_attempts
            WHERE succeeded = 0 AND attempted_at >= ?
            ORDER BY attempted_at
            """,
            (window,),
        ).fetchall()
        return [moment for row in rows if (moment := from_iso(row["attempted_at"]))]

    def _prune_login_attempts(self, now: datetime) -> None:
        cutoff = to_iso(now - timedelta(days=1))
        self._conn.execute("DELETE FROM login_attempts WHERE attempted_at < ?", (cutoff,))
        self._conn.commit()

    def _upsert_one(self, job: Job, now: datetime) -> bool:
        if job.latam_eligibility is None or job.keyword_status is None:
            raise ValueError(f"Job {job.source}:{job.source_job_id} is not classified")
        canonical = canonical_url(job.url)
        digest = content_hash(job.title, job.company, canonical)
        moment = to_iso(now)
        existing = self._conn.execute(
            "SELECT * FROM jobs WHERE source = ? AND source_job_id = ?",
            (job.source, job.source_job_id),
        ).fetchone()
        duplicate = self._canonical_exists(canonical, job.source, job.source_job_id)
        previous = existing["notification_status"] if existing is not None else None
        notification = _notification_status(job, duplicate=duplicate, previous=previous)
        published = to_iso(job.published_at)
        shared = (
            job.title,
            job.company,
            job.url,
            job.apply_url,
            job.apply_url_type.value,
            canonical,
            job.description,
            json.dumps(list(job.tags)),
            job.location_text,
            json.dumps(list(job.location_countries)),
            job.salary,
            published,
            job.remote_status.value,
            job.latam_eligibility.value,
            job.eligibility_reason,
            job.keyword_status.value,
            job.keyword_reason,
            notification,
            digest,
        )
        if existing is None:
            if not _within_recent_window(job.published_at, now):
                return False
            alerts = (
                not duplicate
                and job.latam_eligibility is LatamEligibility.ACCEPTED
                and job.keyword_status is KeywordStatus.MATCHED
            )
            self._conn.execute(
                """
                INSERT INTO jobs (
                    source, source_job_id, title, company, url, apply_url, apply_url_type,
                    canonical_url, description, tags_json, location_text, location_countries_json,
                    salary, published_at, remote_status, latam_eligibility, eligibility_reason,
                    keyword_status, keyword_reason, notification_status, content_hash,
                    discovered_at, last_seen_at, alert_seen_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                """,
                (
                    job.source,
                    job.source_job_id,
                    *shared,
                    moment,
                    moment,
                    None if alerts else moment,
                ),
            )
            return True
        self._conn.execute(
            """
            UPDATE jobs SET
                title = ?, company = ?, url = ?, apply_url = ?, apply_url_type = ?,
                canonical_url = ?, description = ?, tags_json = ?, location_text = ?,
                location_countries_json = ?, salary = ?, published_at = ?, remote_status = ?,
                latam_eligibility = ?, eligibility_reason = ?, keyword_status = ?,
                keyword_reason = ?, notification_status = ?, content_hash = ?, last_seen_at = ?
            WHERE source = ? AND source_job_id = ?
            """,
            (*shared, moment, job.source, job.source_job_id),
        )
        return False

    def _canonical_exists(self, canonical: str, source: str, source_job_id: str) -> bool:
        row = self._conn.execute(
            """
            SELECT 1 FROM jobs
            WHERE canonical_url = ? AND NOT (source = ? AND source_job_id = ?)
            LIMIT 1
            """,
            (canonical, source, source_job_id),
        ).fetchone()
        return row is not None

    def _has_migrations_table(self) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
        ).fetchone()
        return row is not None

    def _apply_migration(self, version: int, script: str) -> None:
        self._conn.execute("BEGIN")
        try:
            for statement in _sql_statements(script):
                self._conn.execute(statement)
            self._conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, to_iso(utc_now())),
            )
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise


def _serialized(method: Callable) -> Callable:
    @wraps(method)
    def wrapped(self: Store, *args: object, **kwargs: object) -> object:
        with self._lock:
            return method(self, *args, **kwargs)

    return wrapped


for _name in (
    "close",
    "migrate",
    "load_or_seed",
    "save_settings",
    "upsert_jobs",
    "delete_expired_jobs",
    "remember_boards",
    "list_boards",
    "count_jobs",
    "get_job",
    "list_jobs",
    "list_all_jobs",
    "list_pending_notifications",
    "mark_notified",
    "mark_notification_failed",
    "skip_pending_notifications",
    "list_unseen_alerts",
    "count_unseen_alerts",
    "mark_alerts_seen",
    "count_new_accepted",
    "update_classification",
    "get_schedule",
    "list_schedules",
    "record_success",
    "record_failure",
    "record_run",
    "latest_run",
    "latest_successful_run",
    "record_login_attempt",
    "recent_login_failures",
):
    setattr(Store, _name, _serialized(getattr(Store, _name)))


def should_store(job: Job, settings: Settings) -> bool:
    if job.keyword_status is not KeywordStatus.MATCHED or job.latam_eligibility is None:
        return False
    if (
        job.latam_eligibility is LatamEligibility.MAYBE
        and not settings.eligibility.include_maybe_in_storage
    ):
        return False
    if (
        job.latam_eligibility is LatamEligibility.REJECTED
        and not settings.eligibility.store_rejected
    ):
        return False
    return True


def _notification_status(job: Job, *, duplicate: bool, previous: str | None) -> str:
    if previous == "sent":
        return "sent"
    if previous in {"failed", "backlog"}:
        return previous
    if duplicate:
        return "skipped"
    if (
        job.latam_eligibility is LatamEligibility.ACCEPTED
        and job.keyword_status is KeywordStatus.MATCHED
    ):
        return "pending"
    return "skipped"


def _stored_job(row: sqlite3.Row) -> StoredJob:
    return StoredJob(
        id=int(row["id"]),
        source=row["source"],
        source_job_id=row["source_job_id"],
        title=row["title"],
        company=row["company"],
        url=row["url"],
        canonical_url=row["canonical_url"],
        description=row["description"],
        tags=tuple(json.loads(row["tags_json"])),
        location_text=row["location_text"],
        location_countries=tuple(json.loads(row["location_countries_json"])),
        salary=row["salary"],
        published_at=row["published_at"],
        discovered_at=row["discovered_at"],
        last_seen_at=row["last_seen_at"],
        remote_status=row["remote_status"],
        latam_eligibility=row["latam_eligibility"],
        eligibility_reason=row["eligibility_reason"],
        keyword_status=row["keyword_status"],
        keyword_reason=row["keyword_reason"],
        notification_status=row["notification_status"],
        notification_attempts=int(row["notification_attempts"]),
        notified_at=row["notified_at"],
        content_hash=row["content_hash"],
        apply_url=row["apply_url"],
        apply_url_type=row["apply_url_type"],
        alert_seen_at=row["alert_seen_at"],
    )


def _collector_run(row: sqlite3.Row) -> CollectorRun:
    started = from_iso(row["started_at"])
    finished = from_iso(row["finished_at"])
    if started is None or finished is None:
        raise ValueError("collector run is missing a timestamp")
    return CollectorRun(
        id=int(row["id"]),
        started_at=started,
        finished_at=finished,
        outcome=row["outcome"],
        new_jobs=int(row["new_jobs"]),
        sources_ok=int(row["sources_ok"]),
        sources_failed=int(row["sources_failed"]),
    )


def _within_recent_window(published: datetime | None, now: datetime) -> bool:
    if published is None or published.tzinfo is None:
        return False
    return published >= now - timedelta(days=4)


def _sql_statements(script: str) -> list[str]:
    statements: list[str] = []
    current: list[str] = []
    for line in script.splitlines():
        if line.strip().startswith("--"):
            continue
        current.append(line)
        if line.rstrip().endswith(";"):
            statement = "\n".join(current).strip().rstrip(";").strip()
            if statement:
                statements.append(statement)
            current = []
    return statements
