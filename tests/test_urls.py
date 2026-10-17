from latam_jobs.models import ApplyUrlType
from latam_jobs.urls import canonical_url, classify_apply_link


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


def test_classify_apply_link_requires_a_different_http_host() -> None:
    listing = "https://remoteok.com/remote-jobs/example"
    direct, kind = classify_apply_link(listing, "https://boards.greenhouse.io/acme/jobs/1")
    assert kind is ApplyUrlType.DIRECT
    assert direct == "https://boards.greenhouse.io/acme/jobs/1"

    same, same_kind = classify_apply_link(listing, listing)
    assert same is None
    assert same_kind is ApplyUrlType.JOB_BOARD

    missing, missing_kind = classify_apply_link(listing, None)
    assert missing is None
    assert missing_kind is ApplyUrlType.JOB_BOARD

    invalid, invalid_kind = classify_apply_link(listing, "javascript:alert(1)")
    assert invalid is None
    assert invalid_kind is ApplyUrlType.JOB_BOARD

    duplicate, duplicate_kind = classify_apply_link(
        "https://www.remoteok.com/jobs/1", "https://remoteok.com/jobs/1"
    )
    assert duplicate is None
    assert duplicate_kind is ApplyUrlType.JOB_BOARD
