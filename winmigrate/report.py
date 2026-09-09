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

from .models import Action, Item, ScanResult, Severity, SkipReason
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
            disposition = _disposition(item)
            table.add_row(
                Text(marker, style=ACTION_STYLE[item.action]),
                item.title + (" [red](secret)[/red]" if item.is_secret else ""),
                pathutil.display(item.source_path or "", result.source.profile_path),
                humanize.bytes_(item.size_bytes) if item.action is Action.CAPTURE else "—",
                f"{item.file_count:,}" if item.action is Action.CAPTURE else "—",
                disposition,
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


def preview_to_dict(result: ScanResult) -> dict[str, Any]:
    """Machine-readable form of the preview, for ``--json``."""
    from . import manifest as manifest_mod

    return manifest_mod.public_view(manifest_mod.build(result))
