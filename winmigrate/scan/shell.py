"""The taskbar, the desktop layout, and the Start menu.

This is how somebody opens things. Not the most technically interesting thing
in the migration and probably the most *used*: the row of icons along the
bottom is, for a great many people, the entire interface. A new machine with
the default five pins is a new machine no matter what else was restored onto
it.

Windows keeps all three as opaque binary, and there is no documented shape to
rebuild them from -- so the bytes are carried, uninterpreted, and put back:

* **Taskbar pins** are two halves that only work together: shortcut files under
  ``User Pinned\\TaskBar``, and a ``Taskband`` blob in the registry that says
  which of them are pinned and in what order. Carry one without the other and
  the taskbar does not change.
* **Desktop icon positions** live in a shell bag, keyed by screen resolution,
  and only apply when auto-arrange is off. Carried best-effort: on a machine
  with a different screen it does nothing, which is the right kind of nothing.
* **The Start menu layout** is a single file whose format changes between
  Windows releases. It is carried with the build number it came from, and put
  back only onto a machine of the same release -- a Start menu that has to
  rebuild itself is a nuisance, and one that is half-transplanted from another
  Windows version is worse.

Nothing here is secret. A pinned shortcut names a program, which is the same
thing the software inventory already records in full.
"""

from __future__ import annotations

import base64
import logging

from ..models import (
    Category,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, HKLM, Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)

TASKBAND_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband"
DESKTOP_BAG_KEY = r"Software\Microsoft\Windows\Shell\Bags\1\Desktop"
WINDOWS_VERSION_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion"

#: Relative to AppData\Roaming.
PINNED_FOLDER = ("Microsoft", "Internet Explorer", "Quick Launch", "User Pinned")

#: Relative to AppData\Local.
START_LAYOUT = (
    "Packages",
    "Microsoft.Windows.StartMenuExperienceHost_cw5n1h2txyewy",
    "LocalState",
    "start2.bin",
)

#: Taskband values worth carrying. The rest of that key is state Explorer
#: rewrites for itself.
TASKBAND_VALUES = ("Favorites", "FavoritesResolve", "FavoritesChanges", "FavoritesVersion")


def scan_shell(env: Environment):
    """Return the taskbar, desktop-layout and Start-menu items."""
    items: list[Item] = []
    for builder in (_pinned_shortcuts, _taskband, _desktop_positions, _start_layout):
        item = builder(env)
        if item is not None:
            items.append(item)
    return items, []


def _pinned_shortcuts(env: Environment) -> Item | None:
    """The shortcut files behind the taskbar pins."""
    path = env.appdata_roaming().joinpath(*PINNED_FOLDER)
    if not path.is_dir():
        return None
    relative = pathutil.relative_within(path, env.profile_root)
    if relative is None or relative == ".":
        return None
    return Item(
        id="shell:pinned_shortcuts",
        category=Category.SHELL,
        kind=Kind.TREE,
        title="Shortcuts pinned to the taskbar and Start",
        source_path=str(path),
        archive_path=f"data/{relative}",
        restore=RestoreSpec(
            target=str(path),
            strategy=RestoreStrategy.MERGE,
            notes=["Half of the taskbar; the other half is the Taskband record."],
        ),
    )


def _taskband(env: Environment) -> Item | None:
    """Which shortcuts are pinned, and in what order.

    Without this the shortcut files sit in a folder and the taskbar looks
    exactly as it did before the restore.
    """
    values = env.read_registry_key(HKCU, TASKBAND_KEY) or {}
    carried = {
        name: _encode(value)
        for name, value in sorted(values.items())
        if name in TASKBAND_VALUES and isinstance(value, (bytes, bytearray, int, str))
    }
    if not carried:
        return None
    return Item(
        id="shell:taskbar",
        category=Category.SHELL,
        kind=Kind.RECORD,
        title="Your taskbar",
        record={"values": carried},
        record_public=True,
        restore=RestoreSpec(
            target=f"HKCU\\{TASKBAND_KEY}",
            strategy=RestoreStrategy.REPLACE,
            notes=["Appears once Explorer restarts, which the restore does for you."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "The row of icons along the bottom of the screen, in the order you "
                "put them in.",
            )
        ],
    )


def _desktop_positions(env: Environment) -> Item | None:
    """Where the icons sit on the desktop, per screen size."""
    values = env.read_registry_key(HKCU, DESKTOP_BAG_KEY) or {}
    carried = {
        name: _encode(value)
        for name, value in sorted(values.items())
        if name.startswith("ItemPos") and isinstance(value, (bytes, bytearray))
    }
    if not carried:
        return None
    return Item(
        id="shell:desktop_layout",
        category=Category.SHELL,
        kind=Kind.RECORD,
        title="Where your desktop icons sit",
        record={"values": carried},
        record_public=True,
        restore=RestoreSpec(
            target=f"HKCU\\{DESKTOP_BAG_KEY}",
            strategy=RestoreStrategy.MERGE,
            notes=["Only takes effect on a screen the same size, with auto-arrange off."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "Windows records these per screen size, so a new machine with a "
                "different screen keeps its own arrangement.",
            )
        ],
    )


def _start_layout(env: Environment) -> Item | None:
    """The Start menu's pinned tiles, with the Windows build they came from."""
    path = env.appdata_local().joinpath(*START_LAYOUT)
    if not path.is_file():
        return None
    relative = pathutil.relative_within(path, env.profile_root)
    if relative is None or relative == ".":
        return None
    return Item(
        id="shell:start_menu",
        category=Category.SHELL,
        kind=Kind.FILE,
        title="Your Start menu",
        source_path=str(path),
        archive_path=f"data/{relative}",
        record={"windows_build": windows_build(env)},
        record_public=True,
        restore=RestoreSpec(
            target=str(path),
            strategy=RestoreStrategy.REPLACE,
            notes=["Put back only onto the same Windows release; see the report."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "The pinned tiles. Its format changes between Windows releases, so "
                "it is only put back onto a machine of the same one.",
            )
        ],
    )


def windows_build(env: Environment) -> str:
    """This machine's Windows build, as the string Windows itself reports."""
    value = env.read_registry_value(HKLM, WINDOWS_VERSION_KEY, "CurrentBuild")
    return str(value) if value is not None else ""


def _encode(value):
    """Registry values as JSON can hold them: bytes become base64, marked as such."""
    if isinstance(value, (bytes, bytearray)):
        return {"base64": base64.b64encode(bytes(value)).decode("ascii")}
    return value


def decode(value):
    """The inverse of :func:`_encode`. Returns None for anything unrecognised."""
    if isinstance(value, dict):
        raw = value.get("base64")
        if not isinstance(raw, str):
            return None
        try:
            return base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return None
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return value
    return None
