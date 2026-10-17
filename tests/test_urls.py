from latam_jobs.models import ApplyUrlType, Job
from latam_jobs.urls import attach_direct_apply, canonical_url, classify_apply_link, direct_ats_url


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


def test_direct_ats_url_requires_one_opening() -> None:
    assert (
        direct_ats_url("https://boards.greenhouse.io/acme/jobs/42")
        == "https://boards.greenhouse.io/acme/jobs/42"
    )
    assert (
        direct_ats_url("https://jobs.lever.co/figma/abc/apply")
        == "https://jobs.lever.co/figma/abc/apply"
    )
    assert (
        direct_ats_url("https://jobs.ashbyhq.com/linear/job-1/application")
        == "https://jobs.ashbyhq.com/linear/job-1/application"
    )
    assert direct_ats_url("https://boards.greenhouse.io/acme") is None
    assert direct_ats_url("https://jobs.lever.co/spotify") is None
    assert direct_ats_url("https://api.ashbyhq.com/posting-api/job-board/ramp") is None


def test_attach_direct_apply_uses_the_opening_link_from_the_listing() -> None:
    job = Job(
        source="jobicy",
        source_job_id="1",
        title="Senior Rust Engineer",
        company="Acme",
        url="https://jobicy.com/jobs/1",
        description="Apply at https://job-boards.greenhouse.io/acme/jobs/42 today.",
        location_text="Worldwide",
    )
    attach_direct_apply(job)
    assert job.apply_url == "https://job-boards.greenhouse.io/acme/jobs/42"
    assert job.apply_url_type is ApplyUrlType.DIRECT

    listing = Job(
        source="greenhouse",
        source_job_id="acme:42",
        title="Senior Rust Engineer",
        company="Acme",
        url="https://job-boards.greenhouse.io/acme/jobs/42",
        description="The posting itself.",
        location_text="Remote",
    )
    attach_direct_apply(listing)
    assert listing.apply_url is None
