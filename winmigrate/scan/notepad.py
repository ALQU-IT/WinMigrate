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


def scan_notepad(env: Environment, files_only: bool = False):
    """Return the Notepad session item and its follow-up, if there is one."""
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

    archive = pathutil.relative_within(local_state, env.profile_root)
    if archive is None:
        log.warning("%s is outside the profile root; not captured", local_state)
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


def _count_tabs(tab_state: Path) -> int:
    """How many tab buffers are in TabState.

    Each open tab is a ``.bin`` file; Notepad also writes numbered side files
    (``<guid>.1.bin``) for pending edits, which are part of the same tab rather
    than another one, so they are not counted twice.
    """
    if not tab_state.is_dir():
        return 0
    seen: set[str] = set()
    try:
        entries = list(tab_state.iterdir())
    except OSError as exc:
        log.warning("could not read %s: %s", tab_state, exc)
        return 0
    for entry in entries:
        if not entry.is_file() or entry.suffix.lower() != ".bin":
            continue
        # "<guid>.bin" and "<guid>.1.bin" are one tab; key on the leading part.
        seen.add(entry.name.split(".", 1)[0].lower())
    return len(seen)


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
