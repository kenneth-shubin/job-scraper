-- We Work Remotely is no longer collected.
DELETE FROM jobs WHERE source = 'wwr';

DELETE FROM source_state WHERE source_name = 'wwr';

UPDATE app_settings SET document = json_remove(document, '$.sources.wwr');

-- Move stored intervals to the schedule built from each site's published request rules.
UPDATE app_settings SET document = json_replace(
    document,
    '$.sources.remotive.interval_hours', 6,
    '$.sources.himalayas.interval_hours', 24,
    '$.sources.himalayas.max_pages', 2,
    '$.sources.jobicy.interval_hours', 3,
    '$.sources.jobicy.max_pages', 3,
    '$.sources.getonboard.interval_hours', 3,
    '$.sources.remoteok.interval_hours', 6,
    '$.sources.arbeitnow.interval_hours', 6,
    '$.sources.greenhouse.interval_hours', 2,
    '$.sources.lever.interval_hours', 2,
    '$.sources.ashby.interval_hours', 6
);

UPDATE source_state
SET next_fetch_at = strftime(
    '%Y-%m-%dT%H:%M:%S+00:00',
    last_success_at,
    CASE source_name
        WHEN 'remotive' THEN '+6 hours'
        WHEN 'himalayas' THEN '+24 hours'
        WHEN 'jobicy' THEN '+3 hours'
        WHEN 'getonboard' THEN '+3 hours'
        WHEN 'remoteok' THEN '+6 hours'
        WHEN 'arbeitnow' THEN '+6 hours'
        WHEN 'greenhouse' THEN '+2 hours'
        WHEN 'lever' THEN '+2 hours'
        WHEN 'ashby' THEN '+6 hours'
    END
)
WHERE last_success_at IS NOT NULL
  AND source_name IN (
    'remotive', 'himalayas', 'jobicy', 'getonboard', 'remoteok', 'arbeitnow',
    'greenhouse', 'lever', 'ashby'
  );
