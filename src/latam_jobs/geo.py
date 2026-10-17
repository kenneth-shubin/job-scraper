"""Latin American residence countries and the regions used for eligibility."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Country:
    slug: str
    name: str
    alpha2: str
    region: str
    aliases: tuple[str, ...] = ()


def _fold(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().lower())
    stripped = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return " ".join(stripped.replace("-", " ").split())


COUNTRIES: tuple[Country, ...] = (
    Country("mexico", "Mexico", "MX", "mexico", ("méxico", "mejico")),
    Country("guatemala", "Guatemala", "GT", "central_america"),
    Country("honduras", "Honduras", "HN", "central_america"),
    Country("el-salvador", "El Salvador", "SV", "central_america", ("el salvador",)),
    Country("nicaragua", "Nicaragua", "NI", "central_america"),
    Country("costa-rica", "Costa Rica", "CR", "central_america", ("costa rica",)),
    Country("panama", "Panama", "PA", "central_america", ("panamá",)),
    Country("cuba", "Cuba", "CU", "caribbean"),
    Country(
        "dominican-republic",
        "Dominican Republic",
        "DO",
        "caribbean",
        ("dominican republic", "república dominicana"),
    ),
    Country("colombia", "Colombia", "CO", "south_america"),
    Country("venezuela", "Venezuela", "VE", "south_america"),
    Country("ecuador", "Ecuador", "EC", "south_america"),
    Country("peru", "Peru", "PE", "south_america", ("perú",)),
    Country("bolivia", "Bolivia", "BO", "south_america"),
    Country("chile", "Chile", "CL", "south_america"),
    Country("argentina", "Argentina", "AR", "south_america"),
    Country("uruguay", "Uruguay", "UY", "south_america"),
    Country("paraguay", "Paraguay", "PY", "south_america"),
    Country("brazil", "Brazil", "BR", "south_america", ("brasil",)),
)

_BY_SLUG = {country.slug: country for country in COUNTRIES}

_LOOKUP: dict[str, str] = {}
for country in COUNTRIES:
    for label in (country.slug, country.name, country.alpha2, *country.aliases):
        _LOOKUP[_fold(label)] = country.slug


def country_slug(value: str) -> str | None:
    """Return the canonical slug for a name, alias, or ISO alpha-2 code."""
    return _LOOKUP.get(_fold(value))


def country_by_slug(slug: str) -> Country | None:
    return _BY_SLUG.get(slug)


def latin_america_slugs() -> frozenset[str]:
    return frozenset(_BY_SLUG)


def south_america_slugs() -> frozenset[str]:
    return frozenset(country.slug for country in COUNTRIES if country.region == "south_america")


def central_america_slugs() -> frozenset[str]:
    return frozenset(country.slug for country in COUNTRIES if country.region == "central_america")
