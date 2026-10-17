import gzip
import json
from datetime import UTC, datetime, timedelta

from latam_jobs.ats_directory import (
    BOARD_PAUSE_SECONDS,
    CatalogBoard,
    board_url,
    load_catalog,
    parse_directory,
)
from latam_jobs.settings import settings_from_mapping


def _gzip(rows: list[dict]) -> bytes:
    text = "\n".join(json.dumps(row) for row in rows).encode("utf-8")
    return gzip.compress(text)


def test_parse_directory_keeps_live_greenhouse_lever_and_ashby_rows() -> None:
    payload = _gzip(
        [
            {"provider": "greenhouse", "slug": "GoodwayGroup", "status": "ok"},
            {"provider": "greenhouse", "slug": "deadco", "status": "dead"},
            {"provider": "lever", "slug": "Palantir", "region": "eu", "status": "ok"},
            {"provider": "recruitee", "slug": "acme", "status": "ok"},
            {"provider": "ashby", "slug": "linear", "status": "ok"},
        ]
    )
    boards = {(board.provider, board.slug, board.region) for board in parse_directory(payload)}
    assert ("greenhouse", "GoodwayGroup", None) in boards
    assert ("greenhouse", "deadco", None) not in boards
    assert ("recruitee", "acme", None) not in boards
    lever = next(board for board in parse_directory(payload) if board.provider == "lever")
    assert board_url(lever) == "https://api.eu.lever.co/v0/postings/Palantir?mode=json"


def test_shipped_catalog_loads_without_a_download(tmp_path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "updated_at": "2026-10-17T00:00:00+00:00",
                "boards": [{"provider": "greenhouse", "slug": "gitlab", "region": None}],
            }
        ),
        encoding="utf-8",
    )
    calls: list[str] = []

    class Client:
        def get(self, url: str, **_kwargs: object) -> None:
            calls.append(url)
            raise AssertionError(url)

    settings = settings_from_mapping({"ats_catalog": {"update_online": False}})
    boards = load_catalog(
        settings,
        now=datetime(2026, 10, 18, tzinfo=UTC),
        client=Client(),
        working_path=tmp_path / "missing.json",
        seed_path=seed,
    )
    assert calls == []
    assert boards == (CatalogBoard("greenhouse", "gitlab", None),)


def test_stale_working_copy_is_replaced_when_online_updates_are_on(tmp_path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps({"updated_at": "2026-01-01T00:00:00+00:00", "boards": []}),
        encoding="utf-8",
    )
    working = tmp_path / "ats-companies.json"
    working.write_text(
        json.dumps(
            {
                "updated_at": "2026-10-01T00:00:00+00:00",
                "boards": [{"provider": "ashby", "slug": "old", "region": None}],
            }
        ),
        encoding="utf-8",
    )
    payload = _gzip([{"provider": "greenhouse", "slug": "gitlab", "status": "ok"}])

    class Client:
        def get(self, url: str, **_kwargs: object):
            return type("Response", (), {"content": payload})()

    settings = settings_from_mapping({"ats_catalog": {"update_online": True}})
    now = datetime(2026, 10, 17, tzinfo=UTC)
    boards = load_catalog(
        settings,
        now=now,
        client=Client(),
        working_path=working,
        seed_path=seed,
    )
    assert boards == (CatalogBoard("greenhouse", "gitlab", None),)
    saved = json.loads(working.read_text(encoding="utf-8"))
    assert saved["boards"] == [{"provider": "greenhouse", "slug": "gitlab", "region": None}]


def test_a_failed_download_keeps_the_local_catalog(tmp_path) -> None:
    seed = tmp_path / "seed.json"
    seed.write_text(
        json.dumps(
            {
                "updated_at": "2026-01-01T00:00:00+00:00",
                "boards": [{"provider": "lever", "slug": "spotify", "region": None}],
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def get(self, url: str, **_kwargs: object) -> None:
            raise OSError("offline")

    settings = settings_from_mapping({"ats_catalog": {"update_online": True}})
    boards = load_catalog(
        settings,
        now=datetime(2026, 10, 17, tzinfo=UTC),
        client=Client(),
        working_path=tmp_path / "missing.json",
        seed_path=seed,
    )
    assert boards == (CatalogBoard("lever", "spotify", None),)
    assert BOARD_PAUSE_SECONDS == 0.25


def test_a_fresh_working_copy_is_not_downloaded_again(tmp_path) -> None:
    working = tmp_path / "ats-companies.json"
    now = datetime(2026, 10, 17, 12, tzinfo=UTC)
    working.write_text(
        json.dumps(
            {
                "updated_at": (now - timedelta(hours=1)).isoformat(),
                "boards": [{"provider": "ashby", "slug": "linear", "region": None}],
            }
        ),
        encoding="utf-8",
    )

    class Client:
        def get(self, url: str, **_kwargs: object) -> None:
            raise AssertionError(url)

    settings = settings_from_mapping({"ats_catalog": {"update_online": True}})
    boards = load_catalog(
        settings,
        now=now,
        client=Client(),
        working_path=working,
        seed_path=tmp_path / "missing-seed.json",
    )
    assert boards == (CatalogBoard("ashby", "linear", None),)
