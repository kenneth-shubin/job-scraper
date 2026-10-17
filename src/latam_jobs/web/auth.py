"""Signed dashboard sessions. The password is never stored or placed in a cookie."""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import UTC, datetime, timedelta

SESSION_COOKIE = "latam_jobs_session"
SESSION_SECONDS = 7 * 24 * 60 * 60
_COOLDOWN_CAP = 15 * 60


def passwords_match(provided: str, expected: str) -> bool:
    return hmac.compare_digest(
        hashlib.sha256(provided.encode()).digest(),
        hashlib.sha256(expected.encode()).digest(),
    )


def issue_session(secret: str, *, now: datetime | None = None) -> str:
    moment = now or datetime.now(UTC)
    expires = int(moment.timestamp()) + SESSION_SECONDS
    nonce = secrets.token_urlsafe(18)
    payload = f"{expires}.{nonce}"
    return f"{payload}.{_sign(secret, payload)}"


def session_valid(secret: str, cookie: str | None, *, now: datetime | None = None) -> bool:
    if not cookie:
        return False
    parts = cookie.split(".")
    if len(parts) != 3:
        return False
    expires_text, nonce, signature = parts
    payload = f"{expires_text}.{nonce}"
    if not hmac.compare_digest(signature, _sign(secret, payload)):
        return False
    try:
        expires = int(expires_text)
    except ValueError:
        return False
    moment = now or datetime.now(UTC)
    return moment.timestamp() < expires


def cooldown_active(failures: list[datetime], now: datetime) -> bool:
    if len(failures) < 5:
        return False
    delay = min(30 * (2 ** (len(failures) - 5)), _COOLDOWN_CAP)
    return now < max(failures) + timedelta(seconds=delay)


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
