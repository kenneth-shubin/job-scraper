from pathlib import Path

import yaml

from latam_jobs.eligibility import classify_eligibility, resolve_remote_status
from latam_jobs.models import Job, RemoteStatus
from latam_jobs.settings import settings_from_mapping

CASES = Path("tests/fixtures/eligibility_cases.yaml")


def _job(case: dict[str, object]) -> Job:
    remote_status = case.get("remote_status", RemoteStatus.UNKNOWN)
    return Job(
        source="fixture",
        source_job_id=str(case["id"]),
        title=str(case["title"]),
        company="Example",
        url="https://example.com/jobs/1",
        description=str(case.get("description") or ""),
        location_text=str(case.get("location_text") or ""),
        location_countries=tuple(case.get("location_countries") or ()),
        remote_status=RemoteStatus(str(remote_status)),
    )


def test_eligibility_fixtures() -> None:
    cases = yaml.safe_load(CASES.read_text(encoding="utf-8"))
    assert len(cases) >= 25
    for case in cases:
        settings = settings_from_mapping({"residence_country": case["residence_country"]})
        job = _job(case)
        eligibility, reason = classify_eligibility(job, settings)
        assert eligibility.value == case["expect_eligibility"], case["id"]
        assert reason == case["expect_reason"], case["id"]


def test_remote_status_detection() -> None:
    remote = Job(
        source="fixture",
        source_job_id="remote",
        title="Engineer",
        company="Example",
        url="https://example.com/jobs/1",
        description="Fully remote.",
        location_text="Worldwide",
    )
    hybrid = Job(
        source="fixture",
        source_job_id="hybrid",
        title="Engineer",
        company="Example",
        url="https://example.com/jobs/2",
        description="Remote or hybrid.",
        location_text="Worldwide",
    )
    assert resolve_remote_status(remote) is RemoteStatus.REMOTE
    assert resolve_remote_status(hybrid) is RemoteStatus.HYBRID
