"""Command line entry point."""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

from latam_jobs.pipeline import collect, format_job
from latam_jobs.scheduler import LockError, RunLock, StopFlag
from latam_jobs.settings import ConfigError, config_path_from_env, load_env_file, load_settings
from latam_jobs.store import Store


def _cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    print(
        f"Settings OK: residence_country={settings.residence_country} "
        f"sources={len(settings.sources)} config={args.config}"
    )
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    lock = None
    stop = StopFlag()
    if not args.dry_run:
        lock = RunLock(Path(settings.database.path).with_suffix(".lock"))
        lock.acquire()
        stop.install()
    try:
        runs = collect(settings, only_source=args.source, dry_run=args.dry_run, stop=stop)
    finally:
        stop.close()
        if lock is not None:
            lock.release()
    for run in runs:
        if run.error:
            print(f"[{run.name}] failed: {run.error}", file=sys.stderr)
            continue
        if run.not_modified:
            print(f"[{run.name}] not modified")
            continue
        print(f"[{run.name}] {len(run.jobs)} jobs, {run.skipped_items} skipped")
        for job in run.jobs:
            print(format_job(job))
            print()
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    store = Store(Path(settings.database.path))
    try:
        store.migrate()
        schedules = store.list_schedules()
    finally:
        store.close()
    if not schedules:
        print("No source runs recorded yet.")
        return 0
    for schedule in schedules:
        print(
            f"{schedule.source_name} success={schedule.last_success_at} "
            f"next={schedule.next_fetch_at} failures={schedule.consecutive_failures} "
            f"status={schedule.last_http_status} error={schedule.last_error or '-'}"
        )
    return 0


def _cmd_list(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    store = Store(Path(settings.database.path))
    try:
        store.migrate()
        jobs = store.list_jobs(eligibility=args.eligibility, limit=args.limit)
    finally:
        store.close()
    for job in jobs:
        print(f"[{job.latam_eligibility}] {job.title} — {job.company} ({job.source}) {job.url}")
    print(f"{len(jobs)} jobs")
    return 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        default=None,
        help="Path to settings.yaml. Defaults to LATAM_JOBS_CONFIG, /etc, or ./settings.yaml.",
    )
    parser = argparse.ArgumentParser(prog="latam-jobs", description="Collect LATAM remote jobs.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    check = subparsers.add_parser("check", parents=[common], help="Validate settings and exit.")
    check.set_defaults(func=_cmd_check)
    run = subparsers.add_parser(
        "run", parents=[common], help="Fetch enabled sources and print jobs."
    )
    run.add_argument(
        "--source", default=None, help="Fetch one enabled source, ignoring the others."
    )
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print jobs without writing the database or sending notifications.",
    )
    run.set_defaults(func=_cmd_run)
    status = subparsers.add_parser("status", parents=[common], help="Show source schedules.")
    status.set_defaults(func=_cmd_status)
    listing = subparsers.add_parser("list", parents=[common], help="List stored jobs.")
    listing.add_argument("--eligibility", choices=("accepted", "rejected", "maybe"))
    listing.add_argument("--limit", type=int, default=50)
    listing.set_defaults(func=_cmd_list)
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.config is None:
        args.config = config_path_from_env()
    else:
        args.config = Path(args.config)
    try:
        return int(args.func(args))
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2
    except LockError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
