CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE app_settings (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    document TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE jobs (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_job_id TEXT NOT NULL,
    title TEXT NOT NULL,
    company TEXT NOT NULL,
    url TEXT NOT NULL,
    canonical_url TEXT NOT NULL,
    description TEXT NOT NULL,
    tags_json TEXT NOT NULL,
    location_text TEXT NOT NULL,
    location_countries_json TEXT NOT NULL,
    salary TEXT,
    published_at TEXT,
    discovered_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    remote_status TEXT NOT NULL,
    latam_eligibility TEXT NOT NULL,
    eligibility_reason TEXT NOT NULL,
    keyword_status TEXT NOT NULL,
    keyword_reason TEXT NOT NULL,
    notification_status TEXT NOT NULL,
    notification_attempts INTEGER NOT NULL DEFAULT 0,
    notified_at TEXT,
    content_hash TEXT NOT NULL,
    UNIQUE (source, source_job_id)
);

CREATE INDEX idx_jobs_eligibility_notification
    ON jobs (latam_eligibility, notification_status);

CREATE INDEX idx_jobs_canonical_url ON jobs (canonical_url);

CREATE TABLE source_state (
    source_name TEXT PRIMARY KEY,
    last_attempt_at TEXT,
    last_success_at TEXT,
    next_fetch_at TEXT,
    etag TEXT,
    last_modified TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    backoff_until TEXT,
    last_http_status INTEGER,
    last_error TEXT
);
