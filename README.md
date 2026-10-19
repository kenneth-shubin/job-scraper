# LATAM jobs

Personal collector for remote software jobs that a resident of one Latin American country can take. The default residence country is Colombia. Jobs are stored in SQLite and reviewed in a dashboard on `127.0.0.1`. Telegram is optional and stays off until you enable it.

## Local use

```bash
uv sync
cp .env.example .env
# Put long random values in WEB_SECRET and DASHBOARD_PASSWORD before starting the dashboard.
uv run latam-jobs check
uv run latam-jobs run --dry-run
uv run latam-jobs serve
```

Open `http://127.0.0.1:8080` and enter `DASHBOARD_PASSWORD`. The jobs page works with notifications disabled. `latam-jobs run` does not need the password.

Himalayas and Get on Board are searched with each keyword, and Get on Board asks for remote listings when remote-only is on. Jobicy is read in one LATAM pass per run, and keywords are matched locally. A job is stored when it was posted or reposted within the last 4 days. The Show filter lists the last 24 hours, or jobs posted on or after the calendar day three days ago in the display timezone. A saved job is removed automatically once it is more than 4 days old. A different-host apply URL shows Apply Directly and View Original Job. When an aggregator listing includes a Greenhouse, Lever, or Ashby link to one opening, that link is the Apply Directly URL. An aggregator listing without that link shows Apply on Job Board only. Greenhouse, Lever, and Ashby boards come from the local company catalog. Online updates are on by default and refresh that catalog every 24 hours; a failed refresh still uses the local file. Company board requests run one at a time with a short pause. A Greenhouse list is opened without the description, and the description is fetched when the title matches a keyword and the post or repost is inside 4 days. A Greenhouse repost is dated from the later of its first publish time and its update time. Each row shows how long ago the job was posted. Times use the display timezone from the settings page, which defaults to `America/Bogota`.

## Background search

While `latam-jobs serve` runs, every enabled source is collected automatically on its own interval. There is no manual check button. The scheduler looks every 30 seconds and starts at most one due source per look, so sources start spaced out, and a source never overlaps itself. The jobs page shows what is being checked now, when the last source finished, and how many new accepted jobs arrived in the last 24 hours. The Settings page shows each source's last run, next run, and last error.

Intervals follow each site's published request rules:

| Source | Interval | Requests per run |
| --- | --- | --- |
| Remotive | 6 hours | 1 |
| Himalayas | 24 hours | one search per keyword, at most 2 pages, 3 seconds apart |
| Jobicy | 3 hours | one LATAM pass, at most 3 cursor pages |
| Get on Board | 3 hours | one search per keyword, at most 1 request a second |
| Remote OK | 6 hours | 1 |
| Arbeitnow | 6 hours | at most 2 pages |
| Greenhouse, Lever | 2 hours | one board at a time, 1 to 2 seconds apart |
| Ashby | 6 hours | one board at a time, 30 second timeout |

The code enforces a minimum per source (1 hour for Jobicy, Greenhouse, and Lever; the interval above for the rest), so neither `settings.yaml` nor the Settings page can schedule a site more often. On a career page, a 429 or 5xx doubles the pause, and three in a row end that pass early. A failed source waits out its backoff: 24 hours after a 403 or 429, 1 hour otherwise, doubling per failure up to 7 days. Setting `HIMALAYAS_API_KEY` sends a free Himalayas key for its keyed allowance.

The server holds the collector lock while it runs, so `latam-jobs run` exits with a message instead of collecting at the same time.

### Restarts

Schedules live in SQLite, so a restart continues where the server stopped. The first check runs 2 minutes after startup, and overdue sources then start one at a time, 30 seconds apart. A source that missed several intervals runs once. A pass cut off by a stop never recorded success, so it runs again, and jobs already saved are not stored twice. Backoff from before the stop still applies. The OS releases the lock when the process exits, even after a crash. Jobs are kept for 4 days, so a stop longer than that misses jobs that were posted and closed in the gap.

## Alerts

A new accepted job that matches a keyword raises an alert. Jobs stored before alerts existed, rejected jobs, and duplicates of a stored URL do not. The jobs page shows a banner with the new jobs and a New badge on their rows until you press Mark all seen. Enable desktop alerts asks the browser for permission; after that the page checks `/alerts` every 30 seconds and shows a desktop notification for jobs it has not announced yet. The page must stay open in a tab. Each alert channel sits behind one interface in `src/latam_jobs/alerts.py`, and Telegram is the second channel.

## Telegram
`In development`
Alerts are off until the dashboard's "Send Telegram alerts" switch is on and both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Only accepted keyword matches that have not been sent are included. Turning the switch on holds back jobs found before that moment, so the chat receives only later finds. Messages are plain text, in batches under 3,500 characters, and a batch is marked sent only after Telegram returns HTTP 200.

A crash after Telegram accepts a batch and before SQLite commits can send that batch again on the next run. That is the at-least-once window, and it is limited to one batch. A failed send stays pending and stops the rest of that run. After five failed attempts the row is marked failed. Rows already marked sent are not selected again. Missing credentials leave every pending row unsent.

`settings.yaml` is only the bootstrap file. The first successful load copies residence country, keywords, and source schedules into SQLite. After that, change those values in the dashboard. The database path and the web bind address still come from the bootstrap file.

## VPS layout

Ubuntu host, dedicated user `latamjobs`, no public inbound port. The dashboard listens on localhost. Reach it through an SSH tunnel.

| Path | Purpose |
| --- | --- |
| `/opt/latam-jobs` | Code and the virtualenv |
| `/var/lib/latam-jobs/latam-jobs.sqlite` | Database |
| `/etc/latam-jobs/settings.yaml` | Bootstrap config |
| `/etc/latam-jobs/latam-jobs.env` | Secrets, mode `0600` |

## Install

Install `uv` and `sqlite3`, then create the user and directories:

```bash
sudo useradd --system --create-home --home-dir /var/lib/latam-jobs --shell /usr/sbin/nologin latamjobs
sudo mkdir -p /opt/latam-jobs /etc/latam-jobs /var/lib/latam-jobs/backups
sudo chown latamjobs:latamjobs /var/lib/latam-jobs /var/lib/latam-jobs/backups
```

Copy this repository to `/opt/latam-jobs` and install the package there:

```bash
sudo cp -a . /opt/latam-jobs
cd /opt/latam-jobs
sudo uv sync --frozen
sudo chown -R root:root /opt/latam-jobs
sudo chmod -R a+rX /opt/latam-jobs
```

Copy `settings.yaml` to `/etc/latam-jobs/settings.yaml` and set:

```yaml
database:
  path: /var/lib/latam-jobs/latam-jobs.sqlite
web:
  host: 127.0.0.1
  port: 8080
```

Leave the other values as a starting point. The dashboard becomes the source of truth after the first run.

Create the secrets file and restrict it to root. systemd reads it for the service user:

```bash
sudo cp .env.example /etc/latam-jobs/latam-jobs.env
sudo chmod 0600 /etc/latam-jobs/latam-jobs.env
```

Set both `WEB_SECRET` and `DASHBOARD_PASSWORD` to long random values. The web service refuses to start until both are present. Neither value is written to SQLite or the logs. `HIMALAYAS_API_KEY` is optional. Telegram variables can stay empty until you want alerts.

The dashboard listens on `127.0.0.1:8080`. The password protects that local page. It does not protect SSH. A future public bind needs HTTPS; the session cookie is marked Secure only on HTTPS, so the SSH tunnel can stay on HTTP.

`LATAM_JOBS_CONFIG` selects the bootstrap file. The units set it to `/etc/latam-jobs/settings.yaml`. Without that variable, the program uses `/etc/latam-jobs/settings.yaml` when that file exists, and `./settings.yaml` otherwise.

## systemd

```bash
sudo cp deploy/latam-jobs-web.service /etc/systemd/system/
sudo cp deploy/latam-jobs-backup.service deploy/latam-jobs-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now latam-jobs-web.service
sudo systemctl enable --now latam-jobs-backup.timer
```

The web service runs `latam-jobs serve`, which collects every source in the background and holds the exclusive lock on `/var/lib/latam-jobs/latam-jobs.lock`. It restarts on failure (`Restart=on-failure`) and starts again after a reboot, so search resumes without a manual start.

`deploy/latam-jobs.timer` and `deploy/latam-jobs.service` are for a host without the dashboard. That oneshot runs hourly, waits a random 0–120 seconds, catches up after a reboot because the timer sets `Persistent=true`, and exits 1 while the web service holds the lock. On `SIGTERM` it finishes the current source and exits before the next one. Logs for both units go to journald:

```bash
journalctl -u latam-jobs.service -n 100
journalctl -u latam-jobs-web.service -n 100
```

## SSH tunnel

From your laptop:

```bash
ssh -L 8080:127.0.0.1:8080 user@your-vps
```

Then open `http://127.0.0.1:8080`. Keep port 8080 on localhost. The dashboard password does not protect SSH, and a future public bind needs HTTPS.

## Checks

Confirm the units parse:

```bash
systemd-analyze verify /etc/systemd/system/latam-jobs.service \
  /etc/systemd/system/latam-jobs.timer \
  /etc/systemd/system/latam-jobs-web.service \
  /etc/systemd/system/latam-jobs-backup.service \
  /etc/systemd/system/latam-jobs-backup.timer
```

Force one collection:

```bash
sudo systemctl start latam-jobs.service
journalctl -u latam-jobs.service -n 50
```

Or one source, as the service user:

```bash
sudo -u latamjobs LATAM_JOBS_CONFIG=/etc/latam-jobs/settings.yaml \
  /opt/latam-jobs/.venv/bin/latam-jobs run --source remotive
```

Confirm a second run is locked. Hold the lock, then start the collector; it should exit 1:

```bash
sudo -u latamjobs flock -n /var/lib/latam-jobs/latam-jobs.lock -c 'sleep 30' &
sudo systemctl start latam-jobs.service
```

Confirm the timer will catch up after downtime:

```bash
systemctl list-timers latam-jobs.timer
```

`Persistent=true` means a missed hourly tick runs once the machine is back.

## Backup and restore

`deploy/backup.sh` runs `sqlite3 .backup` into `/var/lib/latam-jobs/backups` and keeps the newest 7 files. The daily timer starts it.

Restore while the web service is stopped so nothing writes the live file:

```bash
sudo systemctl stop latam-jobs-web.service
sudo -u latamjobs sqlite3 /var/lib/latam-jobs/latam-jobs.sqlite \
  ".restore '/var/lib/latam-jobs/backups/latam-jobs-TIMESTAMP.sqlite'"
sudo systemctl start latam-jobs-web.service
```

Replace `TIMESTAMP` with the file you want. Stop the collector timer first if a run is in progress.
