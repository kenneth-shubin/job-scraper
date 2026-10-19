"""Local FastAPI dashboard for stored jobs and collection settings."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from latam_jobs.alerts import DashboardChannel, telegram_turned_on
from latam_jobs.geo import COUNTRIES
from latam_jobs.locations import location_label
from latam_jobs.models import ApplyUrlType, Job, RemoteStatus
from latam_jobs.pipeline import annotate, collect, due_sources
from latam_jobs.scheduler import (
    SAFE_MIN_INTERVAL_HOURS,
    LockError,
    RunLock,
    from_iso,
    run_lock_path,
    utc_now,
)
from latam_jobs.settings import ConfigError, Settings, merge_registered_sources
from latam_jobs.store import Store, StoredJob
from latam_jobs.web.auth import (
    SESSION_COOKIE,
    cooldown_active,
    issue_session,
    passwords_match,
    session_valid,
)
from latam_jobs.web.timefmt import format_clock, format_posted, format_when

logger = logging.getLogger(__name__)
_PACKAGE = Path(__file__).parent
_templates = Jinja2Templates(directory=str(_PACKAGE / "templates"))
_templates.env.globals["location_label"] = location_label
_templates.env.globals["format_posted"] = format_posted
_templates.env.globals["format_when"] = format_when
_templates.env.globals["format_clock"] = format_clock
_COOKIE = "latam_jobs_csrf"
_OPEN_PATHS = {"/login", "/logout"}
_DASHBOARD = DashboardChannel()

STARTUP_DELAY_SECONDS = 120.0
TICK_SECONDS = 30.0


class _IntervalTooShort(ValueError):
    """A source interval below the safe minimum for that site."""


def create_app(
    *,
    bootstrap: Settings,
    store: Store,
    registry: dict[str, Any],
    secret: str,
    password: str,
) -> FastAPI:
    if not secret.strip() or not password:
        raise ConfigError("WEB_SECRET and DASHBOARD_PASSWORD are required to serve the dashboard.")
    app = FastAPI(title="LATAM jobs", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_PACKAGE / "static")), name="static")
    app.state.bootstrap = bootstrap
    app.state.store = store
    app.state.registry = registry
    app.state.secret = secret
    app.state.password = password
    app.state.running = {}
    app.state.running_guard = threading.Lock()
    app.state.scheduler_state = "off"
    app.state.first_check_at = None
    app.state.collect_finished = 0
    app.state.collect_announced = 0

    @app.middleware("http")
    async def require_session(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.url.path
        if path.startswith("/static") or path in _OPEN_PATHS:
            return await call_next(request)
        if session_valid(secret, request.cookies.get(SESSION_COOKIE)):
            return await call_next(request)
        if request.headers.get("hx-request"):
            return Response(status_code=200, headers={"HX-Redirect": "/login"})
        return RedirectResponse("/login", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request) -> Response:
        context = {"title": "Unlock", "error": ""}
        return _login_response(request, secret, context)

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
        if not _csrf_matches(secret, token, request.cookies.get(_COOKIE)):
            return HTMLResponse("Invalid CSRF token.", status_code=403)
        now = utc_now()
        if cooldown_active(store.recent_login_failures(now), now):
            return _login_response(
                request,
                secret,
                {"title": "Unlock", "error": "Incorrect password. Please try again."},
                status_code=401,
            )
        provided = str(form.get("password") or "")
        if not passwords_match(provided, password):
            store.record_login_attempt(now, succeeded=False)
            return _login_response(
                request,
                secret,
                {"title": "Unlock", "error": "Incorrect password. Please try again."},
                status_code=401,
            )
        store.record_login_attempt(now, succeeded=True)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            SESSION_COOKIE,
            issue_session(secret, now=now),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            path="/",
            max_age=7 * 24 * 60 * 60,
        )
        return response

    @app.post("/logout")
    async def logout(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
        if not _csrf_matches(secret, token, request.cookies.get(_COOKIE)):
            return HTMLResponse("Invalid CSRF token.", status_code=403)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/", response_class=HTMLResponse)
    def jobs_page(request: Request) -> Response:
        eligibility, source, query, application, posted = _filters(request)
        return _page(
            request,
            secret,
            "jobs.html",
            {
                "title": "Jobs",
                "jobs": _matching_jobs(
                    store,
                    eligibility,
                    source,
                    query,
                    application,
                    posted,
                    _current_settings(app).display_timezone,
                ),
                "eligibility": eligibility,
                "source": source,
                "query": query,
                "application": application,
                "posted": posted,
                "sources": list(registry),
                **_status_context(app),
                **_clock(app),
            },
        )

    @app.get("/jobs/rows", response_class=HTMLResponse)
    def job_rows(request: Request) -> Response:
        eligibility, source, query, application, posted = _filters(request)
        return _templates.TemplateResponse(
            request,
            "jobs_rows.html",
            {
                "jobs": _matching_jobs(
                    store,
                    eligibility,
                    source,
                    query,
                    application,
                    posted,
                    _current_settings(app).display_timezone,
                ),
                **_clock(app),
            },
        )

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request) -> Response:
        current = _current_settings(app)
        return _settings_response(
            request,
            secret,
            _settings_context(app, current, saved=request.query_params.get("saved") == "1"),
        )

    @app.post("/settings")
    async def save_settings(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
        if not _csrf_matches(secret, token, request.cookies.get(_COOKIE)):
            return HTMLResponse("Invalid CSRF token.", status_code=403)
        current = _current_settings(app)
        try:
            updated = _settings_from_form(current, form, registry)
        except (ValidationError, ValueError) as exc:
            if isinstance(exc, ValidationError):
                errors = [err["msg"] for err in exc.errors()]
            elif isinstance(exc, _IntervalTooShort):
                errors = [str(exc)]
            else:
                errors = ["Interval hours must be a whole number from 1 to 168."]
            return _settings_response(
                request,
                secret,
                _settings_context(app, current, errors=errors),
                status_code=400,
            )
        store.save_settings(updated, bootstrap=bootstrap)
        reclassify_stored_jobs(store, updated)
        telegram_turned_on(store, current, updated)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/status")
    def status_page() -> Response:
        return RedirectResponse("/", status_code=303)

    @app.get("/status/summary", response_class=HTMLResponse)
    def status_summary(request: Request) -> Response:
        return _summary_response(request, app)

    @app.get("/alerts")
    def alerts() -> Response:
        unseen = _DASHBOARD.unseen(store)
        return JSONResponse(
            {
                "count": unseen.count,
                "jobs": [
                    {
                        "id": job.id,
                        "title": job.title,
                        "company": job.company,
                        "url": job.apply_url or job.url,
                    }
                    for job in unseen.jobs
                ],
            }
        )

    @app.post("/alerts/seen")
    async def alerts_seen(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
        if not _csrf_matches(secret, token, request.cookies.get(_COOKIE)):
            return HTMLResponse("Invalid CSRF token.", status_code=403)
        try:
            up_to = int(str(form.get("up_to") or "0"))
        except ValueError:
            up_to = 0
        _DASHBOARD.mark_seen(store, utc_now(), up_to_id=up_to)
        if not request.headers.get("hx-request"):
            return RedirectResponse("/", status_code=303)
        return Response(status_code=204, headers={"HX-Trigger": "alertsSeen, refreshJobs"})

    @app.get("/alerts/bell", response_class=HTMLResponse)
    def alert_bell(request: Request) -> Response:
        nonce, cookie = _csrf_pair(request, secret)
        response = _templates.TemplateResponse(
            request,
            "alert_bell.html",
            {"alerts": _DASHBOARD.unseen(store), "csrf_token": nonce},
        )
        if cookie is not None:
            _set_csrf(response, cookie)
        return response

    return app


def reclassify_stored_jobs(store: Store, settings: Settings) -> None:
    for stored in store.list_all_jobs():
        job = _job_from_stored(stored)
        annotate(job, settings)
        store.update_classification(stored, job)


def _current_settings(app: FastAPI) -> Settings:
    stored = app.state.store.load_or_seed(app.state.bootstrap)
    return merge_registered_sources(stored, app.state.registry)


_SHOW_WINDOWS = {"24h", "3d"}


def _matching_jobs(
    store: Store,
    eligibility: str,
    source: str,
    query: str,
    application: str,
    posted: str,
    timezone_name: str,
) -> list[StoredJob]:
    now = utc_now()
    store.delete_expired_jobs(now)
    return store.list_jobs(
        eligibility=None if eligibility == "all" else eligibility,
        source=source or None,
        query=query or None,
        application=None if application == "all" else application,
        since=_posted_since(posted, now, timezone_name),
        limit=500,
    )


def _posted_since(posted: str, now: datetime, timezone_name: str) -> datetime:
    if posted == "24h":
        return now - timedelta(hours=24)
    zone = ZoneInfo(timezone_name)
    local_now = now.astimezone(zone)
    start = datetime.combine(local_now.date() - timedelta(days=3), time.min, tzinfo=zone)
    return start.astimezone(UTC)


def _filters(request: Request) -> tuple[str, str, str, str, str]:
    eligibility = request.query_params.get("eligibility") or "accepted"
    if eligibility not in {"accepted", "rejected", "maybe", "all"}:
        eligibility = "accepted"
    application = request.query_params.get("application") or "all"
    if application not in {"all", "direct", "job_board"}:
        application = "all"
    posted = request.query_params.get("posted") or "24h"
    if posted not in _SHOW_WINDOWS:
        posted = "24h"
    return (
        eligibility,
        request.query_params.get("source") or "",
        request.query_params.get("q") or "",
        application,
        posted,
    )


def running_sources(app: FastAPI) -> list[str]:
    with app.state.running_guard:
        return [name for name, thread in app.state.running.items() if thread.is_alive()]


def scheduler_tick(app: FastAPI, *, now: datetime | None = None) -> threading.Thread | None:
    """Start the first enabled source that is due and idle.

    One start per tick spaces sources TICK_SECONDS apart, so a restart with
    several overdue sources never sends one burst to every site.
    """
    settings = _current_settings(app)
    registry = app.state.registry
    due = due_sources(settings, app.state.store, frozenset(registry), now or utc_now())
    busy = set(running_sources(app))
    for name in registry:
        if name in due and name not in busy:
            return _start_source(app, name)
    return None


def _start_source(app: FastAPI, name: str) -> threading.Thread:
    thread = threading.Thread(
        target=_collect_source,
        args=(app, name),
        name=f"latam-jobs-{name}",
        daemon=True,
    )
    with app.state.running_guard:
        app.state.running[name] = thread
    thread.start()
    return thread


def _collect_source(app: FastAPI, name: str) -> None:
    try:
        collect(
            _current_settings(app),
            registry=app.state.registry,
            include=frozenset({name}),
            honor_backoff=True,
        )
    except Exception:
        logger.exception("background check of %s failed", name)
    finally:
        with app.state.running_guard:
            app.state.running.pop(name, None)
            app.state.collect_finished += 1


def start_scheduler(
    app: FastAPI,
    *,
    first_delay_seconds: float = STARTUP_DELAY_SECONDS,
    tick_seconds: float = TICK_SECONDS,
) -> threading.Event:
    """Run every source on its own schedule while the server is up.

    The server holds the run lock for its whole lifetime, so `latam-jobs run`
    never collects at the same time.
    """
    stop = threading.Event()
    lock = RunLock(run_lock_path(Path(_current_settings(app).database.path)))
    app.state.first_check_at = utc_now() + timedelta(seconds=first_delay_seconds)
    app.state.scheduler_state = "starting"
    held = _try_lock(app, lock)

    def loop() -> None:
        nonlocal held
        try:
            if stop.wait(first_delay_seconds):
                return
            while True:
                held = held or _try_lock(app, lock)
                if held:
                    app.state.scheduler_state = "on"
                    try:
                        scheduler_tick(app)
                    except Exception:
                        logger.exception("scheduler tick failed")
                if stop.wait(tick_seconds):
                    return
        finally:
            if held:
                lock.release()
            app.state.scheduler_state = "off"

    threading.Thread(target=loop, name="latam-jobs-scheduler", daemon=True).start()
    return stop


def _try_lock(app: FastAPI, lock: RunLock) -> bool:
    try:
        lock.acquire()
    except LockError:
        logger.warning("another collector run holds %s; background search waits", lock.path)
        app.state.scheduler_state = "waiting"
        return False
    return True


def _summary_response(request: Request, app: FastAPI) -> Response:
    secret = app.state.secret
    nonce, cookie = _csrf_pair(request, secret)
    context = _status_context(app)
    context["csrf_token"] = nonce
    response = _templates.TemplateResponse(request, "status_summary.html", context)
    if cookie is not None:
        _set_csrf(response, cookie)
    if app.state.collect_finished != app.state.collect_announced:
        app.state.collect_announced = app.state.collect_finished
        response.headers["HX-Trigger"] = "refreshJobs"
    return response


def _clock(app: FastAPI) -> dict[str, Any]:
    return {"display_timezone": _current_settings(app).display_timezone, "now": utc_now()}


def _status_context(app: FastAPI) -> dict[str, Any]:
    settings = _current_settings(app)
    store: Store = app.state.store
    now = utc_now()
    zone = settings.display_timezone
    finished = [
        schedule
        for schedule in store.list_schedules()
        if schedule.source_name in app.state.registry and schedule.last_attempt_at is not None
    ]
    latest = max(finished, key=lambda schedule: schedule.last_attempt_at or now, default=None)
    if latest is None or latest.last_attempt_at is None:
        last_finished = "Never"
    else:
        last_finished = f"{format_when(latest.last_attempt_at, zone, now)}, {latest.source_name}"
    alerts = _DASHBOARD.unseen(store)
    return {
        "search_status": _search_status(app, zone, now),
        "last_finished": last_finished,
        "new_today": store.count_new_accepted(now - timedelta(hours=24)),
        "alerts": alerts,
    }


def _search_status(app: FastAPI, zone: str, now: datetime) -> str:
    state = app.state.scheduler_state
    running = running_sources(app)
    if running:
        return f"Background search is on. Checking now: {', '.join(running)}."
    if state == "off":
        return "Background search runs while the server is up."
    if state == "waiting":
        return "Background search is waiting for another collector run to finish."
    first = app.state.first_check_at
    if state == "starting" and first is not None and first > now:
        return f"Background search is on. First check at {format_clock(first, zone)}."
    return "Background search is on."


def _settings_from_form(current: Settings, form: Any, registry: dict[str, Any]) -> Settings:
    sources = {}
    for name, source in current.sources.items():
        if form.get(f"source-present-{name}") != "1":
            sources[name] = source
            continue
        raw_interval = str(form.get(f"interval-{name}") or source.interval_hours)
        interval = int(raw_interval)
        minimum = SAFE_MIN_INTERVAL_HOURS.get(name, 1)
        if interval < minimum:
            unit = "hour" if minimum == 1 else "hours"
            raise _IntervalTooShort(f"{name} needs at least {minimum} {unit} between checks.")
        sources[name] = source.model_copy(
            update={
                "enabled": form.get(f"enabled-{name}") == "on",
                "interval_hours": interval,
            }
        )
    for name in registry:
        if name not in sources:
            sources[name] = current.sources[name]
    timezone = str(form.get("display_timezone") or current.display_timezone)
    draft = current.model_copy(
        update={
            "residence_country": str(form.get("residence_country") or ""),
            "display_timezone": timezone,
            "remote_only": form.get("remote_only") == "on",
            "keywords": _keyword_list(str(form.get("keywords") or "")),
            "exclude_keywords": _keyword_list(str(form.get("exclude_keywords") or "")),
            "notifications": current.notifications.model_copy(
                update={"enabled": form.get("notifications-enabled") == "on"}
            ),
            "sources": sources,
        }
    )
    return Settings.model_validate(draft.model_dump())


def _keyword_list(value: str) -> list[str]:
    cleaned = value.replace(",", "\n")
    return [item.strip() for item in cleaned.splitlines() if item.strip()]


def _page(request: Request, secret: str, template: str, context: dict[str, Any]) -> Response:
    nonce, cookie = _csrf_pair(request, secret)
    context["csrf_token"] = nonce
    response = _templates.TemplateResponse(request, template, context)
    if cookie is not None:
        _set_csrf(response, cookie)
    return response


def _login_response(
    request: Request,
    secret: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    nonce, cookie = _csrf_pair(request, secret)
    context["csrf_token"] = nonce
    context["login_page"] = True
    response = _templates.TemplateResponse(request, "login.html", context, status_code=status_code)
    if cookie is not None:
        _set_csrf(response, cookie)
    return response


def _settings_response(
    request: Request,
    secret: str,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    nonce, cookie = _csrf_pair(request, secret)
    context["csrf_token"] = nonce
    response = _templates.TemplateResponse(
        request, "settings.html", context, status_code=status_code
    )
    if cookie is not None:
        _set_csrf(response, cookie)
    return response


def _set_csrf(response: Response, cookie: str) -> None:
    response.set_cookie(
        _COOKIE,
        cookie,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=7 * 24 * 60 * 60,
    )


def _settings_context(
    app: FastAPI,
    settings: Settings,
    *,
    errors: list[str] | None = None,
    saved: bool = False,
) -> dict[str, Any]:
    store: Store = app.state.store
    now = utc_now()
    zone = settings.display_timezone
    runs = {}
    for schedule in store.list_schedules():
        upcoming = max(
            (moment for moment in (schedule.next_fetch_at, schedule.backoff_until) if moment),
            default=None,
        )
        runs[schedule.source_name] = {
            "last": format_when(schedule.last_attempt_at, zone, now),
            "next": "Due now"
            if upcoming is None or upcoming <= now
            else format_clock(upcoming, zone),
            "error": schedule.last_error or "",
        }
    return {
        "title": "Settings",
        "settings": settings,
        "countries": sorted(COUNTRIES, key=lambda country: country.name),
        "errors": errors or [],
        "saved": saved,
        "keywords_text": ", ".join(settings.keywords),
        "exclude_text": ", ".join(settings.exclude_keywords),
        "runs": runs,
        "minimums": SAFE_MIN_INTERVAL_HOURS,
    }


def _job_from_stored(stored: StoredJob) -> Job:
    return Job(
        source=stored.source,
        source_job_id=stored.source_job_id,
        title=stored.title,
        company=stored.company,
        url=stored.url,
        description=stored.description,
        tags=stored.tags,
        location_text=stored.location_text,
        location_countries=stored.location_countries,
        salary=stored.salary,
        published_at=from_iso(stored.published_at),
        remote_status=RemoteStatus(stored.remote_status),
        apply_url=stored.apply_url,
        apply_url_type=ApplyUrlType(stored.apply_url_type),
    )


def _csrf_pair(request: Request, secret: str) -> tuple[str, str | None]:
    cookie = request.cookies.get(_COOKIE)
    if cookie and _signed(secret, cookie):
        return cookie.split(".", 1)[0], None
    nonce = secrets.token_urlsafe(32)
    return nonce, f"{nonce}.{_sign(secret, nonce)}"


def _csrf_matches(secret: str, token: str, cookie: str | None) -> bool:
    if not cookie or not _signed(secret, cookie):
        return False
    nonce = cookie.split(".", 1)[0]
    return hmac.compare_digest(token, nonce)


def _signed(secret: str, cookie: str) -> bool:
    if "." not in cookie:
        return False
    nonce, signature = cookie.split(".", 1)
    expected = _sign(secret, nonce)
    return hmac.compare_digest(signature, expected)


def _sign(secret: str, nonce: str) -> str:
    return hmac.new(secret.encode(), nonce.encode(), hashlib.sha256).hexdigest()
