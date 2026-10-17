"""Local Greenhouse, Lever, and Ashby board catalog.

The shipped file is the seed. A successful online refresh writes a working copy
next to the database. Collection reads the working copy when it exists.
"""

from __future__ import annotations

import gzip
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from latam_jobs.scheduler import from_iso, to_iso
from latam_jobs.settings import Settings

logger = logging.getLogger(__name__)

SEED_PATH = Path(__file__).parent / "catalog" / "ats_companies.json"
DIRECTORY_URL = "https://raw.githubusercontent.com/moonie0201/ats-directory/main/companies.jsonl.gz"
BOARD_PAUSE_SECONDS = 0.25
_REFRESH_AFTER = timedelta(hours=24)
_PROVIDERS = frozenset({"greenhouse", "lever", "ashby"})


@dataclass(frozen=True)
class CatalogBoard:
    provider: str
    slug: str
    region: str | None = None


@dataclass(frozen=True)
class CatalogDocument:
    updated_at: datetime
    boards: tuple[CatalogBoard, ...]


class Pacer:
    """Wait between requests. The first call does not wait."""

    def __init__(self, sleeper) -> None:
        self._sleeper = sleeper
        self._started = False

    def wait(self) -> None:
        if self._started:
            self._sleeper(BOARD_PAUSE_SECONDS)
        self._started = True


def board_url(board: CatalogBoard) -> str:
    slug = board.slug
    if board.provider == "greenhouse":
        return f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    if board.provider == "lever":
        host = "api.eu.lever.co" if (board.region or "").casefold() == "eu" else "api.lever.co"
        return f"https://{host}/v0/postings/{slug}?mode=json"
    if board.provider == "ashby":
        return f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    raise ValueError(f"Unknown ATS provider {board.provider!r}")


def targets(provider: str, ctx) -> list[tuple[str, str]]:
    """Catalog rows for this provider, then any extra feed slugs, one URL each."""
    found: dict[str, str] = {}
    for board in ctx.catalog:
        if board.provider == provider and board.slug not in found:
            found[board.slug] = board_url(board)
    for feed in ctx.settings.feeds:
        slug = feed.strip()
        if slug and slug not in found:
            found[slug] = board_url(CatalogBoard(provider, slug))
    return list(found.items())


def load_catalog(
    settings: Settings,
    *,
    now: datetime,
    client,
    working_path: Path,
    seed_path: Path = SEED_PATH,
) -> tuple[CatalogBoard, ...]:
    """Return the local catalog. Refresh it from GitHub only when that option is on."""
    seed = _read(seed_path)
    working = _read(working_path) if working_path.is_file() else None
    current = working if working is not None else seed
    if not settings.ats_catalog.update_online:
        return current.boards
    if working is not None and now - working.updated_at < _REFRESH_AFTER:
        return current.boards
    try:
        response = client.get(DIRECTORY_URL)
        boards = parse_directory(response.content)
    except Exception as exc:
        logger.warning("ats catalog refresh failed: %s", exc)
        return current.boards
    _write(working_path, CatalogDocument(updated_at=now, boards=boards))
    return boards


def parse_directory(payload: bytes) -> tuple[CatalogBoard, ...]:
    text = _gunzip(payload).decode("utf-8")
    found: dict[tuple[str, str], CatalogBoard] = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or row.get("status") != "ok":
            continue
        provider = row.get("provider")
        slug = row.get("slug")
        if provider not in _PROVIDERS or not isinstance(slug, str) or not slug.strip():
            continue
        token = slug.strip()
        region = row.get("region")
        found.setdefault(
            (provider, token),
            CatalogBoard(
                provider=provider,
                slug=token,
                region=region.strip() if isinstance(region, str) and region.strip() else None,
            ),
        )
    return tuple(found.values())


def _gunzip(payload: bytes) -> bytes:
    try:
        return gzip.decompress(payload)
    except gzip.BadGzipFile:
        return payload


def _read(path: Path) -> CatalogDocument:
    if not path.is_file():
        return CatalogDocument(updated_at=datetime.min.replace(tzinfo=UTC), boards=())
    payload = json.loads(path.read_text(encoding="utf-8"))
    updated = from_iso(str(payload.get("updated_at") or ""))
    if updated is None:
        updated = datetime.min.replace(tzinfo=UTC)
    boards = []
    for row in payload.get("boards") or []:
        if not isinstance(row, dict):
            continue
        provider = row.get("provider")
        slug = row.get("slug")
        if provider not in _PROVIDERS or not isinstance(slug, str) or not slug.strip():
            continue
        region = row.get("region")
        boards.append(
            CatalogBoard(
                provider=provider,
                slug=slug.strip(),
                region=region.strip() if isinstance(region, str) and region.strip() else None,
            )
        )
    return CatalogDocument(updated_at=updated, boards=tuple(boards))


def _write(path: Path, document: CatalogDocument) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "updated_at": to_iso(document.updated_at),
        "boards": [
            {"provider": board.provider, "slug": board.slug, "region": board.region}
            for board in document.boards
        ],
    }
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
