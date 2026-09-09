"""Console rendering of a scan: the preview the user approves before anything happens.

The preview is the tool's central promise -- it says exactly what would be
captured, exactly what would be skipped and why, and what the user will still
have to do by hand afterwards. Capture consumes the same
:class:`~winmigrate.models.ScanResult`, so the preview cannot drift from what
actually happens.
"""

from __future__ import annotations

from typing import Any

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from .models import Action, Item, Kind, ScanResult, Severity, SkipReason
from .util import humanize
from .util import paths as pathutil

ACTION_STYLE = {
    Action.CAPTURE: "green",
    Action.SKIP: "dim",
    Action.MANUAL: "yellow",
}

SKIP_LABEL = {
    SkipReason.SYNCED: "already synced",
    SkipReason.EXCLUDED: "excluded",
    SkipReason.REGENERABLE: "regenerable",
    SkipReason.CLOUD_PLACEHOLDER: "online-only",
    SkipReason.EMPTY: "empty",
    SkipReason.UNREADABLE: "unreadable",
    SkipReason.NOT_PRESENT: "not present",
    SkipReason.REPARSE_POINT: "junction",
    SkipReason.FILES_ONLY_MODE: "files-only mode",
}


def render_preview(result: ScanResult, console: Console, *, verbose: bool = False) -> None:
    """Print the full preview. Writes nothing to disk."""
    _render_header(result, console)
    _render_sync_roots(result, console)
    _render_items(result, console, verbose=verbose)
    _render_skipped(result, console)
    _render_followups(result, console)
    _render_notes(result, console)
    _render_totals(result, console)


def _render_header(result: ScanResult, console: Console) -> None:
    source = result.source
    mode = "files-only (no secrets)" if result.files_only else "full"
    lines = [
        f"[bold]{source.hostname or 'this machine'}[/bold]  •  user [bold]{source.username or '?'}[/bold]",
        f"profile: {source.profile_path}",
        f"os: {source.os_name} {source.os_version} {source.os_build} ({source.architecture})",
        f"mode: {mode}   scanned in {humanize.duration(result.duration_seconds)}",
    ]
    console.print(Panel("\n".join(lines), title="WinMigrate — scan preview", border_style="cyan"))
    if not source.is_windows:
        console.print(
            "[yellow]Running off Windows: this is a fixture/development scan, "
            "not a real profile.[/yellow]\n"
        )


def _render_sync_roots(result: ScanResult, console: Console) -> None:
    if not result.sync_roots:
        return
    table = Table(title="Cloud-synced folders (not captured)", title_justify="left", expand=False)
    table.add_column("Provider")
    table.add_column("Folder")
    table.add_column("Account")
    table.add_column("Size", justify="right")
    table.add_column("Files", justify="right")
    for root in result.sync_roots:
        table.add_row(
            root.provider,
            pathutil.display(root.root, result.source.profile_path),
            root.account_hint or "—",
            humanize.bytes_(root.bytes_skipped),
            f"{root.files_skipped:,}",
        )
    console.print(table)
    console.print(
        "[dim]These reach the new machine by signing in to the provider, "
        "so copying them into the bundle would only double its size.[/dim]\n"
    )


def _render_items(result: ScanResult, console: Console, *, verbose: bool) -> None:
    grouped = result.items_by_category()
    for category, items in grouped.items():
        visible = [item for item in items if verbose or _is_interesting(item)]
        if not visible:
            continue
        table = Table(
            title=category.value.replace("_", " ").title(),
            title_justify="left",
            expand=False,
        )
        table.add_column("", width=1)
        table.add_column("Item", no_wrap=True)
        table.add_column("Source", no_wrap=True, overflow="ellipsis", max_width=34)
        table.add_column("Size", justify="right", no_wrap=True)
        table.add_column("Files", justify="right", no_wrap=True)
        table.add_column("Disposition", overflow="fold")
        for item in visible:
            marker = "✓" if item.action is Action.CAPTURE else ("!" if item.action is Action.MANUAL else "·")
            # A record carries no bytes; showing "0 B / 0 files" reads as a
            # failure to capture something rather than as a different shape.
            has_bytes = item.kind in {Kind.TREE, Kind.FILE} and item.action is Action.CAPTURE
            table.add_row(
                Text(marker, style=ACTION_STYLE[item.action]),
                item.title + (" [red](secret)[/red]" if item.is_secret else ""),
                pathutil.display(item.source_path or "", result.source.profile_path),
                humanize.bytes_(item.size_bytes) if has_bytes else "—",
                f"{item.file_count:,}" if has_bytes else "—",
                _disposition(item),
                style=None if item.action is Action.CAPTURE else "dim",
            )
        console.print(table)
        console.print()


def _is_interesting(item: Item) -> bool:
    """Hide empty/absent folders unless --verbose; show anything with content."""
    if item.action is not Action.SKIP:
        return True
    if item.skip_reason in {SkipReason.NOT_PRESENT, SkipReason.EMPTY}:
        return False
    return True


def _disposition(item: Item) -> str:
    if item.action is Action.CAPTURE:
        target = _short_target(item.restore.target) if item.restore else "—"
        skipped = item.skipped_bytes
        suffix = f"  [dim](−{humanize.bytes_(skipped)} skipped)[/dim]" if skipped else ""
        return f"→ {target}{suffix}"
    if item.action is Action.MANUAL:
        return "[yellow]needs you — see follow-ups[/yellow]"
    label = SKIP_LABEL.get(item.skip_reason, item.skip_reason.value if item.skip_reason else "skipped")
    return f"skipped ({label})"


def _short_target(target: str) -> str:
    """``%USERPROFILE%\\Documents`` reads better as ``~\\Documents`` in a table."""
    for variable in ("%USERPROFILE%", "%APPDATA%", "%LOCALAPPDATA%"):
        if target.upper().startswith(variable):
            return "~" + target[len(variable) :] if variable == "%USERPROFILE%" else target
    return target


def _render_skipped(result: ScanResult, console: Console) -> None:
    """Summarize why bytes are not in the bundle, largest reason first."""
    by_reason: dict[SkipReason, tuple[int, int]] = {}
    for item in result.items:
        for group in item.skipped:
            total, files = by_reason.get(group.reason, (0, 0))
            by_reason[group.reason] = (total + group.bytes, files + group.files)
        if item.action is Action.SKIP and item.skip_reason and item.size_bytes:
            total, files = by_reason.get(item.skip_reason, (0, 0))
            by_reason[item.skip_reason] = (total + item.size_bytes, files + item.file_count)
    by_reason = {reason: value for reason, value in by_reason.items() if value[0] or value[1]}
    if not by_reason:
        return
    table = Table(title="Not captured", title_justify="left", expand=False)
    table.add_column("Reason")
    table.add_column("Size", justify="right")
    table.add_column("Files", justify="right")
    for reason, (size, files) in sorted(by_reason.items(), key=lambda kv: -kv[1][0]):
        table.add_row(SKIP_LABEL.get(reason, reason.value), humanize.bytes_(size), f"{files:,}")
    console.print(table)
    console.print()


def _render_followups(result: ScanResult, console: Console) -> None:
    if not result.followups:
        return
    console.print("[bold]After restoring, you will still need to:[/bold]")
    for index, followup in enumerate(result.followups, start=1):
        console.print(f"  [bold]{index}. {followup.title}[/bold]")
        console.print(f"     [dim]{followup.why}[/dim]")
        for step in followup.steps:
            console.print(f"       • {step}")
    console.print()


def _render_notes(result: ScanResult, console: Console) -> None:
    notes = [note for note in result.notes if note.severity is not Severity.INFO]
    item_notes = [
        (item, note)
        for item in result.items
        for note in item.notes
        if note.severity is not Severity.INFO
    ]
    if not notes and not item_notes:
        return
    console.print("[bold yellow]Warnings[/bold yellow]")
    for note in notes:
        console.print(f"  • {note.message}" + (f" [dim]({note.detail})[/dim]" if note.detail else ""))
    for item, note in item_notes:
        console.print(f"  • {item.title}: {note.message}")
    console.print()


def _render_totals(result: ScanResult, console: Console) -> None:
    totals = result.totals()
    lines = [
        f"capture: [bold green]{humanize.bytes_(totals.capture_bytes)}[/bold green] "
        f"in {humanize.count(totals.capture_files, 'file')}",
        f"skipped: {humanize.bytes_(totals.skipped_bytes)} "
        f"in {humanize.count(totals.skipped_files, 'file')}",
        f"items: {totals.item_count}  •  needing you afterwards: {len(result.followups)}",
    ]
    if totals.secret_item_count:
        lines.append(
            f"secret items: {totals.secret_item_count} "
            "[dim](encrypted bundle only; never in the sidecar manifest or log)[/dim]"
        )
    console.print(Panel("\n".join(lines), title="Totals", border_style="green"))
    console.print(
        "[dim]This was a read-only scan. Nothing was copied, changed, or sent anywhere.[/dim]"
    )


def render_reinstall_plan(artifacts, console: Console) -> None:
    """Show what a reinstall run would do, before it does any of it."""
    table = Table(title="Reinstall plan", title_justify="left", expand=False)
    table.add_column("Step")
    table.add_column("Detail", overflow="fold")
    if artifacts.winget_import:
        table.add_row(
            f"winget import ({artifacts.reinstallable_count} apps)", str(artifacts.winget_import)
        )
    if artifacts.office_configuration:
        office = artifacts.office
        detail = str(artifacts.office_configuration)
        if office is not None:
            detail += f"\n{', '.join(office.titles)} — {office.platform}, channel {office.channel}"
        table.add_row("Office (needs setup.exe from the ODT)", detail)
    if artifacts.manual_list:
        table.add_row(f"by hand ({artifacts.manual_count} apps)", str(artifacts.manual_list))
    console.print(table)


def preview_to_dict(result: ScanResult) -> dict[str, Any]:
    """Machine-readable form of the preview, for ``--json``."""
    from . import manifest as manifest_mod

    return manifest_mod.public_view(manifest_mod.build(result))


# --- capture and restore reporting ----------------------------------------
def render_capture_report(report, console: Console) -> None:
    """Print what capture actually did."""
    from .util import humanize as _h

    lines = [
        f"bundle: [bold]{report.bundle_path}[/bold]",
        f"captured: [bold green]{_h.bytes_(report.captured_bytes)}[/bold green] "
        f"in {_h.count(report.captured_files, 'file')}",
        f"bundle size: {_h.bytes_(report.bundle_bytes)} "
        f"({report.compression_ratio:.0%} of the source bytes)",
        f"took {_h.duration(report.duration_seconds)}",
        f"shadow copy: {'used' if report.used_shadow_copy else 'not used'}",
    ]
    console.print(Panel("\n".join(lines), title="Capture complete", border_style="green"))

    for note in report.notes:
        if note.severity is not Severity.INFO:
            console.print(f"[yellow]![/yellow] {note.message}" + (f" [dim]({note.detail})[/dim]" if note.detail else ""))

    if report.failures:
        table = Table(title=f"Could not capture ({len(report.failures)})", title_justify="left")
        table.add_column("Path", overflow="ellipsis", max_width=60)
        table.add_column("Reason")
        for path, reason in report.failures[:20]:
            table.add_row(path, reason)
        console.print(table)
        if len(report.failures) > 20:
            console.print(f"[dim]…and {len(report.failures) - 20} more; see the log.[/dim]")
        console.print(
            "[dim]Locked files are usually captured cleanly by running the capture "
            "from an elevated prompt, which allows a shadow copy.[/dim]"
        )
    console.print(
        f"[dim]Keep {report.manifest_path.name} beside the bundle: it lets the bundle be "
        "identified and integrity-checked without the passphrase.[/dim]"
    )


def render_restore_report(report, console: Console, *, dry_run: bool | None = None) -> None:
    """Print what restore did, then what the user must still do themselves.

    A dry run must never look like a completed restore. It is titled as a
    preview, and it says outright that nothing was written -- otherwise the
    absence of the destination folder afterwards reads as a failure.
    """
    from .util import humanize as _h

    dry_run = report.dry_run if dry_run is None else dry_run
    verb = "would restore" if dry_run else "restored"
    lines = [
        f"{verb}: [bold green]{_h.bytes_(report.restored_bytes)}[/bold green] "
        f"in {_h.count(report.restored_files, 'file')}",
    ]
    if report.destination:
        lines.append(
            f"{'would go to' if dry_run else 'destination'}: [bold]{report.destination}[/bold]"
        )
    if report.skipped_existing:
        lines.append(
            f"already present and matching: {report.skipped_existing:,} "
            "[dim](a re-run picks up where it left off)[/dim]"
        )
    if report.kept_existing:
        lines.append(f"[yellow]kept existing, differing files: {report.kept_existing:,}[/yellow]")
    lines.append(
        "integrity: "
        + ("[green]verified against the sidecar manifest[/green]" if report.verified
           else "authenticated by the bundle's own tags")
    )
    lines.append(f"took {_h.duration(report.duration_seconds)}")
    if dry_run:
        title, border = "Dry run — nothing was written", "cyan"
    elif report.ok:
        title, border = "Restore complete", "green"
    else:
        title, border = "Restore finished with problems", "red"
    console.print(Panel("\n".join(lines), title=title, border_style=border))
    if dry_run:
        console.print(
            "[dim]This was a preview. No files were written and "
            + (f"{report.destination} was not created. " if report.destination else "")
            + "Re-run without --dry-run to restore for real.[/dim]"
        )

    if report.digest_mismatches:
        console.print(
            f"[bold red]Digest mismatch on {len(report.digest_mismatches)} item(s):[/bold red] "
            + ", ".join(report.digest_mismatches)
        )
        console.print(
            "[red]The restored files do not match what the manifest recorded. "
            "Do not delete the source machine.[/red]"
        )

    if report.failures:
        table = Table(title=f"Could not restore ({len(report.failures)})", title_justify="left")
        table.add_column("Path", overflow="ellipsis", max_width=60)
        table.add_column("Reason")
        for path, reason in report.failures[:20]:
            table.add_row(path, reason)
        console.print(table)

    for note in report.notes:
        if note.severity is not Severity.INFO:
            console.print(f"[yellow]![/yellow] {note.message}" + (f" [dim]({note.detail})[/dim]" if note.detail else ""))

    artifacts = getattr(report, "artifacts", None)
    if artifacts is not None:
        console.print()
        lines = []
        if artifacts.winget_import:
            lines.append(
                f"{artifacts.reinstallable_count} application(s) can be reinstalled by winget"
            )
        if artifacts.manual_count:
            lines.append(f"{artifacts.manual_count} need installing by hand — see the list")
        if getattr(artifacts, "component_count", 0):
            lines.append(
                f"[dim]{artifacts.component_count} runtimes/drivers listed for "
                "completeness; nothing to do[/dim]"
            )
        if artifacts.office_configuration:
            lines.append("an Office configuration matching the old install was written")
        lines.append(f"files: [bold]{artifacts.directory}[/bold]")
        lines.append(
            "run [bold]winmigrate reinstall "
            f'"{artifacts.directory}"[/bold] when you are ready'
        )
        console.print(
            Panel("\n".join(lines), title="Software — nothing installed yet", border_style="cyan")
        )

    if report.followups:
        console.print()
        console.print("[bold]Now finish these yourself — they need your identity, not the tool's:[/bold]")
        for index, followup in enumerate(report.followups, start=1):
            console.print(f"  [bold]{index}. {followup.title}[/bold]")
            if followup.why:
                console.print(f"     [dim]{followup.why}[/dim]")
            for step in followup.steps:
                console.print(f"       • {step}")
