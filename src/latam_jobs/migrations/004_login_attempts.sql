CREATE TABLE login_attempts (
    id INTEGER PRIMARY KEY,
    attempted_at TEXT NOT NULL,
    succeeded INTEGER NOT NULL
);

CREATE INDEX idx_login_attempts_time ON login_attempts (attempted_at);
