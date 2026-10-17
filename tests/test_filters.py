from latam_jobs.filters import match_keywords
from latam_jobs.models import Job, KeywordStatus
from latam_jobs.pipeline import annotate
from latam_jobs.settings import settings_from_mapping


def _job(title: str, description: str = "", tags: tuple[str, ...] = ()) -> Job:
    return Job(
        source="fixture",
        source_job_id="1",
        title=title,
        company="Example",
        url="https://example.com/jobs/1",
        description=description,
        location_text="Worldwide",
        tags=tags,
    )


def test_inclusion_is_or_and_case_insensitive() -> None:
    settings = settings_from_mapping({"keywords": ["python", "react"]})
    status, reason = match_keywords(_job("React Developer"), settings)
    assert status is KeywordStatus.MATCHED
    assert reason == "matched 'react' in title"


def test_exclusion_wins_over_inclusion() -> None:
    settings = settings_from_mapping({"keywords": ["python"], "exclude_keywords": ["clearance"]})
    status, reason = match_keywords(
        _job("Python Engineer", "This role requires a clearance."),
        settings,
    )
    assert status is KeywordStatus.EXCLUDED
    assert reason == "excluded by 'clearance' in description"


def test_java_does_not_match_javascript() -> None:
    settings = settings_from_mapping({"keywords": ["java"]})
    status, reason = match_keywords(_job("JavaScript Developer"), settings)
    assert status is KeywordStatus.EXCLUDED
    assert reason == "no inclusion keyword matched"
    status, reason = match_keywords(_job("Java Developer"), settings)
    assert status is KeywordStatus.MATCHED
    assert reason == "matched 'java' in title"


def test_short_token_matches_title_not_description() -> None:
    settings = settings_from_mapping({"keywords": ["go"]})
    missed, missed_reason = match_keywords(
        _job("Backend Engineer", "We go to market quickly."),
        settings,
    )
    assert missed is KeywordStatus.EXCLUDED
    assert missed_reason == "no inclusion keyword matched"
    matched, matched_reason = match_keywords(_job("Go Engineer"), settings)
    assert matched is KeywordStatus.MATCHED
    assert matched_reason == "matched 'go' in title"


def test_multiword_phrase_allows_flexible_space() -> None:
    settings = settings_from_mapping({"keywords": ["django rest"]})
    status, reason = match_keywords(
        _job("Engineer", "Experience with django   rest framework."),
        settings,
    )
    assert status is KeywordStatus.MATCHED
    assert reason == "matched 'django rest' in description"


def test_pipeline_drops_keyword_misses() -> None:
    settings = settings_from_mapping({"keywords": ["python"]})
    kept = annotate(_job("Python Developer"), settings)
    dropped = annotate(_job("Designer"), settings)
    assert kept.keyword_status is KeywordStatus.MATCHED
    assert kept.latam_eligibility is not None
    assert dropped.keyword_status is KeywordStatus.EXCLUDED
    assert dropped.latam_eligibility is None
