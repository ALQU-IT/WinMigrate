"""The wizard's shape: which page follows which, and when you may leave one.

Kept apart from the widgets on purpose. A wizard is mostly rules -- you cannot
reach the destination page before the scan has produced something to choose
from, you cannot start a capture with an empty passphrase, Back means different
things on different pages -- and rules buried in button callbacks are rules
nobody can test. tkinter needs a display; this does not.

The page order is the one every Windows installer has trained people to expect:
say what is about to happen, do the slow part with a progress bar, let them
choose, ask where it goes, show a summary they can still back out of, then work,
then report. Nothing here is novel and that is the point -- someone who has
installed software before already knows how to drive it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Mode(str, Enum):
    """Which of the two jobs the user came to do."""

    BACKUP = "backup"
    RESTORE = "restore"


class Step(str, Enum):
    """The pages. Which of them you see depends on the mode."""

    CHOOSE = "choose"

    # Backing up.
    WELCOME = "welcome"
    SCANNING = "scanning"
    SELECT = "select"
    DESTINATION = "destination"
    CONFIRM = "confirm"
    WORKING = "working"
    DONE = "done"

    # Putting one back.
    SOURCE = "source"
    OPENING = "opening"
    RESTORE_SELECT = "restore_select"
    RESTORE_CONFIRM = "restore_confirm"
    RESTORING = "restoring"
    RESTORE_DONE = "restore_done"


BACKUP_ORDER: tuple[Step, ...] = (
    Step.CHOOSE,
    Step.WELCOME,
    Step.SCANNING,
    Step.SELECT,
    Step.DESTINATION,
    Step.CONFIRM,
    Step.WORKING,
    Step.DONE,
)

RESTORE_ORDER: tuple[Step, ...] = (
    Step.CHOOSE,
    Step.SOURCE,
    Step.OPENING,
    Step.RESTORE_SELECT,
    Step.RESTORE_CONFIRM,
    Step.RESTORING,
    Step.RESTORE_DONE,
)

#: Kept for the backup path's own sake; ``order()`` is what the window asks.
ORDER = BACKUP_ORDER


def as_mode(mode: Mode | str) -> Mode:
    """Coerce to *this* module's Mode, by value rather than by identity.

    ``Mode`` is a str enum, so a member compares equal to its own value. That
    matters because identity does not survive the module being imported twice --
    which happens more readily than it should: a frozen build, a reload, a test
    that clears sys.modules. An ``is`` check that quietly fails would not raise;
    it would return the backup rail to someone doing a restore.
    """
    try:
        return Mode(mode)
    except ValueError:
        return Mode.BACKUP


def order(mode: Mode | str) -> tuple[Step, ...]:
    return RESTORE_ORDER if as_mode(mode) is Mode.RESTORE else BACKUP_ORDER


#: Pages that run something and move on by themselves. The buttons do not
#: advance from these; the worker finishing does.
AUTOMATIC: frozenset[Step] = frozenset(
    {Step.SCANNING, Step.WORKING, Step.OPENING, Step.RESTORING}
)

#: Pages after which there is nothing to go back to.
TERMINAL: frozenset[Step] = frozenset({Step.DONE, Step.RESTORE_DONE})

#: Headings, as a person would read them.
TITLES: dict[Step, tuple[str, str]] = {
    Step.CHOOSE: (
        "What would you like to do?",
        "Back up this machine's profile, or put a backup onto this one.",
    ),
    Step.SOURCE: (
        "Which backup?",
        "Choose the .dat file. Its passphrase is needed to look inside — nothing "
        "is written until you have seen what is in it.",
    ),
    Step.OPENING: (
        "Opening the backup",
        "Checking it arrived intact and reading what it holds. Nothing is being "
        "written yet.",
    ),
    Step.RESTORE_SELECT: (
        "Choose what to put back",
        "Everything is selected. Untick anything this machine should not get.",
    ),
    Step.RESTORE_CONFIRM: (
        "Ready to restore",
        "Nothing has been written yet. Check where this is going, then begin.",
    ),
    Step.RESTORING: (
        "Putting your files back",
        "Files already here and identical are skipped, so this can be re-run "
        "safely if it is interrupted.",
    ),
    Step.RESTORE_DONE: (
        "Restored",
        "What is left needs you rather than the tool.",
    ),
    Step.WELCOME: (
        "Back up this Windows profile",
        "Everything you keep — files, browser profiles, settings and the list of "
        "software you have installed — goes into one encrypted file you control.",
    ),
    Step.SCANNING: (
        "Looking through your profile",
        "Nothing is being copied yet. This only counts what is there so you can "
        "choose what to keep.",
    ),
    Step.SELECT: (
        "Choose what to keep",
        "Everything is selected. Untick anything you would rather leave behind.",
    ),
    Step.DESTINATION: (
        "Where should the backup go?",
        "One file, encrypted with a passphrase only you hold.",
    ),
    Step.CONFIRM: (
        "Ready to start",
        "Nothing has been written yet. Check this over, then begin.",
    ),
    Step.WORKING: (
        "Creating the backup",
        "You can keep using the computer, though closing your browser and Outlook "
        "gives a cleaner copy.",
    ),
    Step.DONE: ("Finished", "Your backup is ready."),
}

#: What the forward button says. A wizard whose button always reads "Next" makes
#: the user find out what it did by pressing it.
NEXT_LABEL: dict[Step, str] = {
    Step.CHOOSE: "Continue",
    Step.SOURCE: "Open",
    Step.RESTORE_SELECT: "Next",
    Step.RESTORE_CONFIRM: "Start restore",
    Step.RESTORE_DONE: "Finish",
    Step.WELCOME: "Scan",
    Step.SELECT: "Next",
    Step.DESTINATION: "Next",
    Step.CONFIRM: "Start backup",
    Step.DONE: "Finish",
}


@dataclass
class WizardData:
    """Everything the pages collect, and the facts they are gated on."""

    mode: Mode = Mode.BACKUP

    # Backing up.
    profile_root: str = ""
    scan_done: bool = False
    rows: list = field(default_factory=list)
    selected: set[str] = field(default_factory=set)
    output_path: str = ""
    passphrase: str = ""
    passphrase_confirm: str = ""
    capture_done: bool = False

    # Putting one back.
    bundle_path: str = ""
    bundle_passphrase: str = ""
    bundle_open: bool = False
    restore_rows: list = field(default_factory=list)
    restore_selected: set[str] = field(default_factory=set)
    destination: str = ""
    dry_run: bool = False
    overwrite: bool = False
    restore_done: bool = False


@dataclass(frozen=True, slots=True)
class Check:
    """Whether the forward button works, and what to say when it does not."""

    ok: bool
    message: str = ""


def next_step(step: Step, mode: Mode | str = Mode.BACKUP) -> Step | None:
    sequence = order(mode)
    if step not in sequence:
        return None
    index = sequence.index(step)
    return sequence[index + 1] if index + 1 < len(sequence) else None


def previous_step(step: Step, mode: Mode | str = Mode.BACKUP) -> Step | None:
    """Where Back goes, or None when there is nowhere sensible to go.

    Back from Select returns to Welcome, which means scanning again -- that is
    what changing the profile or the options is for, and it is the only reason
    to go back from there. The pages that run something are skipped over for the
    same reason: landing on a progress bar that immediately runs again is not
    going back.

    Back is not offered while something is running, nor once it has finished --
    a restore that has written files is not something a button can undo.
    """
    if step in AUTOMATIC or step in TERMINAL or step is Step.CHOOSE:
        return None
    sequence = order(mode)
    if step not in sequence:
        return None
    index = sequence.index(step)
    for candidate in reversed(sequence[:index]):
        if candidate not in AUTOMATIC:
            return candidate
    return None


def can_go_back(step: Step, mode: Mode | str = Mode.BACKUP) -> bool:
    return previous_step(step, mode) is not None


def next_label(step: Step) -> str:
    return NEXT_LABEL.get(step, "Next")


def check(step: Step, data: WizardData) -> Check:
    """May the user leave ``step``? If not, why not -- in words, not a code."""
    if step is Step.CHOOSE:
        return Check(True)

    if step is Step.SOURCE:
        if not data.bundle_path.strip():
            return Check(False, "Choose the backup file to restore from.")
        bundle = Path(data.bundle_path)
        if not bundle.is_file():
            return Check(False, f"{bundle.name} is not there.")
        if not data.bundle_passphrase:
            return Check(False, "The backup's passphrase is needed to open it.")
        return Check(True)

    if step is Step.RESTORE_SELECT:
        if not data.restore_selected:
            return Check(False, "Nothing is selected, so there is nothing to put back.")
        return Check(True)

    if step is Step.RESTORE_CONFIRM:
        if not data.destination.strip():
            return Check(False, "Choose where the files should go.")
        # The parent has to exist; the destination itself is created.
        parent = Path(data.destination).parent
        if not Path(data.destination).is_dir() and not parent.is_dir():
            return Check(False, f"{parent} does not exist.")
        return Check(True)

    if step is Step.RESTORE_DONE:
        return Check(True)

    if step is Step.WELCOME:
        if not data.profile_root.strip():
            return Check(False, "Choose the profile folder to back up.")
        return Check(True)

    if step is Step.SELECT:
        if not data.selected:
            return Check(False, "Nothing is selected, so there is nothing to back up.")
        return Check(True)

    if step is Step.DESTINATION:
        if not data.output_path.strip():
            return Check(False, "Choose where to save the backup.")
        parent = Path(data.output_path).parent
        if not parent.is_dir():
            return Check(False, f"{parent} does not exist.")
        if not data.passphrase:
            return Check(False, "A passphrase is required — the backup is always encrypted.")
        if data.passphrase != data.passphrase_confirm:
            return Check(False, "The two passphrases do not match.")
        return Check(True)

    if step is Step.CONFIRM:
        return Check(True)

    if step is Step.DONE:
        return Check(True)

    # SCANNING and WORKING advance when their worker finishes, not on a click.
    return Check(False, "")


def title(step: Step) -> tuple[str, str]:
    return TITLES.get(step, (step.value.title(), ""))


def progress_steps(mode: Mode | str = Mode.BACKUP) -> tuple[Step, ...]:
    """The pages worth showing in a progress rail down the side.

    The pages that run something are folded into the step they belong to --
    nobody thinks of "scanning" and "choosing" as separate stages of a backup,
    and a rail entry that highlights for half a second is noise. Choosing the
    mode is not a stage either: by the time there is a rail to look at, that
    decision is made.
    """
    if as_mode(mode) is Mode.RESTORE:
        return (
            Step.SOURCE,
            Step.RESTORE_SELECT,
            Step.RESTORE_CONFIRM,
            Step.RESTORE_DONE,
        )
    return (Step.WELCOME, Step.SELECT, Step.DESTINATION, Step.CONFIRM, Step.DONE)


#: Where a page that runs something is shown on the rail.
FOLDED: dict[Step, Step] = {
    Step.SCANNING: Step.WELCOME,
    Step.WORKING: Step.CONFIRM,
    Step.OPENING: Step.SOURCE,
    Step.RESTORING: Step.RESTORE_CONFIRM,
}


def rail_index(step: Step, mode: Mode | str = Mode.BACKUP) -> int:
    """Which rail entry to highlight for the page currently showing."""
    target = FOLDED.get(step, step)
    rail = progress_steps(mode)
    return rail.index(target) if target in rail else 0


#: Short labels for the rail.
RAIL_LABELS: dict[Step, str] = {
    Step.WELCOME: "Scan",
    Step.SELECT: "Choose",
    Step.DESTINATION: "Destination",
    Step.CONFIRM: "Confirm",
    Step.DONE: "Finish",
    Step.SOURCE: "Backup",
    Step.RESTORE_SELECT: "Choose",
    Step.RESTORE_CONFIRM: "Confirm",
    Step.RESTORE_DONE: "Finish",
}
