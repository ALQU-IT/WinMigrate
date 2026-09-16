"""Where the GUI proposes to write the bundle.

The tool is most often run from the thing the backup is going onto: a USB
drive, an external disk, a network share mapped to a letter. So the default
save location is the drive WinMigrate itself was started from, which for the
common case is already the right answer and costs the user no thought.

It is only a default. The user can point it anywhere, and there is one case
where they should: when the program is running from the same drive it is
capturing, writing the bundle there needs as much free space again as the
profile. That is worth saying out loud rather than discovering at 90%.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path, PureWindowsPath


def program_directory() -> Path:
    """The folder WinMigrate is running from.

    Frozen by PyInstaller this is the folder holding the .exe, which is what
    "the drive I started it from" means to someone running it off a USB stick.
    From a source checkout it is the package's own directory, which is at least
    on the drive they installed it to.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent.parent


def program_drive() -> Path:
    """The root of the drive WinMigrate is running from.

    Falls back to the program's own folder where there is no drive letter to
    speak of -- a UNC path, or any non-Windows host the GUI is being exercised
    on -- because a bundle has to be proposed somewhere.
    """
    directory = program_directory()
    drive = PureWindowsPath(str(directory)).drive
    if drive and drive.endswith(":"):
        return Path(drive + "\\")
    return directory


#: Windows drive types, from GetDriveTypeW.
DRIVE_REMOVABLE = 2
DRIVE_FIXED = 3

#: A backup wants room for the profile and then some. A stick with less free
#: space than this is not what somebody plugged in to take their files away.
MINIMUM_STICK_BYTES = 4 * 1024 * 1024 * 1024


def removable_drives(lister=None) -> list[Path]:
    """Drive roots Windows calls removable, in letter order.

    Asked of Windows rather than guessed at from the drive letter: a USB disk
    can be any letter, and a fixed disk can be E:. ``lister`` exists so this is
    exercised without plugging anything in.
    """
    if lister is not None:
        return [Path(entry) for entry in lister()]
    if sys.platform != "win32":  # pragma: no cover -- exercised through lister
        return []
    try:  # pragma: no cover -- Windows-only
        import ctypes
        import string

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        found = []
        mask = kernel32.GetLogicalDrives()
        for index, letter in enumerate(string.ascii_uppercase):
            if not mask & (1 << index):
                continue
            root = f"{letter}:\\"
            if kernel32.GetDriveTypeW(root) == DRIVE_REMOVABLE:
                found.append(Path(root))
        return found
    except Exception:  # noqa: BLE001 -- a default must always appear
        return []


def proposed_drive(needed_bytes: int = 0, lister=None) -> Path:
    """Where a backup should go, before the user has said anything.

    Running from a stick already? Then that is the answer, and always was.
    Running from the system drive is the case this adds: somebody downloaded
    the program to their Downloads folder and plugged a stick in, which is what
    most people do, and proposing they write the backup onto the disk they are
    backing up is proposing the one location that cannot work as a backup.

    A removable drive is only offered when it has room for what is planned --
    an empty 2 GB stick is not somewhere to put forty gigabytes of photographs,
    and finding that out at ninety per cent is the whole failure this avoids.
    """
    program = program_drive()
    if not _is_system_drive(program):
        return program
    best: tuple[int, Path] | None = None
    for drive in removable_drives(lister):
        free = _free_space(drive)
        if free < max(needed_bytes, MINIMUM_STICK_BYTES):
            continue
        if best is None or free > best[0]:
            best = (free, drive)
    return best[1] if best else program


def _is_system_drive(path: Path) -> bool:
    """Is this the drive Windows itself is on?"""
    system = os.environ.get("SystemDrive") or "C:"
    return PureWindowsPath(str(path)).drive.upper() == system.upper()


def _free_space(drive: Path) -> int:
    try:
        import shutil

        return shutil.disk_usage(drive).free
    except OSError:
        return 0


def default_bundle_path(
    host: str = "",
    user: str = "",
    now: datetime | None = None,
    needed_bytes: int = 0,
    lister=None,
) -> Path:
    """A full proposed path: a drive that can hold it, and a name that sorts.

    The name carries host and user because bundles from several machines end up
    in one folder more often than not, and a timestamp because the second
    capture must not silently land on the first.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    parts = [part for part in (_slug(host), _slug(user)) if part]
    parts.append(stamp)
    return proposed_drive(needed_bytes, lister) / ("-".join(parts) + ".dat")


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in text.strip()]
    return "".join(keep).strip("-").lower()


def same_drive(one: os.PathLike[str] | str, other: os.PathLike[str] | str) -> bool:
    """True when two paths are on the same Windows drive.

    Used to warn that the bundle is being written to the disk it is reading,
    which needs the profile's size again in free space.
    """
    first = PureWindowsPath(str(one)).drive.upper()
    second = PureWindowsPath(str(other)).drive.upper()
    return bool(first) and first == second


#: A bundle is always written with this extension, and its sidecar sits beside
#: it with the same stem.
BUNDLE_SUFFIX = ".dat"


def bundles_beside_program(directory=None) -> list[Path]:
    """Backups sitting in the folder WinMigrate is running from, newest first.

    The second half of a migration is done on the new machine, usually standing
    over it with a USB drive plugged in, and the backup is on that drive next to
    the program. Making someone choose a mode and then browse for a file they
    are already standing on is two steps that exist only because they were easy
    to write.

    A file is only offered if its plaintext sidecar is beside it. That is not
    fussiness: without the sidecar there is no transfer check and no way to say
    anything about the bundle before the passphrase is typed, and something that
    merely ends in .dat is as likely to be someone else's data file.
    """
    directory = Path(directory) if directory is not None else program_directory()
    try:
        candidates = [
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.lower() == BUNDLE_SUFFIX
        ]
    except OSError:
        return []
    found = [path for path in candidates if path.with_suffix(".manifest.json").is_file()]
    found.sort(key=_sort_key, reverse=True)
    return found


def _sort_key(path: Path) -> float:
    """Newest first, by the file's own timestamp rather than its name.

    The default name carries a timestamp, but a bundle someone renamed should
    still sort sensibly rather than falling to the bottom.
    """
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


def describe_bundle_file(path: Path) -> str:
    """One line for a chooser: the name, its size, and when it was made."""
    from ..util import humanize  # noqa: PLC0415

    try:
        stat = path.stat()
    except OSError:
        return path.name
    made = datetime.fromtimestamp(stat.st_mtime).strftime("%d %b %Y, %H:%M")
    return f"{path.name} — {humanize.bytes_(stat.st_size)}, {made}"
