"""Synchronous HTTP client with timeouts, limited retries, and conditional requests."""

from __future__ import annotations

import logging
import random
import time
from collections.abc import Callable
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit

import httpx

logger = logging.getLogger(__name__)

USER_AGENT = "latam-jobs/0.1 (personal notifier)"
RETRYABLE_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_BACKOFF_SECONDS = 60.0


class HttpError(Exception):
    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class HttpResponse:
    def __init__(self, response: httpx.Response) -> None:
        self.status_code = response.status_code
        self.headers = {key.lower(): value for key, value in response.headers.items()}
        self.content = response.content
        self.text = response.text
        self._response = response

    def json(self) -> object:
        return self._response.json()


class HttpClient:
    def __init__(
        self,
        *,
        timeout: float,
        max_retries: int,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        rng: random.Random | None = None,
    ) -> None:
        self._max_retries = max_retries
        self._sleeper = sleeper
        self._rng = rng or random.Random()
        self._client = httpx.Client(
            timeout=timeout,
            transport=transport,
            headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def get(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpResponse:
        last_transport_error: Exception | None = None
        extra = {} if timeout is None else {"timeout": timeout}
        for attempt in range(self._max_retries + 1):
            started = time.perf_counter()
            try:
                response = self._client.get(url, params=params, headers=headers, **extra)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_transport_error = exc
                logger.info("http GET %s error=%s", _log_target(url), type(exc).__name__)
                if attempt >= self._max_retries:
                    raise HttpError(f"Request failed for {_log_target(url)}: {exc}") from exc
                self._sleeper(_delay_seconds(attempt, None, self._rng))
                continue
            elapsed_ms = int((time.perf_counter() - started) * 1000)
            logger.info(
                "http GET %s status=%s elapsed_ms=%s",
                _log_target(str(response.request.url)),
                response.status_code,
                elapsed_ms,
            )
            if response.status_code == 304 or response.status_code < 400:
                return HttpResponse(response)
            if response.status_code in RETRYABLE_STATUSES and attempt < self._max_retries:
                self._sleeper(_delay_seconds(attempt, response, self._rng))
                continue
            raise HttpError(
                f"HTTP {response.status_code} for {_log_target(str(response.request.url))}",
                status_code=response.status_code,
            )
        raise HttpError(f"Request failed for {_log_target(url)}: {last_transport_error}")


def _delay_seconds(attempt: int, response: httpx.Response | None, rng: random.Random) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            parsed = _parse_retry_after(retry_after)
            if parsed is not None:
                return min(MAX_BACKOFF_SECONDS, parsed)
    base = min(MAX_BACKOFF_SECONDS, 0.5 * (2**attempt))
    return min(MAX_BACKOFF_SECONDS, base + rng.uniform(0, base / 2))


def _parse_retry_after(value: str) -> float | None:
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        moment = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return max(0.0, (moment - datetime.now(UTC)).total_seconds())


def _log_target(url: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
