import httpx

from latam_jobs.http import HttpClient, HttpError


def test_retries_transient_500_then_succeeds() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) < 3:
            return httpx.Response(500, json={"error": "temporary"})
        return httpx.Response(200, json={"ok": True})

    client = HttpClient(
        timeout=5,
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    response = client.get("https://example.com/jobs")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert len(calls) == 3
    client.close()


def test_does_not_retry_403() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(403, json={"error": "forbidden"})

    client = HttpClient(
        timeout=5,
        max_retries=2,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    try:
        client.get("https://example.com/jobs")
    except HttpError as exc:
        assert exc.status_code == 403
    else:
        raise AssertionError("expected HTTP 403")
    assert calls == [1]
    client.close()


def test_timeout_is_retried_then_reported() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        raise httpx.ReadTimeout("timed out", request=request)

    client = HttpClient(
        timeout=5,
        max_retries=1,
        transport=httpx.MockTransport(handler),
        sleeper=lambda _seconds: None,
    )
    try:
        client.get("https://example.com/jobs")
    except HttpError as exc:
        assert exc.status_code is None
        assert "timed out" in str(exc)
    else:
        raise AssertionError("expected timeout")
    assert calls == [1, 1]
    client.close()
