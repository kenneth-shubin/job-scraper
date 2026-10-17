import argparse
import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from latam_jobs.cli import _cmd_serve
from latam_jobs.settings import ConfigError, DatabaseSettings, Settings
from latam_jobs.sources.remotive import RemotiveSource
from latam_jobs.store import Store
from latam_jobs.web.app import create_app
from latam_jobs.web.auth import cooldown_active, issue_session, session_valid

PASSWORD = "test-password"
SECRET = "test-secret"


def _client(
    tmp_path: Path, *, secret: str = SECRET, password: str = PASSWORD
) -> tuple[TestClient, Store]:
    database = tmp_path / "jobs.sqlite"
    bootstrap = Settings(
        database=DatabaseSettings(path=str(database)),
        residence_country="colombia",
        keywords=["backend"],
    )
    store = Store(database)
    store.migrate()
    store.load_or_seed(bootstrap)
    app = create_app(
        bootstrap=bootstrap,
        store=store,
        registry={"remotive": RemotiveSource()},
        secret=secret,
        password=password,
    )
    return TestClient(app), store


def _token(client: TestClient) -> str:
    page = client.get("/login")
    match = re.search(r'name="csrf_token" value="([^"]+)"', page.text)
    assert match is not None
    return match.group(1)


def test_missing_secrets_stop_serve(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = tmp_path / "jobs.sqlite"
    bootstrap = Settings(database=DatabaseSettings(path=str(database)))
    store = Store(database)
    store.migrate()
    with pytest.raises(ConfigError):
        create_app(
            bootstrap=bootstrap,
            store=store,
            registry={},
            secret="",
            password=PASSWORD,
        )
    with pytest.raises(ConfigError):
        create_app(
            bootstrap=bootstrap,
            store=store,
            registry={},
            secret=SECRET,
            password="",
        )
    store.close()

    monkeypatch.setenv("WEB_SECRET", SECRET)
    monkeypatch.delenv("DASHBOARD_PASSWORD", raising=False)
    assert _cmd_serve(argparse.Namespace(config=Path("settings.yaml"))) == 2


def test_login_logout_and_session_cookie(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    client, store = _client(tmp_path)
    anonymous = client.get("/", follow_redirects=False)
    assert anonymous.status_code == 303
    assert anonymous.headers["location"] == "/login"

    denied = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)
    assert denied.status_code == 403
    assert "Invalid CSRF token." in denied.text

    with caplog.at_level(logging.DEBUG):
        wrong = client.post(
            "/login",
            data={"csrf_token": _token(client), "password": "nope"},
            follow_redirects=False,
        )
    assert wrong.status_code == 401
    assert "Incorrect password. Please try again." in wrong.text
    assert PASSWORD not in caplog.text

    signed_in = client.post(
        "/login",
        data={"csrf_token": _token(client), "password": PASSWORD},
        follow_redirects=False,
    )
    assert signed_in.status_code == 303
    assert signed_in.headers["location"] == "/"
    cookie = signed_in.headers["set-cookie"]
    assert PASSWORD not in cookie
    assert "HttpOnly" in cookie
    assert "samesite=lax" in cookie.lower()
    assert "Secure" not in cookie
    assert client.get("/").status_code == 200

    home = client.get("/")
    match = re.search(r'name="csrf_token" value="([^"]+)"', home.text)
    assert match is not None
    logged_out = client.post("/logout", data={"csrf_token": match.group(1)}, follow_redirects=False)
    assert logged_out.status_code == 303
    assert logged_out.headers["location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 303
    store.close()


def test_htmx_and_settings_require_a_session(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    rows = client.get("/jobs/rows", headers={"HX-Request": "true"}, follow_redirects=False)
    assert rows.status_code == 200
    assert rows.headers["hx-redirect"] == "/login"
    assert rows.text == ""
    settings = client.post(
        "/settings",
        data={"residence_country": "brazil", "keywords": "django"},
        follow_redirects=False,
    )
    assert settings.status_code == 303
    assert settings.headers["location"] == "/login"
    assert (
        store.load_or_seed(
            Settings(database=DatabaseSettings(path=str(store.path)))
        ).residence_country
        == "colombia"
    )
    store.close()


def test_session_follows_the_signing_secret(tmp_path: Path) -> None:
    first, store = _client(tmp_path)
    first.post(
        "/login",
        data={"csrf_token": _token(first), "password": PASSWORD},
        follow_redirects=False,
    )
    cookie = first.cookies.get("latam_jobs_session")
    assert cookie is not None
    assert session_valid(SECRET, cookie) is True
    assert session_valid("other-secret", cookie) is False

    second, _other = _client(tmp_path, secret=SECRET)
    second.cookies.set("latam_jobs_session", cookie)
    assert second.get("/").status_code == 200

    changed, _changed_store = _client(tmp_path, secret="other-secret")
    changed.cookies.set("latam_jobs_session", cookie)
    assert changed.get("/", follow_redirects=False).status_code == 303

    expired = issue_session(SECRET, now=datetime(2020, 1, 1, tzinfo=UTC))
    assert session_valid(SECRET, expired, now=datetime(2026, 10, 17, tzinfo=UTC)) is False
    first.cookies.set("latam_jobs_session", expired)
    assert first.get("/", follow_redirects=False).status_code == 303
    store.close()


def test_login_cooldown_after_repeated_failures(tmp_path: Path) -> None:
    client, store = _client(tmp_path)
    token = _token(client)
    for _ in range(5):
        response = client.post(
            "/login",
            data={"csrf_token": token, "password": "nope"},
            follow_redirects=False,
        )
        assert response.status_code == 401
    blocked = client.post(
        "/login",
        data={"csrf_token": token, "password": PASSWORD},
        follow_redirects=False,
    )
    assert blocked.status_code == 401
    assert "Incorrect password. Please try again." in blocked.text
    assert client.get("/", follow_redirects=False).status_code == 303
    failures = store._conn.execute(
        "SELECT COUNT(*) AS count FROM login_attempts WHERE succeeded = 0"
    ).fetchone()
    assert int(failures["count"]) == 5
    store.close()


def test_cooldown_grows_and_caps() -> None:
    now = datetime(2026, 10, 17, 15, 0, tzinfo=UTC)
    recent = now - timedelta(seconds=10)
    assert cooldown_active([recent] * 4, now) is False
    assert cooldown_active([recent] * 5, now) is True
    assert cooldown_active([recent] * 5, now + timedelta(seconds=31)) is False
    assert cooldown_active([now] * 6, now + timedelta(seconds=59)) is True
    assert cooldown_active([now] * 12, now + timedelta(seconds=899)) is True
    assert cooldown_active([now] * 12, now + timedelta(seconds=901)) is False
