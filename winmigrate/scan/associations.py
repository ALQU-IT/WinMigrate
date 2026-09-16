"""Which program opens which kind of file.

"Why does my PDF open in Edge now?" is one of the most common things somebody
asks about a new computer, and one of the hardest for them to fix: the answer
is buried several screens into Settings, once per file type, and they have to
know the file type to look for.

WinMigrate reads the answers off the old machine and hands them over as a list.
It does not set them, and that is not an oversight. Windows deliberately
protects a default-app choice with a hash over the file type, the user's SID
and a timestamp; a program that writes the choice without the hash is ignored,
and one that forges the hash is doing exactly what the protection exists to
stop -- silently making itself your default browser. So the list is the
deliverable: what was set, in the user's own words, so they can set it again
in a few clicks instead of discovering it a file at a time.

Scheduled tasks are here for the same reason and are handled the same way: see
:mod:`winmigrate.scan.tasks`.
"""

from __future__ import annotations

import logging

from ..models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, Environment

log = logging.getLogger(__name__)

FILE_EXTS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"

#: The ones worth naming on a report somebody reads. A profile accumulates
#: hundreds of these, most of them for file types nobody opens by hand, and a
#: list of hundreds is a list nobody reads.
EVERYDAY_TYPES: tuple[str, ...] = (
    ".pdf", ".htm", ".html", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".txt", ".rtf", ".csv", ".odt", ".ods",
    ".jpg", ".jpeg", ".png", ".gif", ".heic", ".bmp", ".tif", ".tiff", ".webp",
    ".mp3", ".wav", ".flac", ".m4a", ".mp4", ".mkv", ".avi", ".mov", ".wmv",
    ".zip", ".7z", ".rar", ".iso", ".eml", ".msg", ".epub",
)

#: ProgIds that mean "whatever Windows ships", so saying them back adds nothing.
_UNINTERESTING = ("AppX", "Applications\\")


def scan_associations(env: Environment):
    """Return the file-association report and the follow-up that acts on it."""
    chosen = user_choices(env)
    if not chosen:
        return [], []

    item = Item(
        id="settings:file_associations",
        category=Category.FILE_ASSOCIATIONS,
        kind=Kind.REPORT,
        title=f"Which program opens which file ({len(chosen)})",
        record={"associations": chosen},
        record_public=True,
        restore=RestoreSpec(
            target="Settings > Apps > Default apps",
            strategy=RestoreStrategy.GUIDED,
            notes=["Set each in Settings; Windows does not let a program set them."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "Read from this machine and written down. Windows protects these "
                "against being set by a program, which is what stops software "
                "making itself your default browser behind your back.",
            )
        ],
    )
    return [item], [_followup(chosen)]


def user_choices(env: Environment) -> dict[str, str]:
    """The file types this user has actually chosen a program for.

    Only the everyday ones, and only where a choice was made: a profile holds
    hundreds of these and almost all of them are Windows talking to itself.
    """
    chosen: dict[str, str] = {}
    for suffix in EVERYDAY_TYPES:
        value = env.read_registry_value(
            HKCU, f"{FILE_EXTS_KEY}\\{suffix}\\UserChoice", "ProgId"
        )
        if not isinstance(value, str) or not value.strip():
            continue
        if any(value.startswith(prefix) for prefix in _UNINTERESTING):
            continue
        chosen[suffix] = value.strip()
    return chosen


def _followup(chosen: dict[str, str]) -> Followup:
    listed = [f"{suffix} opens with {program}" for suffix, program in sorted(chosen.items())]
    return Followup(
        id="settings:file_associations:guided",
        title=f"Set which program opens which file ({len(chosen)})",
        why=(
            "Windows will not let a program change these -- the protection that "
            "stops software making itself your default browser also stops a "
            "migration tool putting your choices back. They are written down here "
            "so it is a few clicks rather than a discovery, one file at a time."
        ),
        steps=[
            "Open Settings > Apps > Default apps on the new machine.",
            "Search for each file type below and pick the program named.",
            *listed,
        ],
        category=Category.FILE_ASSOCIATIONS,
    )
