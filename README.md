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

A new job is stored only when its publication time is within the last 24 hours. The jobs page lists only those recent jobs, sorted by publication time. Older stored jobs stay in the database and are still refreshed, but they are not shown. A different-host apply URL shows Apply Directly and View Original Job. When an aggregator listing includes a Greenhouse, Lever, or Ashby link to one opening, that link is the Apply Directly URL. An aggregator listing without that link shows Apply on Job Board only. Greenhouse, Lever, and Ashby board APIs stay off, so the same opening is not collected a second time. The jobs page shows when jobs were last checked, how many new accepted jobs that check stored, and a Check for jobs button that runs the collector. Each row shows the posted time in the display timezone. Times use the display timezone from the settings page, which defaults to `America/Bogota`.

## Telegram

Alerts are off until the dashboard's "Send Telegram alerts" switch is on and both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are set. Only accepted keyword matches that have not been sent are included. Messages are plain text, in batches under 3,500 characters, and a batch is marked sent only after Telegram returns HTTP 200.

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
sudo cp deploy/latam-jobs.service deploy/latam-jobs.timer /etc/systemd/system/
sudo cp deploy/latam-jobs-web.service /etc/systemd/system/
sudo cp deploy/latam-jobs-backup.service deploy/latam-jobs-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now latam-jobs.timer
sudo systemctl enable --now latam-jobs-web.service
sudo systemctl enable --now latam-jobs-backup.timer
```

The collector is a oneshot timer. It runs hourly, waits a random 0–120 seconds, and catches up after a reboot because the timer sets `Persistent=true`. The process also takes an exclusive lock on `/var/lib/latam-jobs/latam-jobs.lock`, so a manual run cannot overlap the timer. On `SIGTERM` it finishes the current source and exits before the next one.

The web service runs `latam-jobs serve` and does not take that lock. It restarts on failure and stays up across reboots. Logs for both units go to journald:

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
