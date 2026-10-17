"""Command line entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from latam_jobs.settings import ConfigError, config_path_from_env, load_env_file, load_settings


def _cmd_check(args: argparse.Namespace) -> int:
    settings = load_settings(args.config)
    print(
        f"Settings OK: residence_country={settings.residence_country} "
        f"sources={len(settings.sources)} config={args.config}"
    )
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
    return parser


def main(argv: list[str] | None = None) -> int:
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


if __name__ == "__main__":
    raise SystemExit(main())
