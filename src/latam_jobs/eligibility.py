"""Deterministic remote-status and residence-country eligibility."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from latam_jobs.geo import (
    COUNTRIES,
    central_america_slugs,
    latin_america_slugs,
    south_america_slugs,
)
from latam_jobs.models import Job, LatamEligibility, RemoteStatus
from latam_jobs.settings import Settings

_DECISIVE_REGIONS = {
    "latam",
    "south_america",
    "central_america",
    "worldwide",
    "americas",
    "europe",
    "emea",
    "apac",
}
_REGION_PHRASES = (
    ("latin america", "latam"),
    ("south america", "south_america"),
    ("central america", "central_america"),
    ("northern america", "north_america"),
    ("north america", "north_america"),
    ("anywhere in the world", "worldwide"),
    ("worldwide", "worldwide"),
    ("anywhere", "worldwide"),
    ("global", "worldwide"),
    ("americas", "americas"),
    ("latam", "latam"),
    ("caribbean", "caribbean"),
    ("european union", "europe"),
    ("europe", "europe"),
    ("emea", "emea"),
    ("apac", "apac"),
)
_FILLER = frozenset(
    {
        "remote",
        "work",
        "from",
        "the",
        "a",
        "an",
        "only",
        "based",
        "anywhere",
        "in",
        "of",
        "position",
        "job",
        "timezone",
        "timezones",
        "hours",
        "and",
        "or",
        "within",
        "candidates",
        "resident",
        "residents",
        "role",
        "fully",
    }
)
_EXTRA_COUNTRIES = (
    ("united states of america", "united-states"),
    ("united states", "united-states"),
    ("united kingdom", "united-kingdom"),
    ("great britain", "united-kingdom"),
    ("usa", "united-states"),
    ("us", "united-states"),
    ("uk", "united-kingdom"),
    ("canada", "canada"),
    ("germany", "germany"),
    ("france", "france"),
    ("spain", "spain"),
    ("portugal", "portugal"),
    ("india", "india"),
    ("australia", "australia"),
    ("ireland", "ireland"),
    ("poland", "poland"),
    ("netherlands", "netherlands"),
    ("singapore", "singapore"),
    ("switzerland", "switzerland"),
    ("norway", "norway"),
    ("iceland", "iceland"),
)
_NON_LATAM_PATTERNS = (
    r"\bus residents only\b",
    r"\bunited states residents only\b",
    r"\busa only\b",
    r"\bus only\b",
    r"\bunited states only\b",
    r"\bonly (?:in )?(?:the )?(?:us|usa|united states)\b",
    r"\bmust be based in (?:the )?(?:us|usa|united states)\b",
    r"\bmust (?:live|reside|be located) in (?:the )?(?:us|usa|united states)\b",
    r"\bus work authorization required\b",
    r"\bauthorized to work in the (?:us|usa|united states)\b",
    r"\beligible to work in the (?:us|usa|united states)\b",
    r"\bremote within the (?:us|usa|united states)\b",
    r"\bw2\b.{0,80}\b(?:us|usa|united states)\b.{0,40}\b"
    r"(?:resident|residents|only|authorization)\b",
    r"\bcanada only\b",
    r"\bonly (?:in )?canada\b",
    r"\bmust be based in canada\b",
    r"\bcanadian residents only\b",
    r"\buk only\b",
    r"\bunited kingdom only\b",
    r"\bonly (?:in )?(?:the )?(?:uk|united kingdom)\b",
    r"\bmust be based in (?:the )?(?:uk|united kingdom)\b",
    r"\beurope only\b",
    r"\beu only\b",
    r"\beuropean union only\b",
    r"\bonly (?:in )?(?:europe|the european union)\b",
    r"\bmust be based in (?:europe|the european union)\b",
    r"\bemea only\b",
    r"\bapac only\b",
)
_EXCLUSIVE = re.compile(
    r"\b(only|restricted to|must be based in|must reside in|must live in|"
    r"must be located in|live in)\b"
)
_EXCEPT = re.compile(r"\b(except|excluding|outside|other than)\b")


@dataclass(frozen=True)
class _FieldReading:
    countries: frozenset[str]
    regions: frozenset[str]
    exclusive: bool


def resolve_remote_status(job: Job) -> RemoteStatus:
    if job.remote_status is not RemoteStatus.UNKNOWN:
        return job.remote_status
    text = _normalize(f"{job.title} {job.location_text} {job.description}")
    if re.search(r"\b(optional office|office optional|remote first)\b", text):
        return RemoteStatus.REMOTE
    if re.search(r"\bhybrid\b", text):
        return RemoteStatus.HYBRID
    if re.search(r"\b(on site|onsite|in office|office based)\b", text):
        return RemoteStatus.ONSITE
    if re.search(r"\b(fully remote|work from home|wfh|remote)\b", text):
        return RemoteStatus.REMOTE
    return RemoteStatus.UNKNOWN


def classify_eligibility(job: Job, settings: Settings) -> tuple[LatamEligibility, str]:
    residence = settings.residence_country
    remote_status = resolve_remote_status(job)
    if settings.remote_only and remote_status in {RemoteStatus.ONSITE, RemoteStatus.HYBRID}:
        return _decision(
            LatamEligibility.REJECTED,
            "remote_only",
            residence,
            "on-site and hybrid jobs are excluded",
        )

    structured = _structured_countries(job)
    if structured:
        return _allow_list(structured, residence)

    combined = _normalize("\n".join((job.title, job.location_text, job.description)))
    if _has_non_latam_restriction(combined):
        return _decision(
            LatamEligibility.REJECTED,
            "non_latam_residency",
            residence,
            "explicit residency restriction outside Latin America",
        )
    location = _read_field(job.location_text)
    if location is not None and location.countries:
        return _allow_list(location.countries, residence)
    exception = _worldwide_exception(combined, residence)
    if exception is not None:
        return exception

    exclusive_sets = _exclusive_country_sets(f"{job.location_text}\n{job.description}")
    if len(exclusive_sets) > 1:
        return _decision(
            LatamEligibility.MAYBE,
            "conflicting",
            residence,
            "conflicting location restrictions",
        )
    if len(exclusive_sets) == 1:
        return _allow_list(next(iter(exclusive_sets)), residence)

    reading = _read_field(job.location_text)
    if reading is not None and reading.countries:
        other_exclusive = exclusive_sets
        if other_exclusive and reading.countries not in other_exclusive:
            return _decision(
                LatamEligibility.MAYBE,
                "conflicting",
                residence,
                "conflicting location restrictions",
            )
        return _from_reading(reading, residence)
    if reading is not None:
        return _from_reading(reading, residence)

    if _normalize(job.location_text) in {"americas", "remote americas"}:
        return _decision(
            LatamEligibility.ACCEPTED,
            "americas",
            residence,
            "Americas includes the residence country",
        )

    if re.search(r"\bnorth america\b|\bnorthern america\b", _normalize(job.location_text)):
        return _decision(
            LatamEligibility.MAYBE,
            "north_america",
            residence,
            "North America does not say whether the residence country is included",
        )
    if re.search(r"\bcaribbean\b", _normalize(job.location_text)):
        return _decision(
            LatamEligibility.MAYBE,
            "caribbean",
            residence,
            "Caribbean does not say whether the residence country is included",
        )
    return _decision(
        LatamEligibility.MAYBE,
        "insufficient",
        residence,
        "remote without a geographic eligibility statement",
    )


def _allow_list(countries: frozenset[str], residence: str) -> tuple[LatamEligibility, str]:
    names = ", ".join(sorted(countries))
    if residence in countries:
        return _decision(
            LatamEligibility.ACCEPTED,
            "country_allow_list",
            residence,
            f"allowed countries include {residence}",
        )
    return _decision(
        LatamEligibility.REJECTED,
        "country_allow_list",
        residence,
        f"restricted to {names}, which does not include {residence}",
    )


def _from_reading(reading: _FieldReading, residence: str) -> tuple[LatamEligibility, str]:
    if reading.countries:
        return _allow_list(reading.countries, residence)
    if "worldwide" in reading.regions:
        return _decision(
            LatamEligibility.ACCEPTED,
            "worldwide",
            residence,
            "worldwide includes the residence country",
        )
    if "americas" in reading.regions and not reading.countries:
        return _decision(
            LatamEligibility.ACCEPTED,
            "americas",
            residence,
            "Americas includes the residence country",
        )
    inclusive = sorted(region for region in reading.regions if _region_includes(region, residence))
    if inclusive:
        label = _region_label(inclusive[0])
        return _decision(
            LatamEligibility.ACCEPTED,
            "region",
            residence,
            f"{label} includes the residence country",
        )
    blocking = sorted(
        region
        for region in reading.regions
        if region in _DECISIVE_REGIONS and not _region_includes(region, residence)
    )
    if blocking and not inclusive:
        label = _region_label(blocking[0])
        return _decision(
            LatamEligibility.REJECTED,
            "region",
            residence,
            f"{label} does not include the residence country",
        )
    if reading.regions <= {"north_america", "caribbean"} and reading.regions:
        rule = "north_america" if "north_america" in reading.regions else "caribbean"
        detail = (
            "North America does not say whether the residence country is included"
            if rule == "north_america"
            else "Caribbean does not say whether the residence country is included"
        )
        return _decision(LatamEligibility.MAYBE, rule, residence, detail)
    return _decision(
        LatamEligibility.MAYBE,
        "insufficient",
        residence,
        "remote without a geographic eligibility statement",
    )


def _worldwide_exception(text: str, residence: str) -> tuple[LatamEligibility, str] | None:
    if not re.search(r"\b(worldwide|anywhere|global)\b", text):
        return None
    match = _EXCEPT.search(text)
    if match is None:
        return None
    excluded = frozenset(_extract_countries(text[match.end() :]))
    if residence in excluded:
        return _decision(
            LatamEligibility.REJECTED,
            "worldwide_exception",
            residence,
            f"worldwide except {residence}",
        )
    return _decision(
        LatamEligibility.ACCEPTED,
        "worldwide",
        residence,
        "worldwide includes the residence country",
    )


def _has_non_latam_restriction(text: str) -> bool:
    for pattern in _NON_LATAM_PATTERNS:
        for match in re.finditer(pattern, text):
            prefix = text[max(0, match.start() - 40) : match.start()]
            if _EXCEPT.search(prefix):
                continue
            return True
    return False


def _structured_countries(job: Job) -> frozenset[str]:
    if not job.location_countries:
        return frozenset()
    return frozenset(job.location_countries)


def _exclusive_country_sets(text: str) -> set[frozenset[str]]:
    found: set[frozenset[str]] = set()
    for clause in re.split(r"[.\n;]", text):
        normalized = _normalize(clause)
        if not normalized or _EXCEPT.search(normalized):
            continue
        if _EXCLUSIVE.search(normalized) is None:
            continue
        countries = frozenset(_extract_countries(normalized))
        if countries:
            found.add(countries)
    return found


def _read_field(location_text: str) -> _FieldReading | None:
    normalized = _normalize(location_text)
    if not normalized or _EXCEPT.search(normalized):
        return None
    pieces = re.split(r"\s*(?:,|/|&|\band\b|\bor\b)\s*", normalized)
    countries: list[str] = []
    regions: list[str] = []
    for piece in pieces:
        piece = piece.strip()
        if not piece:
            continue
        region = _match_region_phrase(piece)
        found = _extract_countries(piece)
        if _leftover(piece):
            return None
        countries.extend(found)
        if region is not None:
            regions.append(region)
    if not countries and not regions:
        return None
    return _FieldReading(
        countries=frozenset(countries),
        regions=frozenset(regions),
        exclusive=_EXCLUSIVE.search(normalized) is not None,
    )


def _leftover(piece: str) -> str:
    text = piece
    for phrase, _slug in _country_terms():
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    for phrase, _region in _REGION_PHRASES:
        text = re.sub(rf"\b{re.escape(phrase)}\b", " ", text)
    words = [word for word in text.split() if word not in _FILLER]
    return " ".join(words)


def _match_region_phrase(piece: str) -> str | None:
    for phrase, region in _REGION_PHRASES:
        if re.search(rf"\b{re.escape(phrase)}\b", piece):
            return region
    return None


def _extract_countries(text: str) -> list[str]:
    found: list[str] = []
    occupied = text
    for phrase, slug in _country_terms():
        pattern = rf"\b{re.escape(phrase)}\b"
        if re.search(pattern, occupied):
            found.append(slug)
            occupied = re.sub(pattern, " ", occupied)
    return found


def _country_terms() -> list[tuple[str, str]]:
    terms = [(_normalize(country.name), country.slug) for country in COUNTRIES]
    for country in COUNTRIES:
        terms.extend((_normalize(alias), country.slug) for alias in country.aliases)
    terms.extend(_EXTRA_COUNTRIES)
    unique: dict[str, str] = {}
    for phrase, slug in terms:
        normalized = _normalize(phrase)
        if normalized:
            unique.setdefault(normalized, slug)
    return sorted(unique.items(), key=lambda item: len(item[0]), reverse=True)


def _region_includes(region: str, residence: str) -> bool:
    if region in {"worldwide", "americas", "latam"}:
        return residence in latin_america_slugs()
    if region == "south_america":
        return residence in south_america_slugs()
    if region == "central_america":
        return residence in central_america_slugs()
    return False


def _region_label(region: str) -> str:
    labels = {
        "latam": "Latin America",
        "south_america": "South America",
        "central_america": "Central America",
        "worldwide": "worldwide",
        "americas": "Americas",
        "europe": "Europe",
        "emea": "EMEA",
        "apac": "APAC",
        "north_america": "North America",
        "caribbean": "Caribbean",
    }
    return labels.get(region, region)


def _decision(
    eligibility: LatamEligibility,
    rule: str,
    residence: str,
    detail: str,
) -> tuple[LatamEligibility, str]:
    return eligibility, f"{eligibility.value}:{rule}:{residence}: {detail}"


def _normalize(value: str) -> str:
    text = value.replace("—", " ").replace("–", " ")
    text = unicodedata.normalize("NFKD", text).casefold()
    text = "".join(character for character in text if not unicodedata.combining(character))
    text = text.replace("u.s.a.", "usa").replace("u.s.", "us")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
