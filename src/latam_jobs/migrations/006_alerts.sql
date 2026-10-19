ALTER TABLE jobs ADD COLUMN alert_seen_at TEXT;

-- Jobs stored before alerts existed count as seen, so only new finds raise an alert.
UPDATE jobs SET alert_seen_at = discovered_at;

-- While Telegram is off, the pending backlog is held back so turning it on later
-- sends only jobs found after that moment.
UPDATE jobs SET notification_status = 'backlog'
WHERE notification_status = 'pending'
  AND (SELECT json_extract(document, '$.notifications.enabled') FROM app_settings WHERE id = 1) = 0;
