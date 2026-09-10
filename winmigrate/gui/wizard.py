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


class Step(str, Enum):
    """The pages, in order."""

    WELCOME = "welcome"
    SCANNING = "scanning"
    SELECT = "select"
    DESTINATION = "destination"
    CONFIRM = "confirm"
    WORKING = "working"
    DONE = "done"


ORDER: tuple[Step, ...] = (
    Step.WELCOME,
    Step.SCANNING,
    Step.SELECT,
    Step.DESTINATION,
    Step.CONFIRM,
    Step.WORKING,
    Step.DONE,
)

#: Pages that run something and move on by themselves. The buttons do not
#: advance from these; the worker finishing does.
AUTOMATIC: frozenset[Step] = frozenset({Step.SCANNING, Step.WORKING})

#: Headings, as a person would read them.
TITLES: dict[Step, tuple[str, str]] = {
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
    Step.WELCOME: "Scan",
    Step.SELECT: "Next",
    Step.DESTINATION: "Next",
    Step.CONFIRM: "Start backup",
    Step.DONE: "Finish",
}


@dataclass
class WizardData:
    """Everything the pages collect, and the two facts they are gated on."""

    profile_root: str = ""
    scan_done: bool = False
    rows: list = field(default_factory=list)
    selected: set[str] = field(default_factory=set)
    output_path: str = ""
    passphrase: str = ""
    passphrase_confirm: str = ""
    capture_done: bool = False


@dataclass(frozen=True, slots=True)
class Check:
    """Whether the forward button works, and what to say when it does not."""

    ok: bool
    message: str = ""


def next_step(step: Step) -> Step | None:
    index = ORDER.index(step)
    return ORDER[index + 1] if index + 1 < len(ORDER) else None


def previous_step(step: Step) -> Step | None:
    """Where Back goes, or None when there is nowhere sensible to go.

    Back from Select returns to Welcome, which means scanning again -- that is
    what changing the profile or the options is for, and it is the only reason
    to go back from there. Back is not offered while something is running, nor
    once it has finished: the work is done and there is nothing to undo.
    """
    if step in AUTOMATIC or step in (Step.WELCOME, Step.DONE):
        return None
    index = ORDER.index(step)
    for candidate in reversed(ORDER[:index]):
        if candidate not in AUTOMATIC:
            return candidate
    return None


def can_go_back(step: Step) -> bool:
    return previous_step(step) is not None


def next_label(step: Step) -> str:
    return NEXT_LABEL.get(step, "Next")


def check(step: Step, data: WizardData) -> Check:
    """May the user leave ``step``? If not, why not -- in words, not a code."""
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


def progress_steps() -> tuple[Step, ...]:
    """The pages worth showing in a progress rail down the side.

    The two automatic pages are folded into the step they belong to -- nobody
    thinks of "scanning" and "choosing" as separate stages of a backup, and a
    rail that highlights a row for half a second is noise.
    """
    return (Step.WELCOME, Step.SELECT, Step.DESTINATION, Step.CONFIRM, Step.DONE)


def rail_index(step: Step) -> int:
    """Which rail entry to highlight for the page currently showing."""
    folded = {Step.SCANNING: Step.WELCOME, Step.WORKING: Step.CONFIRM}
    target = folded.get(step, step)
    rail = progress_steps()
    return rail.index(target) if target in rail else 0


#: Short labels for the rail.
RAIL_LABELS: dict[Step, str] = {
    Step.WELCOME: "Scan",
    Step.SELECT: "Choose",
    Step.DESTINATION: "Destination",
    Step.CONFIRM: "Confirm",
    Step.DONE: "Finish",
}
