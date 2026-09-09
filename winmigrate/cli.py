"""Command-line interface.

Phase 1 ships the read-only half of the tool: ``scan`` (and its alias
``preview``), which inventories the current user's profile and prints exactly
what a capture would and would not take. Nothing here writes to the profile.

Planned subcommands, in the order they land: ``capture``, ``package``,
``restore``. They are deliberately absent rather than stubbed, so ``--help``
never advertises something that does not work.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console

from . import __version__, logging_setup, report
from .config import ScanConfig, config_from_dict, load_config_file
from .errors import WinMigrateError
from .platform_win import Environment, require_windows
from .scan import run_scan

log = logging.getLogger(__name__)

EPILOG = """\
WinMigrate is owner-run: it shows what it is doing, keeps everything local, and
leaves anything that needs your identity (account sign-ins, licence activation)
for you to complete, with instructions.
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="winmigrate",
        description="Windows user-profile migration and backup, run by the profile's owner.",
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"winmigrate {__version__}")
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--log-file", type=Path, help="write a detailed log here")
    common.add_argument("-v", "--verbose", action="store_true", help="show every item and debug logging")
    common.add_argument("-q", "--quiet", action="store_true", help="only report errors")
    common.add_argument("--config", type=Path, help="JSON file of scan settings")

    subparsers = parser.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("scan", "inventory this profile and print what a capture would take"),
        ("preview", "alias for 'scan'"),
    ):
        scan_parser = subparsers.add_parser(name, parents=[common], help=help_text)
        _add_scan_arguments(scan_parser)
        scan_parser.set_defaults(func=cmd_scan)
    return parser


def _add_scan_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--profile-root",
        type=Path,
        help="scan this directory instead of the live profile (development and testing)",
    )
    parser.add_argument(
        "--no-skip-synced",
        dest="skip_synced",
        action="store_false",
        help="capture cloud-synced folders too, instead of relying on sign-in",
    )
    parser.add_argument(
        "--include-regenerable",
        action="store_true",
        help="keep node_modules, build output and similar regenerable directories",
    )
    parser.add_argument(
        "--files-only",
        action="store_true",
        help="exclude all credential material from the plan",
    )
    parser.add_argument(
        "--fast",
        dest="measure_skipped",
        action="store_false",
        help="do not measure the size of what is being skipped (faster, vaguer report)",
    )
    parser.add_argument(
        "--exclude",
        action="append",
        default=[],
        metavar="PATTERN",
        help="additional exclusion pattern (repeatable)",
    )
    parser.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="PATTERN",
        help="pattern that overrides an exclusion (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print the plan as JSON instead of a table")
    parser.add_argument(
        "--save-plan",
        type=Path,
        metavar="PATH",
        help="write the plan as JSON to PATH (the only file this command writes)",
    )


def _config_from_args(args: argparse.Namespace) -> ScanConfig:
    config = ScanConfig()
    if args.config:
        config = config_from_dict(load_config_file(args.config), config)
    if args.profile_root is not None:
        config.profile_root = args.profile_root
    config.skip_synced = args.skip_synced and config.skip_synced
    config.include_regenerable = args.include_regenerable or config.include_regenerable
    config.files_only = args.files_only or config.files_only
    config.measure_skipped = args.measure_skipped and config.measure_skipped
    if args.exclude:
        config.extra_excludes = tuple(config.extra_excludes) + tuple(args.exclude)
    if args.include:
        config.extra_includes = tuple(config.extra_includes) + tuple(args.include)
    config._exclude_cache = None
    return config


def cmd_scan(args: argparse.Namespace, console: Console) -> int:
    # A scan against a fixture tree is the supported way to develop off Windows;
    # a scan of a live profile is not, so the platform check still applies.
    require_windows(allow_override=args.profile_root is not None or _override_allowed())

    config = _config_from_args(args)
    env = Environment.fixture(config.profile_root) if config.profile_root else Environment.live()

    status = None
    progress = None
    if not args.quiet and not args.json:
        status = console.status("[cyan]scanning…")
        status.start()
        progress = lambda message: status.update(f"[cyan]{message}…")  # noqa: E731

    try:
        result = run_scan(config, env, progress)
    finally:
        if status is not None:
            status.stop()

    plan = report.preview_to_dict(result)
    if args.save_plan:
        args.save_plan.parent.mkdir(parents=True, exist_ok=True)
        args.save_plan.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        log.info("plan written: %s", args.save_plan)

    if args.json:
        console.print_json(data=plan)
    else:
        report.render_preview(result, console, verbose=args.verbose)
        if args.save_plan:
            console.print(f"[dim]plan written to {args.save_plan}[/dim]")
    return 0


def _override_allowed() -> bool:
    import os

    return os.environ.get("WINMIGRATE_ALLOW_NON_WINDOWS") == "1"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging_setup.configure(args.log_file, verbose=args.verbose, quiet=args.quiet)
    console = Console(stderr=False, quiet=args.quiet)
    try:
        return args.func(args, console)
    except WinMigrateError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        log.error("%s", exc, exc_info=True)
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted; nothing was written[/yellow]")
        return 130


if __name__ == "__main__":
    sys.exit(main())
