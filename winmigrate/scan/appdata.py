"""The program data that lives under AppData, named one program at a time.

``AppData`` is excluded from the file scan, and that default is right: it is
where Windows and every program on the machine keep their caches, their
half-downloaded updates, their machine-bound tokens and their crash dumps.
Copying it wholesale would double the size of a backup with things that are
worthless on the far side, and would carry credential material nobody asked to
move.

But the same folder is also where a large class of desktop programs keep
everything that makes them *yours*. Thunderbird keeps entire mailboxes there.
Word keeps the templates you made and the dictionary of names you taught it.
Windows keeps the folders pinned to Quick Access and the VPN connections you
set up. None of that is a cache, and none of it comes back from an installer.
So the answer is not "carry AppData" and not "carry none of it" -- it is a list
of named locations, each with a reason it is on the list.

**Adding to this table is the intended way to make a migration more complete.**
An entry is a path, a title, a sentence saying why somebody would miss it, and
-- where the program stores credentials -- a flag that puts it in the encrypted
payload only. Nothing here parses what it copies.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from ..models import (
    Category,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
)
from ..platform_win import Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AppLocation:
    """One program's data, under Roaming or Local."""

    slot: str
    parts: tuple[str, ...]
    title: str
    why: str
    #: True for Local rather than Roaming. Roaming is where settings belong and
    #: is the default; Local is where a few things genuinely live, Store apps
    #: most of all.
    local: bool = False
    #: Credential material: encrypted payload only, redacted from the sidecar,
    #: dropped entirely by --files-only.
    secret: bool = False
    #: Shown on the restore side when the program has to exist first.
    needs_program: str = ""


#: What travels. Ordered by how much somebody would miss it.
LOCATIONS: tuple[AppLocation, ...] = (
    AppLocation(
        slot="thunderbird",
        parts=("Thunderbird",),
        title="Thunderbird mail",
        why=(
            "Every message, folder, address book and account setting. Thunderbird "
            "keeps the whole mailbox here, so this is the mail itself and not a "
            "pointer to it."
        ),
        needs_program="Thunderbird",
    ),
    AppLocation(
        slot="office_templates",
        parts=("Microsoft", "Templates"),
        title="Word and Excel templates",
        why="The letterheads and documents you built to start from.",
    ),
    AppLocation(
        slot="office_dictionary",
        parts=("Microsoft", "UProof"),
        title="Your custom dictionary",
        why=(
            "Every name and word you ever told Word to stop underlining. Nobody "
            "rebuilds this; they just live with the red lines."
        ),
    ),
    AppLocation(
        slot="office_spelling",
        parts=("Microsoft", "Spelling"),
        title="Windows spelling dictionary",
        why="The same, for the spell checker Windows itself provides.",
    ),
    AppLocation(
        slot="excel_startup",
        parts=("Microsoft", "Excel", "XLSTART"),
        title="Excel startup workbooks",
        why="Personal macro workbooks -- the macros somebody wrote for themselves.",
    ),
    AppLocation(
        slot="word_startup",
        parts=("Microsoft", "Word", "STARTUP"),
        title="Word startup add-ins",
        why="Templates and add-ins Word loads every time it opens.",
    ),
    AppLocation(
        slot="quick_access",
        parts=("Microsoft", "Windows", "Recent", "AutomaticDestinations"),
        title="Quick Access and recent files",
        why=(
            "The folders pinned down the side of every Explorer window, and the "
            "recent-file lists behind each program on the taskbar."
        ),
    ),
    AppLocation(
        slot="jump_lists",
        parts=("Microsoft", "Windows", "Recent", "CustomDestinations"),
        title="Program jump lists",
        why="The shortcuts programs put on their own taskbar menus.",
    ),
    AppLocation(
        slot="vpn",
        parts=("Microsoft", "Network", "Connections", "Pbk"),
        title="VPN connections",
        why=(
            "The VPN entries somebody set up by hand. The passwords are not in "
            "here -- Windows keeps those separately -- so each will ask once."
        ),
    ),
    AppLocation(
        slot="sticky_notes",
        parts=("Packages", "Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe", "LocalState"),
        title="Sticky Notes",
        why="The notes on the desktop, which are usually the only copy.",
        local=True,
        needs_program="Sticky Notes",
    ),
    AppLocation(
        slot="vlc",
        parts=("vlc",),
        title="VLC settings",
        why="Playback preferences and the list of what was played.",
        needs_program="VLC",
    ),
    AppLocation(
        slot="obs",
        parts=("obs-studio",),
        title="OBS scenes and profiles",
        why="Scene collections, which are hours of work and live nowhere else.",
        needs_program="OBS Studio",
    ),
    AppLocation(
        slot="qbittorrent",
        parts=("qBittorrent",),
        title="qBittorrent settings",
        why="Preferences and the list of what is still transferring.",
        needs_program="qBittorrent",
    ),
    AppLocation(
        slot="keepass",
        parts=("KeePass",),
        title="KeePass settings",
        why=(
            "Its configuration and recent-database list. The database itself is "
            "one of your own files and travels with them."
        ),
        needs_program="KeePass",
    ),
    AppLocation(
        slot="filezilla",
        parts=("FileZilla",),
        title="FileZilla sites",
        why=(
            "The Site Manager, which stores server passwords in the clear. It "
            "travels in the encrypted payload only."
        ),
        secret=True,
        needs_program="FileZilla",
    ),
)


def scan_app_data(env: Environment, files_only: bool = False):
    """Return an item per named AppData location that exists in this profile."""
    items: list[Item] = []
    for location in LOCATIONS:
        item = _item_for(env, location, files_only)
        if item is not None:
            items.append(item)
    return items, []


def path_for(env: Environment, location: AppLocation) -> Path:
    root = env.appdata_local() if location.local else env.appdata_roaming()
    return root.joinpath(*location.parts)


def _item_for(env: Environment, location: AppLocation, files_only: bool) -> Item | None:
    path = path_for(env, location)
    if not path.is_dir():
        return None
    try:
        if not any(path.iterdir()):
            return None
    except OSError:
        return None
    relative = pathutil.relative_within(path, env.profile_root)
    if relative is None or relative == ".":
        log.warning("%s is outside the profile root; not captured", path)
        return None

    notes = [Note(Severity.INFO, location.why)]
    if location.needs_program:
        notes.append(
            Note(
                Severity.INFO,
                f"Install {location.needs_program} on the new machine before opening "
                "it, so it finds this rather than writing over it.",
            )
        )
    # Both prefixes carry a *profile-relative* path, because that is what the
    # restore rebuilds against the new profile: "secrets/AppData/Roaming/X",
    # not "secrets/X", which would land it in the profile root.
    archive = f"secrets/{relative}" if location.secret else f"data/{relative}"
    item = Item(
        id=f"appdata:{location.slot}",
        category=Category.APP_DATA,
        kind=Kind.TREE,
        title=location.title,
        source_path=str(path),
        archive_path=archive,
        sensitivity=Sensitivity.SECRET if location.secret else Sensitivity.NORMAL,
        restore=RestoreSpec(
            target=str(path),
            strategy=RestoreStrategy.MERGE,
            notes=[location.why],
        ),
        notes=notes,
    )
    if location.secret and files_only:
        from ..models import Action, SkipReason  # noqa: PLC0415

        item.action = Action.SKIP
        item.skip_reason = SkipReason.FILES_ONLY_MODE
    return item
