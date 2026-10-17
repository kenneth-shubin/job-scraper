CREATE TABLE ats_boards (
    platform TEXT NOT NULL,
    board TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    PRIMARY KEY (platform, board)
);
