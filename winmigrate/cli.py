"""Command-line interface.

``scan`` inventories a profile and previews the plan; ``capture`` writes that
plan into an encrypted bundle; ``inspect`` reads a bundle's header and sidecar
without a passphrase; ``restore`` puts a bundle back and reports what the user
must finish by hand.

Passphrases are read interactively by default and never appear in a command
line, where they would land in shell history and in the process list.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from rich.console import Console
from rich.progress import (
    BarColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)

from . import __version__, capture as capture_mod, logging_setup, presets, report, restore as restore_mod, vss
from .config import ScanConfig, config_from_dict, load_config_file
from .util import humanize
from .errors import ConfigError, WinMigrateError
from .capture import CaptureOptions
from .platform_win import Environment, require_windows
from .restore import RestoreOptions
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

    capture_parser = subparsers.add_parser(
        "capture", parents=[common], help="write the plan into an encrypted bundle"
    )
    _add_scan_arguments(capture_parser)
    _add_capture_arguments(capture_parser)
    capture_parser.set_defaults(func=cmd_capture)

    inspect_parser = subparsers.add_parser(
        "inspect", parents=[common], help="describe a bundle without decrypting it"
    )
    inspect_parser.add_argument("bundle", type=Path)
    inspect_parser.add_argument("--json", action="store_true", help="raw header and sidecar as JSON")
    inspect_parser.set_defaults(func=cmd_inspect)

    restore_parser = subparsers.add_parser(
        "restore", parents=[common], help="restore a bundle onto this machine"
    )
    _add_restore_arguments(restore_parser)
    restore_parser.set_defaults(func=cmd_restore)

    reinstall_parser = subparsers.add_parser(
        "reinstall",
        parents=[common],
        help="reinstall software from the files a restore wrote",
    )
    _add_reinstall_arguments(reinstall_parser)
    reinstall_parser.set_defaults(func=cmd_reinstall)

    presets_parser = subparsers.add_parser(
        "presets", parents=[common], help="list the exclusion presets you can apply"
    )
    presets_parser.set_defaults(func=cmd_presets)
    return parser


def _add_reinstall_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "directory",
        type=Path,
        help="the WinMigrate-Reinstall folder a restore wrote",
    )
    parser.add_argument(
        "--apps", action="store_true", help="run winget import for the captured applications"
    )
    parser.add_argument(
        "--office",
        type=Path,
        metavar="SETUP_EXE",
        help="run Office setup.exe with the generated configuration "
        "(setup.exe comes from the Office Deployment Tool, which you download)",
    )
    parser.add_argument("--yes", action="store_true", help="do not ask for confirmation")


def _add_capture_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "-o", "--output", type=Path, help="bundle path (default: <host>-<user>-<timestamp>.dat)"
    )
    parser.add_argument(
        "--passphrase-file",
        type=Path,
        help="read the passphrase from this file instead of prompting "
        "(for unattended runs; the file's own permissions are all that protect it)",
    )
    parser.add_argument(
        "--no-vss",
        dest="use_vss",
        action="store_false",
        help="do not create a shadow copy; files held open by programs may be unreadable",
    )
    parser.add_argument(
        "--no-space-check",
        dest="space_check",
        action="store_false",
        help="write even if the destination looks too small",
    )
    parser.add_argument(
        "--yes", action="store_true", help="do not ask for confirmation before writing"
    )
    parser.add_argument(
        "--passwords",
        action="append",
        default=[],
        metavar="BROWSER=CSV",
        help="include a password CSV you exported yourself, e.g. chrome=C:\\path\\pw.csv "
        "(repeatable; skips the interactive prompt for that browser)",
    )
    parser.add_argument(
        "--no-passwords",
        action="store_true",
        help="do not offer to include browser password exports",
    )


def _add_restore_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("bundle", type=Path)
    parser.add_argument(
        "-d", "--destination", type=Path, help="restore here instead of the current profile"
    )
    parser.add_argument("--passphrase-file", type=Path, help="read the passphrase from this file")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="report what would be restored, write nothing"
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="replace existing files that differ from the bundle (default: keep them)",
    )
    parser.add_argument(
        "--item",
        action="append",
        default=[],
        metavar="ID",
        help="restore only this item id (repeatable)",
    )


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
    parser.add_argument(
        "--exclude-preset",
        action="append",
        default=[],
        metavar="NAME",
        help="apply a named exclusion preset, e.g. device-backups or vm-images "
        "(repeatable; see 'winmigrate presets')",
    )
    parser.add_argument(
        "--no-software",
        dest="include_software",
        action="store_false",
        help="skip the installed-software and Office inventory (faster)",
    )
    parser.add_argument(
        "--include-wifi",
        action="store_true",
        help="include saved Wi-Fi profiles (their export contains the network passwords)",
    )
    parser.add_argument(
        "--no-notepad",
        dest="include_notepad",
        action="store_false",
        help="skip Notepad's unsaved tabs (they travel encrypted-only by default)",
    )
    parser.add_argument("--json", action="store_true", help="print the plan as JSON instead of a table")
    parser.add_argument(
        "--save-plan",
        type=Path,
        metavar="PATH",
        help="write the plan as JSON to PATH (the only file this command writes; "
        "it holds the full inventory in plain text, so put it somewhere you control)",
    )


def emit_json(payload: dict) -> None:
    """Write machine-readable JSON to stdout, bypassing rich entirely.

    ``console.print_json`` syntax-highlights its output and, on Windows, routes
    it through the legacy console writer, which raises UnicodeEncodeError as
    soon as a value contains a character the console code page lacks -- exactly
    what happens piping ``scan --json`` into another command. Machine output
    should not be styled anyway.

    UTF-8 is preferred; where the stream cannot represent a character, the
    escaped form is written instead, which is still valid JSON.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, OSError, ValueError):
        pass
    try:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    except UnicodeEncodeError:
        sys.stdout.write(json.dumps(payload, indent=2, ensure_ascii=True) + "\n")
    sys.stdout.flush()


def _config_preset_names(args: argparse.Namespace) -> list[str]:
    """Preset names named inside a --config file (key: exclude_presets)."""
    if not getattr(args, "config", None):
        return []
    data = load_config_file(args.config)
    value = data.get("exclude_presets", [])
    if not isinstance(value, list):
        raise ConfigError("config 'exclude_presets' must be a list of preset names")
    return [str(name) for name in value]


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
    config.include_software = args.include_software and config.include_software
    config.include_wifi = getattr(args, "include_wifi", False) or config.include_wifi
    config.include_notepad = getattr(args, "include_notepad", True) and config.include_notepad
    preset_names = list(getattr(args, "exclude_preset", []) or []) + list(
        _config_preset_names(args)
    )
    if preset_names:
        try:
            preset_patterns = presets.resolve(preset_names)
        except presets.UnknownPreset as exc:
            names = ", ".join(p.name for p in presets.list_presets())
            raise ConfigError(f"unknown exclusion preset {exc}; available: {names}") from exc
        config.extra_excludes = tuple(config.extra_excludes) + preset_patterns
        config.active_presets = tuple(dict.fromkeys(name.strip().lower() for name in preset_names))
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
        emit_json(plan)
    else:
        report.render_preview(result, console, verbose=args.verbose)
        if args.save_plan:
            console.print(f"[dim]plan written to {args.save_plan}[/dim]")
    return 0


def _read_passphrase(args: argparse.Namespace, *, confirm: bool) -> str:
    """Get the passphrase without ever putting it on a command line.

    A passphrase passed as an argument would be visible in shell history and in
    the process list to every other user on the machine, so there is no flag for
    one -- only an interactive prompt or a file the user controls.
    """
    import getpass  # noqa: PLC0415

    path = getattr(args, "passphrase_file", None)
    if path:
        text = Path(path).read_text(encoding="utf-8").strip("\r\n")
        if not text:
            raise ConfigError(f"passphrase file is empty: {path}")
        return text
    passphrase = getpass.getpass("Passphrase: ")
    if not passphrase:
        raise ConfigError("a passphrase is required; the bundle is always encrypted")
    if confirm and getpass.getpass("Confirm passphrase: ") != passphrase:
        raise ConfigError("the passphrases did not match")
    return passphrase


def cmd_capture(args: argparse.Namespace, console: Console) -> int:
    require_windows(allow_override=args.profile_root is not None or _override_allowed())
    config = _config_from_args(args)
    env = Environment.fixture(config.profile_root) if config.profile_root else Environment.live()

    result = run_scan(config, env)
    report.render_preview(result, console, verbose=args.verbose)

    totals = result.totals()
    if totals.capture_files == 0:
        console.print("[yellow]Nothing to capture.[/yellow]")
        return 1

    output = Path(args.output) if args.output else Path(capture_mod.default_bundle_name(result))
    ok, free = capture_mod.check_free_space(output, totals.capture_bytes)
    console.print(
        f"\nDestination [bold]{output}[/bold] — "
        f"{humanize.bytes_(free)} free, plan needs about "
        f"{humanize.bytes_(totals.capture_bytes)}"
        + ("" if ok else " [red](not enough)[/red]")
    )

    _warn_if_no_shadow_copy(args, console, totals)

    if not args.yes:
        console.print(
            "[dim]The bundle is encrypted with a passphrase only you hold. "
            "There is no recovery if you lose it.[/dim]"
        )
        answer = console.input("Write this bundle? [y/N] ").strip().lower()
        if answer not in {"y", "yes"}:
            console.print("Nothing was written.")
            return 1

    shred_after = _collect_browser_passwords(args, result, env, console)

    passphrase = _read_passphrase(args, confirm=True)
    options = CaptureOptions(
        output=output,
        passphrase=passphrase,
        use_vss=args.use_vss,
        skip_space_check=not args.space_check,
    )

    with Progress(
        SpinnerColumn(),
        TextColumn("[cyan]{task.description}"),
        BarColumn(),
        TransferSpeedColumn(),
        TextColumn("{task.completed:,} / {task.total:,} bytes"),
        TimeRemainingColumn(),
        console=console,
        transient=True,
    ) as bar:
        task = bar.add_task("capturing", total=max(totals.capture_bytes, 1))

        def on_progress(title: str, size: int) -> None:
            bar.update(task, advance=size, description=title)

        capture_report = capture_mod.capture(result, options, config, env, on_progress)

    _shred_password_csvs(shred_after, console)
    report.render_capture_report(capture_report, console)
    # 3, not 0: the bundle is valid and worth keeping, but it is not the whole
    # plan, and a script that moves it to the NAS and wipes the source machine
    # has to be able to tell the difference.
    return 0 if not capture_report.failures else 3


def _warn_if_no_shadow_copy(args, console: Console, totals) -> None:
    """Say that a shadow copy is not available *before* the user commits.

    The check itself lived inside the capture, so the warning arrived after the
    passphrase prompt with the progress bar already running -- on a 217 GiB
    profile, two hours after the only moment the user could have acted on it.
    Restarting elevated is cheap before the capture and expensive during it, so
    the question is asked here, with the size in front of the user.
    """
    if not args.use_vss or not vss.is_windows() or vss.is_elevated():
        return
    console.print(
        f"\n[yellow]Not running as administrator, so there will be no shadow copy.[/yellow]\n"
        f"[dim]Files that running programs hold open -- browser databases, Outlook, "
        f"anything mid-write -- may be unreadable or copied in a torn state. They are "
        f"reported at the end rather than silently missed.\n"
        f"To include them, stop now and re-run this from an elevated prompt "
        f"(right-click Terminal or PowerShell, Run as administrator). About "
        f"{humanize.bytes_(totals.capture_bytes)} is planned, so this is the cheap "
        f"moment to decide.[/dim]"
    )


def _collect_browser_passwords(args, result, env, console) -> list[Path]:
    """Add browser password CSVs to the capture, per the agreed handoff.

    WinMigrate never reads a password store: the browser exports the CSV behind
    its own Windows Hello prompt, and this only ingests what the user produced.
    Returns the CSV paths to shred once the bundle is written.

    Two ways in: --passwords BROWSER=CSV for an unattended run, or an
    interactive prompt per browser whose passwords are not already synced.
    """
    from . import passwords as passwords_mod

    if getattr(args, "no_passwords", False) or result.files_only:
        return []

    targets = {t.browser_key: t for t in passwords_mod.export_targets(env)}
    to_shred: list[Path] = []

    # Non-interactive: explicit --passwords BROWSER=CSV pairs.
    explicit: dict[str, str] = {}
    for pair in getattr(args, "passwords", []) or []:
        key, _, path = pair.partition("=")
        if not path:
            raise ConfigError(f"--passwords expects BROWSER=CSV, got {pair!r}")
        explicit[key.strip().lower()] = path.strip()

    for key, path in explicit.items():
        target = targets.get(key)
        if target is None:
            console.print(f"[yellow]No browser {key!r} with local passwords; skipping.[/yellow]")
            continue
        # A file the user pointed us at is theirs to manage; we do not shred it.
        _ingest_password_csv(target, Path(path), result, console, passwords_mod)

    # Interactive: offer each remaining target, unless this is an unattended run.
    interactive = not args.yes and not getattr(args, "passphrase_file", None)
    if interactive:
        for key, target in targets.items():
            if key in explicit:
                continue
            console.print(
                f"\n[bold]{target.title}[/bold] has passwords saved locally (sync is off)."
            )
            if console.input(
                f"Export them from {target.title} into the bundle now? [y/N] "
            ).strip().lower() not in {"y", "yes"}:
                continue
            opened = passwords_mod.open_export_page(target, env)
            # The address is printed either way. These are internal browser URLs
            # that differ between versions, so if the browser lands somewhere
            # other than the export screen the user needs to know where to go --
            # and finding that out after the browser opened is too late.
            lead = (
                f"{target.title} should have opened here:"
                if opened
                else f"Could not launch {target.title} here. Open it yourself and go to:"
            )
            console.print(
                f"[dim]{lead}[/dim]\n    [bold]{target.export_page}[/bold]\n"
                "[dim]Use 'Export passwords' (it will ask for Windows Hello), save the "
                "CSV, then paste its path below.[/dim]"
            )
            raw = console.input("Path to the exported CSV (blank to skip): ").strip().strip('"')
            if not raw:
                continue
            item = _ingest_password_csv(target, Path(raw), result, console, passwords_mod)
            if item is not None:
                to_shred.append(Path(raw))

    return to_shred


def _ingest_password_csv(target, csv_path: Path, result, console, passwords_mod):
    """Validate a CSV and add it to the scan as an encrypted-only item."""
    if not csv_path.is_file():
        console.print(f"[yellow]No file at {csv_path}; skipping {target.title}.[/yellow]")
        return None
    try:
        head = csv_path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        console.print(f"[yellow]Could not read {csv_path}: {exc}; skipping.[/yellow]")
        return None
    if not passwords_mod.looks_like_password_csv(head):
        console.print(
            f"[yellow]{csv_path} does not look like a password export "
            "(no url/username/password header); skipping.[/yellow]"
        )
        return None
    item = passwords_mod.build_password_item(target, csv_path)
    # Replace the scan-time "export yourself" note with the restore-side import
    # instruction for this browser (same follow-up id).
    result.followups = [f for f in result.followups if f.id != f"browser:passwords:{target.browser_key}"]
    result.followups.append(passwords_mod.import_followup(target))
    result.items.append(item)
    console.print(f"[green]{target.title} passwords added (encrypted-only).[/green]")
    return item


def _shred_password_csvs(paths: list[Path], console: Console) -> None:
    from . import passwords as passwords_mod

    if not paths:
        return
    if console.input(
        "\nShred the plaintext CSV(s) you exported now that they are in the bundle? [Y/n] "
    ).strip().lower() in {"n", "no"}:
        console.print("[dim]Left in place. Delete them yourself once you have verified the bundle.[/dim]")
        return
    for path in paths:
        console.print(("[green]shredded[/green] " if passwords_mod.shred(path) else "[yellow]could not shred[/yellow] ") + str(path))


def cmd_inspect(args: argparse.Namespace, console: Console) -> int:
    header = restore_mod.inspect(args.bundle)
    sidecar = restore_mod.load_sidecar(Path(args.bundle))
    if args.json:
        emit_json({"header": header, "sidecar": sidecar})
    else:
        report.render_bundle_summary(header, sidecar, console)
    checked, error = restore_mod.verify_sidecar(Path(args.bundle))
    if error:
        console.print(f"[bold red]integrity:[/bold red] {error} — do not trust this bundle.[/bold red]")
        return 2
    console.print(
        "[green]integrity: matches the sidecar digest (transfer intact)[/green]"
        if checked
        else "[dim]integrity: no sidecar manifest beside the bundle to check against[/dim]"
    )
    return 0


def cmd_restore(args: argparse.Namespace, console: Console) -> int:
    require_windows(allow_override=args.destination is not None or _override_allowed())
    passphrase = _read_passphrase(args, confirm=False)
    options = RestoreOptions(
        bundle=args.bundle,
        passphrase=passphrase,
        destination=args.destination,
        dry_run=args.dry_run,
        overwrite=args.overwrite,
        items=tuple(args.item),
    )
    with console.status("[cyan]restoring…"):
        restore_report = restore_mod.restore(options)
    report.render_restore_report(restore_report, console, dry_run=args.dry_run)
    return 0 if restore_report.ok else 3


def cmd_presets(args: argparse.Namespace, console: Console) -> int:
    from rich.table import Table

    table = Table(title="Exclusion presets", title_justify="left", expand=False)
    table.add_column("Name")
    table.add_column("Patterns", justify="right")
    table.add_column("What it leaves out", overflow="fold")
    for preset in presets.list_presets():
        table.add_row(preset.name, str(len(preset.patterns)), preset.summary)
    console.print(table)
    console.print(
        "\n[dim]Apply with:  winmigrate scan --exclude-preset device-backups "
        "--exclude-preset vm-images[/dim]"
    )
    return 0


def cmd_reinstall(args: argparse.Namespace, console: Console) -> int:
    """Replay the reinstall files a restore wrote. Always asks before installing."""
    from . import reinstall as reinstall_mod

    directory = Path(args.directory)
    if directory.name != reinstall_mod.ARTIFACTS_DIRECTORY and (
        directory / reinstall_mod.ARTIFACTS_DIRECTORY
    ).is_dir():
        directory = directory / reinstall_mod.ARTIFACTS_DIRECTORY
    if not directory.is_dir():
        raise ConfigError(f"no reinstall folder at {directory}")

    artifacts = reinstall_mod.Artifacts(directory=directory)
    import_file = directory / reinstall_mod.WINGET_IMPORT_FILE
    config_file = directory / reinstall_mod.OFFICE_CONFIG_FILE
    manual_file = directory / reinstall_mod.MANUAL_LIST_FILE
    artifacts.winget_import = import_file if import_file.is_file() else None
    artifacts.office_configuration = config_file if config_file.is_file() else None
    artifacts.manual_list = manual_file if manual_file.is_file() else None
    if artifacts.winget_import:
        export = json.loads(artifacts.winget_import.read_text(encoding="utf-8"))
        artifacts.reinstallable_count = sum(
            len(source.get("Packages", []) or []) for source in export.get("Sources", []) or []
        )

    report.render_reinstall_plan(artifacts, console)

    if not args.apps and args.office is None:
        console.print(
            "\n[dim]Nothing was installed. Add --apps to run the winget import, or "
            "--office <setup.exe> to run the Office configuration.[/dim]"
        )
        return 0

    if not args.yes:
        console.print(
            "\n[yellow]This installs software on this machine. It can take a long "
            "time, may prompt for elevation, and installs current versions rather "
            "than the exact versions from the old machine.[/yellow]"
        )
        if console.input("Go ahead? [y/N] ").strip().lower() not in {"y", "yes"}:
            console.print("Nothing was installed.")
            return 1

    status = 0
    if args.apps:
        if artifacts.winget_import is None:
            console.print("[yellow]No winget import file in this folder.[/yellow]")
            status = 1
        else:
            console.print("[cyan]Running winget import — this takes a while…[/cyan]")
            result = reinstall_mod.run_winget_import(artifacts.winget_import)
            console.print(result.stdout.strip() or "[dim](no output)[/dim]")
            if not result.ok:
                console.print(f"[yellow]winget finished with: {result.summary()}[/yellow]")
                console.print(
                    "[dim]winget reports a non-zero exit when any single package fails; "
                    "the others still installed.[/dim]"
                )
                status = status or 0

    if args.office is not None:
        if artifacts.office_configuration is None:
            console.print("[yellow]No Office configuration in this folder.[/yellow]")
            status = 1
        elif not Path(args.office).is_file():
            raise ConfigError(f"Office setup.exe not found: {args.office}")
        else:
            console.print("[cyan]Running Office setup…[/cyan]")
            result = reinstall_mod.run_office_install(
                Path(args.office), artifacts.office_configuration
            )
            if not result.ok:
                console.print(f"[red]Office setup failed: {result.summary()}[/red]")
                status = 2
            else:
                console.print("[green]Office installed. Activation is still yours to complete.[/green]")

    if artifacts.manual_list:
        console.print(
            f"\n[bold]Still by hand:[/bold] {artifacts.manual_list}"
        )
    return status


def _override_allowed() -> bool:
    import os

    return os.environ.get("WINMIGRATE_ALLOW_NON_WINDOWS") == "1"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    console = Console(stderr=False, quiet=args.quiet)
    logging_setup.configure(
        args.log_file, verbose=args.verbose, quiet=args.quiet, console=console
    )
    try:
        return args.func(args, console)
    except WinMigrateError as exc:
        console.print(f"[bold red]error:[/bold red] {exc}")
        log.error("%s", exc, exc_info=True)
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]interrupted; nothing was written[/yellow]")
        return 130
    except UnicodeEncodeError as exc:
        # The console's code page cannot represent something being printed --
        # a path or an application name from another script, typically.
        log.error("console encoding failure", exc_info=True)
        sys.stderr.write(
            "error: this console cannot display some of the characters in the "
            f"output ({exc.encoding}).\n"
            "Try 'chcp 65001' first, or set PYTHONIOENCODING=utf-8, or use "
            "--json / --save-plan to write the result to a file instead.\n"
        )
        return 2


if __name__ == "__main__":
    sys.exit(main())
