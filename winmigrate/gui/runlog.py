"""The window's log file: where it goes, and what it says first.

The command line takes ``--log-file``; the window has nobody to take it from,
and for a long time wrote no log at all. That is the one place a log is worth
most. A capture of a real profile spends its first minutes creating a shadow
copy, during which the progress bar truthfully reads zero -- and someone
watching a bar that has not moved has no way to tell a slow start from a hang.
A log that says what was started, from where, with which rights, and what it is
waiting for answers that without anyone having to ask.

Where it goes matters as much as what it says. WinMigrate is usually run from a
USB drive, so the log goes beside the program: the same drive the user is
holding, next to the backup itself. When that drive is read-only -- or the
program was started from somewhere that is -- it falls back to the user's local
application data, and then to the temporary folder, rather than giving up on
having a log.

Nothing secret is written here. The passphrase never leaves the widget it was
typed into, and :class:`~winmigrate.logging_setup.SecretRedactingFilter` drops
any record that is marked as carrying secrets.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

from .. import logging_setup
from . import defaults


def candidates() -> list[Path]:
    """Directories to try for the log, best first."""
    found: list[Path] = []
    try:
        found.append(defaults.program_directory())
    except Exception:  # noqa: BLE001 -- a frozen build with an odd layout
        pass
    local = os.environ.get("LOCALAPPDATA")
    if local:
        found.append(Path(local) / "WinMigrate")
    found.append(Path(tempfile.gettempdir()))
    return found


#: Exactly the shape :func:`winmigrate.logging_setup.writable_log_path` writes.
#: Nothing else is ever considered for deletion -- a user's own file that merely
#: begins with the same word is not this program's to tidy away.
LOG_NAME = re.compile(r"^winmigrate-\d{8}-\d{6}\.log$")

#: Two per backup is normal (one before the UAC prompt, one after), so a keep
#: count in the low tens is a few months of use rather than a few days.
KEEP = 20


def prune(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete this program's own older logs, newest ``keep`` kept.

    A run leaves a log every time, including the launch that only asked for
    administrator rights and closed. On the USB stick people actually run this
    from, that becomes a page of files in a folder holding the backup itself --
    so the old ones go, and only the ones written by this program, matched
    against the exact name it writes.
    """
    try:
        ours = [path for path in directory.iterdir() if LOG_NAME.match(path.name)]
    except OSError:
        return []
    ours.sort(key=lambda path: path.name, reverse=True)
    removed = []
    for path in ours[keep:]:
        try:
            path.unlink()
        except OSError:  # in use by another copy, or gone already
            continue
        removed.append(path)
    return removed


def begin(purpose: str = "window", extra: dict | None = None) -> Path | None:
    """Open the log, write the start banner, and return where it went.

    Returns None when no candidate directory would take a file, which is not
    fatal: the window opens and works, it simply cannot leave a record.
    """
    path = logging_setup.writable_log_path(candidates())
    if path is not None:
        prune(path.parent)
    logging_setup.configure(path, verbose=False, quiet=False, console=None)
    logging_setup.log_start_banner(purpose, extra)
    return path
