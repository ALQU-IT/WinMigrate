"""What starts when you log in.

Two places hold it, and neither travels today. The Startup folder lives under
``AppData\\Roaming\\Microsoft\\Windows\\Start Menu``, which the file scan
excludes wholesale; the ``Run`` key lives in the registry, which the file scan
never sees at all. So a machine restored from a WinMigrate bundle logs in to a
bare desktop, and the programs somebody expects to already be running -- the
cloud client, the backup agent, the utility that makes their mouse buttons do
what they want -- are not.

Most of them come back on their own once their program is reinstalled, because
installers write these entries themselves. The ones that do not are exactly the
ones nobody remembers: a shortcut dragged into Startup years ago, a tool that
was set up once and has run quietly ever since.

**A Run entry is a command Windows will execute at every login, so this is the
most carefully handled thing in the module.** Every entry is named on screen as
it is applied rather than written quietly, and an entry whose program is not on
the new machine is skipped rather than restored dead -- which is both safer and
more honest, since a dead entry is a login-time error box in perpetuity.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..models import (
    Category,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_ONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"

#: Relative to AppData\Roaming.
STARTUP_FOLDER = ("Microsoft", "Windows", "Start Menu", "Programs", "Startup")


def scan_startup(env: Environment):
    """Return the startup items: the folder, and the Run entries."""
    items: list[Item] = []
    folder = _startup_folder(env)
    if folder is not None:
        items.append(folder)
    entries = _run_entries(env)
    if entries is not None:
        items.append(entries)
    return items, []


def startup_path(env: Environment) -> Path:
    return env.appdata_roaming().joinpath(*STARTUP_FOLDER)


def _startup_folder(env: Environment) -> Item | None:
    """The Startup folder, as a folder.

    Its contents are shortcuts naming programs by absolute path. Most of those
    paths are the same on any Windows machine -- Program Files is Program Files
    -- and Windows repairs the rest by itself when it can. A shortcut that
    cannot be repaired does nothing at login, which is the failure mode to
    want: nothing, rather than an error.
    """
    path = startup_path(env)
    if not path.is_dir():
        return None
    relative = pathutil.relative_within(path, env.profile_root)
    if relative is None or relative == ".":
        log.warning("%s is outside the profile root; not captured", path)
        return None
    if not any(path.iterdir()):
        return None
    return Item(
        id="settings:startup_folder",
        category=Category.STARTUP,
        kind=Kind.TREE,
        title="Programs in your Startup folder",
        source_path=str(path),
        archive_path=f"data/{relative}",
        restore=RestoreSpec(
            target="%APPDATA%\\Microsoft\\Windows\\Start Menu\\Programs\\Startup",
            strategy=RestoreStrategy.MERGE,
            notes=["These start at your next sign-in."],
        ),
    )


def _run_entries(env: Environment) -> Item | None:
    """The Run key: per-user programs Windows starts at login.

    RunOnce is deliberately not carried. It is a queue of things waiting to
    happen once on *that* machine -- half-finished installers, pending
    reboots -- and replaying somebody else's pending reboot on a new computer
    is not migration.
    """
    values = env.read_registry_key(HKCU, RUN_KEY) or {}
    entries = {
        name: value
        for name, value in sorted(values.items())
        if isinstance(name, str) and isinstance(value, str) and value.strip()
    }
    if not entries:
        return None
    return Item(
        id="settings:startup_run",
        category=Category.STARTUP,
        kind=Kind.RECORD,
        title=f"Programs that start when you log in ({len(entries)})",
        record={"entries": entries},
        record_public=True,
        restore=RestoreSpec(
            target=f"HKCU\\{RUN_KEY}",
            strategy=RestoreStrategy.MERGE,
            notes=["Re-added for programs that are on the new machine."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "Each of these is a command Windows runs at login. They are named "
                "one by one in the restore report, and any whose program is not on "
                "the new machine is left out rather than restored broken.",
            )
        ],
    )


def executable_of(command: str) -> str:
    """The program a Run command line actually starts.

    Windows accepts both ``"C:\\Program Files\\App\\app.exe" --quiet`` and the
    same path unquoted, which is ambiguous by construction: the space could
    separate the program from its arguments or be part of the folder name. A
    quoted path is taken as given; an unquoted one is grown a word at a time
    until it names something that exists, which is what Windows itself does.
    """
    text = (command or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        closing = text.find('"', 1)
        return text[1:closing] if closing > 1 else text[1:]
    words = text.split(" ")
    for count in range(1, len(words) + 1):
        candidate = " ".join(words[:count])
        if Path(pathutil.to_posix(candidate)).exists():
            return candidate
    return words[0]
