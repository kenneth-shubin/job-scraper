from datetime import UTC, datetime
from pathlib import Path

from latam_jobs.models import Job, KeywordStatus, LatamEligibility, RemoteStatus
from latam_jobs.notify import deliver_notifications
from latam_jobs.pipeline import collect
from latam_jobs.settings import DatabaseSettings, NotificationSettings, Settings
from latam_jobs.sources.base import FetchContext, FetchResult
from latam_jobs.store import Store

NOW = datetime(2026, 10, 17, 18, 0, tzinfo=UTC)


def _store(tmp_path: Path) -> Store:
    store = Store(tmp_path / "jobs.sqlite")
    store.migrate()
    return store


def _job(
    source_job_id: str,
    *,
    eligibility: LatamEligibility = LatamEligibility.ACCEPTED,
    location_text: str = "Worldwide",
) -> Job:
    return Job(
        source="remotive",
        source_job_id=source_job_id,
        title=f"Backend {source_job_id}",
        company="Example",
        url=f"https://example.com/jobs/{source_job_id}",
        description="backend role",
        location_text=location_text,
        remote_status=RemoteStatus.REMOTE,
        latam_eligibility=eligibility,
        eligibility_reason="classified",
        keyword_status=KeywordStatus.MATCHED,
        keyword_reason="matched 'backend' in title",
        published_at=NOW,
    )


def _settings(store: Store, *, enabled: bool) -> Settings:
    store.load_or_seed(
        Settings(
            database=DatabaseSettings(path=str(store.path)),
            notifications=NotificationSettings(enabled=enabled),
            keywords=["backend"],
        )
    )
    loaded = store.load_or_seed(Settings(database=DatabaseSettings(path=str(store.path))))
    return loaded.model_copy(update={"notifications": NotificationSettings(enabled=enabled)})


def test_batches_stay_under_the_limit_and_only_sent_ids_change(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs(
        [_job("1", location_text="x" * 3000), _job("2", location_text="y" * 3000)],
        NOW,
    )
    settings = _settings(store, enabled=True)
    texts: list[str] = []

    def sender(text: str) -> int:
        texts.append(text)
        return 200 if len(texts) == 1 else 500

    sent = deliver_notifications(
        store, settings, now=NOW, token="token", chat_id="1", sender=sender
    )
    assert sent == 1
    assert len(texts) == 2
    assert all(len(text) <= 3500 for text in texts)
    assert store.get_job("remotive", "1").notification_status == "sent"  # type: ignore[union-attr]
    pending = store.get_job("remotive", "2")
    assert pending is not None
    assert pending.notification_status == "pending"
    assert pending.notification_attempts == 1
    store.close()


def test_pending_selection_ignores_maybe_and_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs(
        [
            _job("accepted"),
            _job("maybe", eligibility=LatamEligibility.MAYBE),
            _job("rejected", eligibility=LatamEligibility.REJECTED),
        ],
        NOW,
    )
    store._conn.execute("UPDATE jobs SET notification_status = 'pending'")
    store._conn.commit()
    settings = _settings(store, enabled=True)
    texts: list[str] = []
    deliver_notifications(
        store,
        settings,
        now=NOW,
        token="token",
        chat_id="1",
        sender=lambda text: texts.append(text) or 200,
    )
    assert len(texts) == 1
    assert "Backend accepted" in texts[0]
    assert "Backend maybe" not in texts[0]
    assert "Backend rejected" not in texts[0]
    store.close()


def test_disabled_or_missing_credentials_send_nothing(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs([_job("1")], NOW)
    calls: list[str] = []

    def sender(text: str) -> int:
        calls.append(text)
        return 200

    disabled = _settings(store, enabled=False)
    assert deliver_notifications(store, disabled, token="token", chat_id="1", sender=sender) == 0
    enabled = _settings(store, enabled=True)
    assert deliver_notifications(store, enabled, token="", chat_id="1", sender=sender) == 0
    assert calls == []
    assert store.get_job("remotive", "1").notification_status == "pending"  # type: ignore[union-attr]
    store.close()


def test_fifth_failure_marks_the_job_failed_and_a_later_run_skips_sent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.upsert_jobs([_job("1")], NOW)
    stored = store.get_job("remotive", "1")
    assert stored is not None
    for _ in range(4):
        store.mark_notification_failed([stored.id], give_up=False)
    settings = _settings(store, enabled=True)
    deliver_notifications(
        store, settings, now=NOW, token="token", chat_id="1", sender=lambda _text: 500
    )
    failed = store.get_job("remotive", "1")
    assert failed is not None
    assert failed.notification_status == "failed"
    assert failed.notification_attempts == 5

    store.upsert_jobs([_job("2")], NOW)
    calls: list[str] = []
    deliver_notifications(
        store,
        settings,
        now=NOW,
        token="token",
        chat_id="1",
        sender=lambda text: calls.append(text) or 200,
    )
    deliver_notifications(
        store,
        settings,
        now=NOW,
        token="token",
        chat_id="1",
        sender=lambda text: calls.append(text) or 200,
    )
    assert len(calls) == 1
    assert "Backend 2" in calls[0]
    assert store.get_job("remotive", "2").notification_status == "sent"  # type: ignore[union-attr]
    store.close()


def test_notification_error_does_not_roll_back_stored_jobs(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "jobs.sqlite"

    class Ok:
        name = "ok"
        default_interval_hours = 12
        default_enabled = True

        def fetch(self, ctx: FetchContext) -> FetchResult:
            return FetchResult(jobs=[_job("1")], status_code=200)

    def boom(*_args: object, **_kwargs: object) -> int:
        raise RuntimeError("telegram down")

    monkeypatch.setattr("latam_jobs.alerts.deliver_notifications", boom)
    settings = Settings(
        database=DatabaseSettings(path=str(database)),
        keywords=["backend"],
        notifications=NotificationSettings(enabled=True),
        sources={},
    )
    from latam_jobs.settings import SourceSettings

    settings = settings.model_copy(
        update={"sources": {"ok": SourceSettings(enabled=True, interval_hours=12)}}
    )
    collect(settings, registry={"ok": Ok()}, now=NOW)
    store = Store(database)
    assert store.count_jobs() == 1
    store.close()
