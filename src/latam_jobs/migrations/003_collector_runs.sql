CREATE TABLE collector_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL,
    outcome TEXT NOT NULL,
    new_jobs INTEGER NOT NULL,
    sources_ok INTEGER NOT NULL,
    sources_failed INTEGER NOT NULL
);
