"""Windows 11 Notepad's unsaved tabs -- the notes nobody ever saved to a file.

Notepad has kept a running session since the 2022 tabs update: close it with
unsaved text in a tab and the text comes back next time. That buffer lives in
the app's package folder and is not a file the user has ever named, so nothing
else in a migration picks it up. It is also the single easiest thing to lose --
"I'll save it later" survives every reboot until the day the machine is
replaced.

Two things follow from where it lives.

**It is captured as SECRET**, so it exists only inside the encrypted payload
and appears in the plaintext sidecar as a redacted stub. Not because Notepad
is sensitive, but because of what people actually put in an unsaved tab: a
password while resetting it, a recovery code, a token pasted out of a terminal.
An unnamed scratch buffer is where secrets go to wait, and this tool does not
put that in a file anyone can read beside the bundle.

**Notepad must be closed on both machines.** It rewrites this folder on exit,
so capturing while it is open catches a stale buffer, and restoring while it is
open means Notepad overwrites what was just restored. The folder also only
exists once the app has run at least once, so a brand-new profile needs Notepad
opened before a restore has anywhere to go.

Nothing here reads the buffers. The tab files are a binary format Microsoft has
changed between versions; they are copied across intact and left for Notepad to
interpret, which is the only thing that can be relied on to still work.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from ..models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
    SkipReason,
)
from ..platform_win import Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)

SECRETS_ARCHIVE_PREFIX = "secrets"

#: The Store package. The suffix is Microsoft's publisher hash and is the same
#: on every machine, so it can be matched exactly rather than globbed.
NOTEPAD_PACKAGE = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"

#: Where the per-tab buffers sit inside the package's LocalState. Used to count
#: tabs for the report; the whole of LocalState is what actually travels.
TAB_STATE_DIR = "TabState"


def _archive_path(source: Path, env: Environment) -> str | None:
    """Profile-relative archive name for ``source``, or None if it is outside.

    Restore joins everything under ``secrets/`` onto the destination profile, so
    the name has to be the real profile-relative location. A path that is not
    below the profile cannot be expressed that way and is not captured rather
    than being put somewhere arbitrary -- a Notepad++ install running from a
    portable folder on another drive, say.
    """
    relative = pathutil.relative_within(source, env.profile_root)
    if relative is None or relative == ".":
        log.warning("%s is outside the profile root; not captured", source)
        return None
    return relative


def scan_notepad(env: Environment, files_only: bool = False):
    """Return the unsaved-editor items and follow-ups for this profile."""
    items: list[Item] = []
    followups: list[Followup] = []
    for found_items, found_followups in (
        _windows_notepad(env, files_only),
        _notepad_plus_plus(env, files_only),
    ):
        items.extend(found_items)
        followups.extend(found_followups)
    return items, followups


def _windows_notepad(env: Environment, files_only: bool):
    """Windows 11 Notepad: unsaved tabs in the Store app's package folder."""
    local_state = (
        env.appdata_local() / "Packages" / NOTEPAD_PACKAGE / "LocalState"
    )
    if not local_state.is_dir():
        return [], []

    tabs = _count_tabs(local_state / TAB_STATE_DIR)
    if tabs == 0:
        # The folder exists because Notepad has run, but there is no session in
        # it. Carrying an empty LocalState would only overwrite a good one.
        return [], []

    archive = _archive_path(local_state, env)
    if archive is None:
        return [], []

    item = Item(
        id="notepad:session",
        category=Category.NOTEPAD,
        kind=Kind.TREE,
        title=f"Notepad session ({tabs} tab(s))",
        source_path=str(local_state),
        archive_path=f"{SECRETS_ARCHIVE_PREFIX}/{archive}",
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target="%LOCALAPPDATA%\\Packages\\" + NOTEPAD_PACKAGE + "\\LocalState",
            strategy=RestoreStrategy.MERGE,
            notes=[
                "Close Notepad on the new machine before restoring, and open it "
                "once beforehand so Windows has created the folder.",
            ],
        ),
        record={"package": NOTEPAD_PACKAGE, "tabs": tabs},
    )
    item.notes.append(
        Note(
            Severity.INFO,
            f"{tabs} tab(s) Notepad is holding open, including text never saved to a file.",
            "Captured into the encrypted bundle only -- an unsaved tab is where "
            "a pasted password tends to sit.",
        )
    )
    item.notes.append(
        Note(
            Severity.WARNING,
            "Close Notepad before capturing.",
            "It rewrites this folder when it exits, so a session captured while "
            "Notepad is open may be missing your most recent edits.",
        )
    )

    if files_only:
        from ..models import Action  # noqa: PLC0415 -- avoid a top-level cycle

        item.action = Action.SKIP
        item.skip_reason = SkipReason.FILES_ONLY_MODE
        return [item], []

    return [item], [_followup(tabs)]


#: Notepad names each tab buffer after a GUID. Anything else in TabState is
#: bookkeeping of one kind or another, not a tab.
_GUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _count_tabs(tab_state: Path) -> int:
    """How many tab buffers are in TabState.

    Each tab is a GUID-named ``.bin``. Notepad also writes numbered side files
    (``<guid>.1.bin``) holding edits it has not folded into the main buffer yet;
    those belong to the same tab, so they are grouped rather than counted again.

    Two things are deliberately not counted. A ``.bin`` whose name is not a GUID
    is not a tab -- Notepad keeps other state in this folder -- and a zero-byte
    buffer is a file Notepad has created but not written, which is not a tab the
    user would recognise either. Both would inflate the number, and a count that
    says four when the user can see three is worse than no count at all: it is
    the only thing telling them whether their notes are in the bundle.
    """
    if not tab_state.is_dir():
        return 0
    try:
        entries = list(tab_state.iterdir())
    except OSError as exc:
        log.warning("could not read %s: %s", tab_state, exc)
        return 0

    guid_named: set[str] = set()
    any_named: set[str] = set()
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != ".bin":
            continue
        try:
            if entry.stat().st_size == 0:
                log.debug("ignoring empty tab buffer: %s", entry.name)
                continue
        except OSError:
            continue
        # "<guid>.bin" and "<guid>.1.bin" are one tab; key on the leading part.
        stem = entry.name.split(".", 1)[0].lower().strip("{}")
        any_named.add(stem)
        if _GUID_RE.match(stem):
            guid_named.add(stem)

    if guid_named:
        return len(guid_named)
    if any_named:
        # Nothing matched the naming this was written against. Rather than
        # report zero tabs for a folder that plainly has buffers in it -- the
        # one answer that would make the user think their notes were not
        # captured -- fall back to counting them and say so in the log.
        log.info(
            "TabState holds %d buffer(s) that are not GUID-named; counting them all",
            len(any_named),
        )
    return len(any_named)


def _followup(tabs: int) -> Followup:
    return Followup(
        id="notepad:session",
        title="Bring your unsaved Notepad tabs back",
        why=(
            f"{tabs} Notepad tab(s) travelled, including any text that was never "
            "saved to a file. Notepad only reads this folder when it starts, and "
            "rewrites it when it exits, so the order of these steps matters."
        ),
        steps=[
            "Open Notepad once on the new machine, then close it. This makes "
            "Windows create the app folder the session restores into.",
            "Run the restore with Notepad closed.",
            "Open Notepad: the tabs should be as you left them.",
            "Save anything you want to keep to a real file -- this session is "
            "still just a scratch buffer on the new machine too.",
        ],
        category=Category.NOTEPAD,
    )


# --- Notepad++ -------------------------------------------------------------
#: Notepad++ keeps its configuration -- and, with the session snapshot feature
#: on (the default for years), the contents of unsaved buffers -- here.
NOTEPADPP_RELATIVE = "Notepad++"

#: The snapshot folder inside it. One file per unsaved buffer.
NOTEPADPP_BACKUP_DIR = "backup"


def _notepad_plus_plus(env: Environment, files_only: bool):
    """Notepad++: the same idea, kept in a config folder rather than a package.

    "Enable session snapshot and periodic backup" is on by default, and it is
    what makes Notepad++ reopen an untitled tab full of text after a restart.
    Each such buffer is a file under ``backup/``; ``session.xml`` is the index
    that ties them to their tabs. Neither is any use without the other, and the
    rest of the folder is the configuration people have usually spent longer on
    than they remember -- themes, shortcuts, the plugin config -- so the whole
    folder travels as one item.

    The backup files are counted rather than session.xml being parsed. The count
    is the same either way, and it does not put an XML parser in front of a file
    this tool has no reason to interpret.
    """
    config_dir = env.appdata_roaming() / NOTEPADPP_RELATIVE
    if not config_dir.is_dir():
        return [], []

    unsaved = _count_backup_files(config_dir / NOTEPADPP_BACKUP_DIR)
    archive = _archive_path(config_dir, env)
    if archive is None:
        return [], []

    title = "Notepad++ configuration"
    if unsaved:
        title += f" and {unsaved} unsaved buffer(s)"

    item = Item(
        id="notepadpp:session",
        category=Category.NOTEPAD,
        kind=Kind.TREE,
        title=title,
        source_path=str(config_dir),
        archive_path=f"{SECRETS_ARCHIVE_PREFIX}/{archive}",
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target="%APPDATA%\\" + NOTEPADPP_RELATIVE,
            strategy=RestoreStrategy.MERGE,
            notes=["Close Notepad++ before restoring; it rewrites this folder on exit."],
        ),
        record={"unsaved_buffers": unsaved},
    )
    item.notes.append(
        Note(
            Severity.INFO,
            (
                f"{unsaved} unsaved buffer(s), plus your settings, themes and shortcuts."
                if unsaved
                else "Settings, themes and shortcuts. No unsaved buffers right now."
            ),
            "Captured into the encrypted bundle only -- an unsaved buffer is where "
            "a pasted password tends to sit.",
        )
    )
    if unsaved:
        item.notes.append(
            Note(
                Severity.WARNING,
                "Close Notepad++ before capturing.",
                "It writes the snapshot on exit, so a capture taken while it is "
                "open may be missing your most recent edits.",
            )
        )

    if files_only:
        from ..models import Action  # noqa: PLC0415 -- avoid a top-level cycle

        item.action = Action.SKIP
        item.skip_reason = SkipReason.FILES_ONLY_MODE
        return [item], []

    return [item], [_notepadpp_followup(unsaved)]


def _count_backup_files(backup_dir: Path) -> int:
    """Unsaved buffers held by the session snapshot. Empty files do not count."""
    if not backup_dir.is_dir():
        return 0
    count = 0
    try:
        entries = list(backup_dir.iterdir())
    except OSError as exc:
        log.warning("could not read %s: %s", backup_dir, exc)
        return 0
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_size > 0:
                count += 1
        except OSError:
            continue
    return count


def _notepadpp_followup(unsaved: int) -> Followup:
    why = (
        "Your Notepad++ configuration travelled"
        + (f", including {unsaved} unsaved buffer(s)." if unsaved else ".")
        + " Notepad++ reads this folder when it starts and rewrites it when it "
        "exits, so the order of these steps matters."
    )
    return Followup(
        id="notepadpp:session",
        title="Restore your Notepad++ session and settings",
        why=why,
        steps=[
            "Install Notepad++ on the new machine and close it.",
            "Run the restore with Notepad++ closed.",
            "Open Notepad++: your settings, and any unsaved tabs, should be there.",
            "If the unsaved tabs are missing, check Settings -> Preferences -> Backup "
            "that 'Enable session snapshot and periodic backup' is on, then restart it.",
            "Save anything you want to keep to a real file.",
        ],
        category=Category.NOTEPAD,
    )
