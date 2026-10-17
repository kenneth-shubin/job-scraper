from pathlib import Path

import pytest

from latam_jobs.cli import main
from latam_jobs.settings import (
    ConfigError,
    Settings,
    load_settings,
    merge_registered_sources,
    settings_from_mapping,
    validate_registered_sources,
)
from latam_jobs.sources import SOURCES

SAMPLE = Path("settings.yaml")


def test_sample_file_loads() -> None:
    settings = load_settings(SAMPLE)
    assert settings.residence_country == "colombia"
    assert settings.remote_only is True
    assert settings.sources["remotive"].enabled is True
    assert settings.sources["remotive"].interval_hours == 12
    assert settings.sources["himalayas"].interval_hours == 24
    assert settings.sources["remoteok"].enabled is False
    assert settings.sources["greenhouse"].enabled is False
    assert settings.sources["lever"].enabled is False
    assert settings.sources["ashby"].enabled is False
    assert settings.sources["jobicy"].enabled is True
    assert "python" in settings.keywords


def test_new_sources_use_adapter_defaults() -> None:
    merged = merge_registered_sources(Settings(), SOURCES)
    assert merged.sources["greenhouse"].enabled is False
    assert merged.sources["lever"].enabled is False
    assert merged.sources["ashby"].enabled is False
    assert merged.sources["jobicy"].max_pages == 2
    assert merged.sources["remoteok"].enabled is False


def test_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="not found"):
        load_settings(tmp_path / "missing.yaml")


def test_bad_types() -> None:
    with pytest.raises(ConfigError):
        settings_from_mapping({"http": {"timeout_seconds": "slow"}})
    with pytest.raises(ConfigError):
        settings_from_mapping({"sources": {"remotive": {"interval_hours": 0}}})


def test_duplicate_keywords_are_kept() -> None:
    settings = settings_from_mapping({"keywords": ["python", "python"]})
    assert settings.keywords == ["python", "python"]


def test_secrets_are_not_read_from_yaml() -> None:
    with pytest.raises(ConfigError):
        settings_from_mapping({"telegram_bot_token": "secret-value"})
    settings = settings_from_mapping({})
    dumped = settings.model_dump()
    assert "telegram_bot_token" not in dumped
    assert "secret-value" not in str(dumped)


def test_omitted_residence_country_defaults_to_colombia() -> None:
    settings = settings_from_mapping({})
    assert settings.residence_country == "colombia"


@pytest.mark.parametrize("value", ["CO", "Colombia", "colombia"])
def test_residence_country_aliases(value: str) -> None:
    settings = settings_from_mapping({"residence_country": value})
    assert settings.residence_country == "colombia"


def test_unknown_country_fails() -> None:
    with pytest.raises(ConfigError, match="Unknown residence country"):
        settings_from_mapping({"residence_country": "spain"})


def test_disabled_unknown_source_passes() -> None:
    settings = settings_from_mapping(
        {"sources": {"futureboard": {"enabled": False, "interval_hours": 24}}}
    )
    validate_registered_sources(settings, {})


def test_cli_check_accepts_sample_and_rejects_invalid(tmp_path: Path) -> None:
    assert main(["check", "--config", str(SAMPLE)]) == 0
    invalid = tmp_path / "bad.yaml"
    invalid.write_text("residence_country: spain\n", encoding="utf-8")
    assert main(["check", "--config", str(invalid)]) == 2


def test_enabled_unknown_source_fails() -> None:
    settings = settings_from_mapping(
        {"sources": {"futureboard": {"enabled": True, "interval_hours": 24}}}
    )
    with pytest.raises(ConfigError, match="futureboard"):
        validate_registered_sources(settings, {"remotive": object()})
