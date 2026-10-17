from datetime import UTC, datetime

import pytest

from latam_jobs.models import DESCRIPTION_LIMIT, Job, RemoteStatus
from latam_jobs.sources.base import FetchContext, FetchResult, SourceSnapshot


def test_job_requires_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        Job(
            source="remotive",
            source_job_id="1",
            title="Backend Engineer",
            company="Example",
            url="https://example.com/jobs/1",
            description="Remote",
            location_text="Worldwide",
            published_at=datetime(2026, 1, 1, 12, 0, 0),
        )


def test_job_accepts_utc_timestamp_and_normalizes_sequences() -> None:
    job = Job(
        source="remotive",
        source_job_id="1",
        title="Backend Engineer",
        company="Example",
        url="https://example.com/jobs/1",
        description="x" * (DESCRIPTION_LIMIT + 10),
        location_text="Colombia",
        tags=["python", "django"],
        location_countries=["colombia"],
        published_at=datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        remote_status=RemoteStatus.REMOTE,
    )
    assert len(job.description) == DESCRIPTION_LIMIT
    assert job.tags == ("python", "django")
    assert job.location_countries == ("colombia",)
    assert job.published_at is not None
    assert job.published_at.tzinfo is UTC


def test_fetch_result_defaults() -> None:
    result = FetchResult()
    assert result.jobs == []
    assert result.not_modified is False
    context = FetchContext(
        client=None,  # type: ignore[arg-type]
        settings=None,  # type: ignore[arg-type]
        state=SourceSnapshot(etag="abc"),
    )
    assert context.state.etag == "abc"
