ALTER TABLE jobs ADD COLUMN apply_url TEXT;

ALTER TABLE jobs ADD COLUMN apply_url_type TEXT NOT NULL DEFAULT 'job_board';
