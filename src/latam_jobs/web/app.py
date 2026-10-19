"""Local FastAPI dashboard for stored jobs and collection settings."""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import threading
from collections.abc import Callable
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from latam_jobs.geo import COUNTRIES
from latam_jobs.locations import location_label
from latam_jobs.models import ApplyUrlType, Job, RemoteStatus
from latam_jobs.pipeline import BOARD_SOURCES, annotate, collect, due_sources
from latam_jobs.scheduler import (
    LockError,
    RunLock,
    board_lock_path,
    earliest_next_fetch,
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
_COOKIE = "latam_jobs_csrf"
_OPEN_PATHS = {"/login", "/logout"}


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
    app.state.collector = threading.Lock()
    app.state.collector_thread = None
    app.state.collector_message = ""
    app.state.board_collector = threading.Lock()
    app.state.board_thread = None
    app.state.board_message = ""
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
            _settings_context(current, saved=request.query_params.get("saved") == "1"),
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
            errors = (
                [err["msg"] for err in exc.errors()]
                if isinstance(exc, ValidationError)
                else ["Interval hours must be a whole number from 1 to 168."]
            )
            return _settings_response(
                request,
                secret,
                _settings_context(current, errors=errors),
                status_code=400,
            )
        store.save_settings(updated, bootstrap=bootstrap)
        reclassify_stored_jobs(store, updated)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/status")
    def status_page() -> Response:
        return RedirectResponse("/", status_code=303)

    @app.post("/jobs/run")
    async def run_collection(request: Request) -> Response:
        form = await request.form()
        token = str(form.get("csrf_token") or "")
        if not _csrf_matches(secret, token, request.cookies.get(_COOKIE)):
            return HTMLResponse("Invalid CSRF token.", status_code=403)
        message = _start_collection(app)
        return _summary_response(request, app, message=message)

    @app.get("/status/summary", response_class=HTMLResponse)
    def status_summary(request: Request) -> Response:
        return _summary_response(request, app)

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


def _start_collection(app: FastAPI) -> str:
    """Run the job-board aggregators now. Company career pages stay on their own schedule."""
    quick = frozenset(name for name in app.state.registry if name not in BOARD_SOURCES)
    if not _start_quick(app, quick, ignore_schedule=True):
        return "A check is already running."
    return "Checking for jobs."


def _start_quick(app: FastAPI, include: frozenset[str], *, ignore_schedule: bool) -> bool:
    if not app.state.collector.acquire(blocking=False):
        return False
    app.state.collector_message = "Checking for jobs."

    def finish(message: str) -> None:
        app.state.collector_message = message
        app.state.collect_finished += 1
        app.state.collector.release()

    thread = threading.Thread(
        target=_collect_in_lane,
        args=(app, include, ignore_schedule, run_lock_path, finish),
        name="latam-jobs-run",
        daemon=True,
    )
    app.state.collector_thread = thread
    thread.start()
    return True


def _start_boards(app: FastAPI, include: frozenset[str]) -> bool:
    if not app.state.board_collector.acquire(blocking=False):
        return False
    app.state.board_message = "Checking company career pages in the background."

    def finish(message: str) -> None:
        app.state.board_message = message
        app.state.collect_finished += 1
        app.state.board_collector.release()

    thread = threading.Thread(
        target=_collect_in_lane,
        args=(app, include, False, board_lock_path, finish),
        name="latam-jobs-boards",
        daemon=True,
    )
    app.state.board_thread = thread
    thread.start()
    return True


def _collect_in_lane(
    app: FastAPI,
    include: frozenset[str],
    ignore_schedule: bool,
    lock_path: Callable[[Path], Path],
    finish: Callable[[str], None],
) -> None:
    message = ""
    try:
        settings = _current_settings(app)
        lock = RunLock(lock_path(Path(settings.database.path)))
        try:
            lock.acquire()
        except LockError:
            message = "A check is already running."
            return
        try:
            collect(
                settings,
                registry=app.state.registry,
                ignore_schedule=ignore_schedule,
                include=include,
            )
        finally:
            lock.release()
    except Exception:
        logger.exception("dashboard collection failed")
        message = "The check failed. See the server log."
    finally:
        finish(message)


def scheduled_check(app: FastAPI) -> None:
    """Start every source that is due. Company career pages run on their own lane."""
    settings = _current_settings(app)
    store: Store = app.state.store
    names = frozenset(app.state.registry)
    now = utc_now()
    quick = due_sources(settings, store, names - BOARD_SOURCES, now)
    boards = due_sources(settings, store, names & BOARD_SOURCES, now)
    if quick:
        _start_quick(app, quick, ignore_schedule=False)
    if boards:
        _start_boards(app, boards)


def start_scheduler(app: FastAPI, *, first_delay_seconds: float = 120.0) -> threading.Event:
    """Check due sources in the background every ``scheduler.check_interval_minutes``."""
    stop = threading.Event()

    def loop() -> None:
        if stop.wait(first_delay_seconds):
            return
        while True:
            try:
                scheduled_check(app)
            except Exception:
                logger.exception("scheduled check failed")
            minutes = _current_settings(app).scheduler.check_interval_minutes
            if stop.wait(minutes * 60):
                return

    threading.Thread(target=loop, name="latam-jobs-scheduler", daemon=True).start()
    return stop


def _summary_response(request: Request, app: FastAPI, *, message: str | None = None) -> Response:
    secret = app.state.secret
    nonce, cookie = _csrf_pair(request, secret)
    context = _status_context(app)
    context["csrf_token"] = nonce
    if message is not None:
        context["run_message"] = message
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
    latest = store.latest_run()
    now = utc_now()
    boards = {
        name
        for name, source in settings.sources.items()
        if name in BOARD_SOURCES and name in app.state.registry and source.enabled
    }
    if app.state.board_collector.locked():
        board_status = app.state.board_message
    elif boards:
        upcoming = earliest_next_fetch(store.list_schedules(), boards)
        if upcoming is None or upcoming <= now:
            board_status = "Company career pages: due at the next automatic check."
        else:
            clock = format_clock(upcoming, settings.display_timezone)
            board_status = f"Company career pages: next automatic check {clock}."
        if app.state.board_message:
            board_status = f"{app.state.board_message} {board_status}"
    else:
        board_status = ""
    return {
        "last_checked": format_when(
            None if latest is None else latest.finished_at,
            settings.display_timezone,
            now,
        ),
        "new_jobs": 0 if latest is None else latest.new_jobs,
        "run_message": app.state.collector_message,
        "board_status": board_status,
    }


def _settings_from_form(current: Settings, form: Any, registry: dict[str, Any]) -> Settings:
    sources = {}
    for name, source in current.sources.items():
        if form.get(f"source-present-{name}") != "1":
            sources[name] = source
            continue
        raw_interval = str(form.get(f"interval-{name}") or source.interval_hours)
        sources[name] = source.model_copy(
            update={
                "enabled": form.get(f"enabled-{name}") == "on",
                "interval_hours": int(raw_interval),
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
    settings: Settings,
    *,
    errors: list[str] | None = None,
    saved: bool = False,
) -> dict[str, Any]:
    return {
        "title": "Settings",
        "settings": settings,
        "countries": sorted(COUNTRIES, key=lambda country: country.name),
        "errors": errors or [],
        "saved": saved,
        "keywords_text": ", ".join(settings.keywords),
        "exclude_text": ", ".join(settings.exclude_keywords),
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
