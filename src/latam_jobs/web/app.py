"""Local FastAPI dashboard for stored jobs and collection settings."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from latam_jobs.geo import COUNTRIES
from latam_jobs.models import Job, RemoteStatus
from latam_jobs.pipeline import annotate
from latam_jobs.scheduler import from_iso
from latam_jobs.settings import Settings, merge_registered_sources
from latam_jobs.store import Store, StoredJob

_PACKAGE = Path(__file__).parent
_templates = Jinja2Templates(directory=str(_PACKAGE / "templates"))
_COOKIE = "latam_jobs_csrf"


def create_app(
    *,
    bootstrap: Settings,
    store: Store,
    registry: dict[str, Any],
    secret: str,
) -> FastAPI:
    app = FastAPI(title="LATAM jobs", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=str(_PACKAGE / "static")), name="static")
    app.state.bootstrap = bootstrap
    app.state.store = store
    app.state.registry = registry
    app.state.secret = secret

    @app.get("/", response_class=HTMLResponse)
    def jobs_page(request: Request) -> Response:
        eligibility, source, query = _filters(request)
        response = _templates.TemplateResponse(
            request,
            "jobs.html",
            {
                "title": "Jobs",
                "jobs": _matching_jobs(store, eligibility, source, query),
                "eligibility": eligibility,
                "source": source,
                "query": query,
                "sources": list(registry),
            },
        )
        return response

    @app.get("/jobs/rows", response_class=HTMLResponse)
    def job_rows(request: Request) -> Response:
        eligibility, source, query = _filters(request)
        return _templates.TemplateResponse(
            request,
            "jobs_rows.html",
            {"jobs": _matching_jobs(store, eligibility, source, query)},
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

    @app.get("/status", response_class=HTMLResponse)
    def status_page(request: Request) -> Response:
        rows = []
        for name in registry:
            schedule = store.get_schedule(name)
            rows.append(
                {
                    "name": name,
                    "last_success": None if schedule is None else schedule.last_success_at,
                    "next_fetch": None if schedule is None else schedule.next_fetch_at,
                    "status": None if schedule is None else schedule.last_http_status,
                    "error": None if schedule is None else schedule.last_error,
                    "ran": schedule is not None,
                }
            )
        return _templates.TemplateResponse(
            request,
            "status.html",
            {"title": "Status", "rows": rows},
        )

    return app


def reclassify_stored_jobs(store: Store, settings: Settings) -> None:
    for stored in store.list_all_jobs():
        job = _job_from_stored(stored)
        annotate(job, settings)
        store.update_classification(stored, job)


def _current_settings(app: FastAPI) -> Settings:
    stored = app.state.store.load_or_seed(app.state.bootstrap)
    return merge_registered_sources(stored, app.state.registry)


def _matching_jobs(store: Store, eligibility: str, source: str, query: str) -> list[StoredJob]:
    return store.list_jobs(
        eligibility=None if eligibility == "all" else eligibility,
        source=source or None,
        query=query or None,
        limit=200,
    )


def _filters(request: Request) -> tuple[str, str, str]:
    eligibility = request.query_params.get("eligibility") or "accepted"
    if eligibility not in {"accepted", "rejected", "maybe", "all"}:
        eligibility = "accepted"
    return (
        eligibility,
        request.query_params.get("source") or "",
        request.query_params.get("q") or "",
    )


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
    draft = current.model_copy(
        update={
            "residence_country": str(form.get("residence_country") or ""),
            "remote_only": form.get("remote_only") == "on",
            "keywords": _keyword_list(str(form.get("keywords") or "")),
            "exclude_keywords": _keyword_list(str(form.get("exclude_keywords") or "")),
            "sources": sources,
        }
    )
    return Settings.model_validate(draft.model_dump())


def _keyword_list(value: str) -> list[str]:
    cleaned = value.replace(",", "\n")
    return [item.strip() for item in cleaned.splitlines() if item.strip()]


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
        response.set_cookie(
            _COOKIE,
            cookie,
            httponly=True,
            samesite="lax",
            path="/",
            max_age=7 * 24 * 60 * 60,
        )
    return response


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
