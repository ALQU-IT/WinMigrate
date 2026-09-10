"""Turning "the user ticked these boxes" into a plan the capture already understands.

The GUI does not get its own capture path. It produces exactly the same
:class:`~winmigrate.models.ScanResult` the command line produces, with the
unticked items marked as skipped, and hands it to the same
:func:`winmigrate.capture.capture`. Anything else would mean two implementations
of what goes in a bundle, and the one nobody tests would be the one that is
wrong.

Deselecting is deliberately its own skip reason. "Excluded" means a pattern
matched, "empty" means there was nothing there -- both are the tool's findings.
``deselected`` is the user's decision, and the manifest should say which of the
two left something out, because six months later that is the only record of why
a folder is missing from a backup.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..models import Action, Item, Kind, ScanResult, SkipReason

#: Kinds the user is offered a choice about. RECORD and REPORT items carry the
#: report itself -- printers, the software inventory, the sync-root
#: explanations -- and are what makes a restore legible, so they are not
#: something to untick by accident.
SELECTABLE_KINDS = (Kind.TREE, Kind.FILE)


@dataclass(frozen=True, slots=True)
class Row:
    """One line in the GUI's list, ready to render."""

    item_id: str
    title: str
    category: str
    size_bytes: int
    file_count: int
    secret: bool
    #: False when the scan already decided against it, for a reason the user
    #: cannot overrule by ticking a box -- the folder is empty, or synced, or
    #: was excluded by a pattern they passed.
    selectable: bool
    #: Ticked when the panel opens.
    selected: bool
    #: Why it cannot be chosen, when it cannot.
    reason: str = ""


def rows_for(scan: ScanResult) -> list[Row]:
    """The list the GUI shows after a scan, in report order."""
    rows: list[Row] = []
    for item in scan.items:
        if item.kind not in SELECTABLE_KINDS:
            continue
        capturable = item.action is Action.CAPTURE
        rows.append(
            Row(
                item_id=item.id,
                title=item.title,
                category=item.category.value.replace("_", " "),
                size_bytes=item.size_bytes,
                file_count=item.file_count,
                secret=item.is_secret,
                selectable=capturable,
                selected=capturable,
                reason="" if capturable else _reason(item),
            )
        )
    return rows


def _reason(item: Item) -> str:
    if item.skip_reason is None:
        return "not being captured"
    return {
        SkipReason.SYNCED: "already synced to the cloud",
        SkipReason.EXCLUDED: "excluded by a pattern",
        SkipReason.EMPTY: "empty",
        SkipReason.NOT_PRESENT: "not on this machine",
        SkipReason.UNREADABLE: "could not be read",
        SkipReason.FILES_ONLY_MODE: "left out by files-only mode",
        SkipReason.REGENERABLE: "rebuilds itself",
        SkipReason.CLOUD_PLACEHOLDER: "online-only",
        SkipReason.REPARSE_POINT: "a junction or symlink",
        SkipReason.DESELECTED: "you unticked it",
    }.get(item.skip_reason, item.skip_reason.value.replace("_", " "))


def apply(scan: ScanResult, selected_ids: set[str]) -> ScanResult:
    """Mark everything selectable and unticked as skipped. Mutates ``scan``.

    Items the scan already decided against are left exactly as they are: their
    existing skip reason is the true one and overwriting it with "deselected"
    would lose why the folder was really left out.
    """
    for item in scan.items:
        if item.kind not in SELECTABLE_KINDS:
            continue
        if item.action is not Action.CAPTURE:
            continue
        if item.id in selected_ids:
            continue
        item.action = Action.SKIP
        item.skip_reason = SkipReason.DESELECTED
    return scan


def selected_totals(rows: list[Row], selected_ids: set[str]) -> tuple[int, int]:
    """``(bytes, files)`` for the ticked rows, for the running total."""
    total_bytes = 0
    total_files = 0
    for row in rows:
        if row.selectable and row.item_id in selected_ids:
            total_bytes += row.size_bytes
            total_files += row.file_count
    return total_bytes, total_files


# --- the other direction ---------------------------------------------------
#: Manifest actions that mean the item's files are actually in the bundle.
CAPTURED = "capture"


def rows_from_manifest(manifest: dict) -> list[Row]:
    """The list shown when putting a backup back.

    Built from the manifest inside the bundle rather than the plaintext sidecar,
    because the sidecar redacts every secret item down to a stub -- and browser
    profiles, SSH keys and the Notepad session are exactly the things someone
    might want to leave off a shared machine. Choosing between them means seeing
    them, which means the passphrase, which is why the backup is opened before
    this page is reached.

    Only items whose files are in the bundle appear. A record of the printers,
    the software inventory and the follow-up list are not things to tick: they
    are how the restore explains itself, and they always come.
    """
    rows: list[Row] = []
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        if item.get("kind") not in ("tree", "file"):
            continue
        if item.get("action") != CAPTURED:
            continue
        rows.append(
            Row(
                item_id=str(item.get("id", "")),
                title=str(item.get("title", item.get("id", ""))),
                category=str(item.get("category", "")).replace("_", " "),
                size_bytes=int(item.get("size_bytes") or 0),
                file_count=int(item.get("file_count") or 0),
                secret=item.get("sensitivity") == "secret",
                selectable=True,
                selected=True,
                reason="",
            )
        )
    return sorted(rows, key=lambda row: (row.category, row.title.lower()))


def restore_target_of(manifest: dict, item_id: str) -> str:
    """Where the manifest says an item goes, for the confirmation page."""
    for item in manifest.get("items", []):
        if isinstance(item, dict) and item.get("id") == item_id:
            spec = item.get("restore")
            if isinstance(spec, dict):
                return str(spec.get("target", ""))
    return ""


def followups_in(manifest: dict) -> list[tuple[str, str]]:
    """``(title, why)`` for each thing the restore cannot finish itself.

    The whole design stops at anything needing a person's identity -- account
    sign-ins, licence activation -- so this list is the actual output of a
    restore, not a footnote to it.
    """
    found: list[tuple[str, str]] = []
    for raw in manifest.get("followups", []):
        if isinstance(raw, dict):
            found.append((str(raw.get("title", "")), str(raw.get("why", ""))))
    return found
