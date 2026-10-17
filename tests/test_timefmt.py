from datetime import UTC, datetime, timedelta

from latam_jobs.web.timefmt import format_posted, format_when

NOW = datetime(2026, 10, 17, 21, 30, tzinfo=UTC)


def test_format_when_uses_the_display_timezone_and_a_relative_phrase() -> None:
    value = datetime(2026, 10, 17, 19, 30, tzinfo=UTC)
    assert format_when(value, "America/Bogota", NOW) == "Oct 17, 2026, 2:30 PM (2 hours ago)"
    assert format_when(None, "America/Bogota", NOW) == "Never"
    assert format_posted(value.isoformat(), "America/Bogota", NOW) == format_when(
        value, "America/Bogota", NOW
    )
    assert format_posted(None, "America/Bogota", NOW) == "—"
    assert format_when(NOW - timedelta(seconds=30), "America/Bogota", NOW).endswith("(just now)")
    assert format_when(NOW - timedelta(minutes=1), "America/Bogota", NOW).endswith("(1 minute ago)")
    assert format_when(NOW - timedelta(days=1), "America/Bogota", NOW).endswith("(1 day ago)")
