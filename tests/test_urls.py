from latam_jobs.urls import canonical_url


def test_strips_tracking_parameters_and_sorts_query() -> None:
    left = canonical_url(
        "HTTPS://Example.com/jobs/1/?utm_source=newsletter&ref=abc&b=2&a=1#section"
    )
    right = canonical_url("https://example.com/jobs/1?a=1&b=2&gh_src=remotive")
    assert left == "https://example.com/jobs/1?a=1&b=2"
    assert left == right


def test_distinct_application_urls_stay_distinct() -> None:
    first = canonical_url("https://example.com/jobs/1")
    second = canonical_url("https://example.com/jobs/2")
    assert first != second
