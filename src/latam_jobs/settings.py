"""Load and validate settings.yaml. Secrets stay in the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from latam_jobs.geo import country_slug

DEFAULT_CONFIG_PATH = Path("settings.yaml")


class ConfigError(Exception):
    """The settings file is missing or invalid."""


class SourceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    interval_hours: int = Field(default=24, ge=1, le=168)
    url: str | None = None
    max_pages: int = Field(default=1, ge=1, le=20)
    feeds: list[str] = Field(default_factory=list)


class EligibilitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    accepted_only_notifications: bool = True
    include_maybe_in_storage: bool = True
    store_rejected: bool = True


class SchedulerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    check_interval_minutes: int = Field(default=60, ge=1, le=24 * 60)
    jitter_seconds: int = Field(default=120, ge=0, le=3600)


class HttpSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timeout_seconds: float = Field(default=20, gt=0, le=120)
    max_retries: int = Field(default=2, ge=0, le=5)


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = "data/latam-jobs.sqlite"


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str = "127.0.0.1"
    port: int = Field(default=8080, ge=1, le=65535)


class NotificationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class AtsCatalogSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    update_online: bool = True


class Settings(BaseModel):
    """Bootstrap file and the editable collection document share this shape."""

    model_config = ConfigDict(extra="forbid")

    residence_country: str = "colombia"
    display_timezone: str = "America/Bogota"
    remote_only: bool = True
    keywords: list[str] = Field(default_factory=list)
    exclude_keywords: list[str] = Field(default_factory=list)
    eligibility: EligibilitySettings = Field(default_factory=EligibilitySettings)
    scheduler: SchedulerSettings = Field(default_factory=SchedulerSettings)
    http: HttpSettings = Field(default_factory=HttpSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    web: WebSettings = Field(default_factory=WebSettings)
    notifications: NotificationSettings = Field(default_factory=NotificationSettings)
    ats_catalog: AtsCatalogSettings = Field(default_factory=AtsCatalogSettings)
    sources: dict[str, SourceSettings] = Field(default_factory=dict)

    @field_validator("residence_country", mode="before")
    @classmethod
    def normalize_residence_country(cls, value: object) -> str:
        if value is None or value == "":
            return "colombia"
        if not isinstance(value, str):
            raise ValueError("residence_country must be a string")
        slug = country_slug(value)
        if slug is None:
            raise ValueError(
                f"Unknown residence country {value!r}. Use a Latin American country name or code."
            )
        return slug

    @field_validator("display_timezone")
    @classmethod
    def check_display_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown timezone {value!r}.") from exc
        return value

    @field_validator("keywords", "exclude_keywords")
    @classmethod
    def strip_keywords(cls, value: list[str]) -> list[str]:
        cleaned = [item.strip() for item in value if item.strip()]
        return cleaned


def config_path_from_env() -> Path:
    override = os.environ.get("LATAM_JOBS_CONFIG")
    if override:
        return Path(override)
    system_path = Path("/etc/latam-jobs/settings.yaml")
    if system_path.is_file():
        return system_path
    return DEFAULT_CONFIG_PATH


def load_env_file(path: Path | None = None) -> None:
    """Load KEY=VALUE pairs without overriding variables already set."""
    env_path = path if path is not None else Path(".env")
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        os.environ[key] = value.strip().strip('"').strip("'")


def load_settings(path: Path | None = None) -> Settings:
    config_path = path if path is not None else config_path_from_env()
    if not config_path.is_file():
        raise ConfigError(f"Settings file not found: {config_path}")
    try:
        loaded = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"Invalid YAML in {config_path}: {exc}") from exc
    if loaded is None:
        loaded = {}
    if not isinstance(loaded, dict):
        raise ConfigError(f"Settings file must be a mapping: {config_path}")
    return settings_from_mapping(loaded)


def settings_from_mapping(payload: dict[str, Any]) -> Settings:
    try:
        return Settings.model_validate(payload)
    except Exception as exc:
        raise ConfigError(str(exc)) from exc


def validate_registered_sources(settings: Settings, registry: dict[str, Any]) -> None:
    """Fail when an enabled source has no adapter. Disabled unknown names are kept."""
    missing = sorted(
        name for name, source in settings.sources.items() if source.enabled and name not in registry
    )
    if missing:
        joined = ", ".join(missing)
        raise ConfigError(f"Enabled sources have no adapter: {joined}")


def merge_registered_sources(settings: Settings, registry: dict[str, Any]) -> Settings:
    """Add adapters that are not in the stored document yet, using their defaults."""
    sources = dict(settings.sources)
    for name, adapter in registry.items():
        if name in sources:
            continue
        feeds = getattr(adapter, "default_feeds", ())
        sources[name] = SourceSettings(
            enabled=bool(getattr(adapter, "default_enabled", False)),
            interval_hours=int(getattr(adapter, "default_interval_hours", 24)),
            url=getattr(adapter, "default_url", None),
            max_pages=int(getattr(adapter, "default_max_pages", 1)),
            feeds=list(feeds),
        )
    return settings.model_copy(update={"sources": sources})
