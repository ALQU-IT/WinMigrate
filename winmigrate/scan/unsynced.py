"""Files in a sync folder that the sync client has not uploaded yet.

A OneDrive folder is left out of the backup because signing in on the new
machine brings it back. That is only true of what OneDrive actually has. A
document saved a minute before the backup, a folder OneDrive stopped on with
an error, a client that has been paused for a week, or one that was never
signed in properly -- all of these are on this disk and nowhere else, and
"OneDrive will bring it back" quietly loses them.

Windows knows the difference. Every file in a folder managed through the Cloud
Files API carries a state: a *placeholder* is a file the client knows about,
and one that is also *in sync* matches the copy in the cloud. Anything else
has not been uploaded. That state is read from the directory listing itself
(see :meth:`Environment.sync_state`): nothing is opened, so nothing is
downloaded, and a file that exists only in the cloud is never touched.

Those files go into the backup, and restore puts them back in the same folder,
where the client uploads them once it is signed in. A sync folder whose client
does not report sync state -- an older Nextcloud, say -- cannot be told apart,
and is left as it was, with a note saying so.
"""

from __future__ import annotations

import os
import re
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ..config import ScanConfig
from ..platform_win import (
    CF_STATE_IN_SYNC,
    CF_STATE_PLACEHOLDER,
    CF_STATE_SYNC_ROOT,
    CLOUD_PLACEHOLDER_MASK,
    FILE_ATTRIBUTE_REPARSE_POINT,
    REPARSE_TAG_NAME_SURROGATE,
    Environment,
)
from ..util import paths as pathutil
from .userfiles import CaptureFile

#: How long the preview spends counting before it settles for "at least".
#: The backup itself always checks every file; this only bounds the wait
#: before the plan is shown.
COUNT_BUDGET_SECONDS = 30.0

#: Files the sync clients never upload. Flagging them would put the same
#: handful into every backup as "not synced yet" forever.
NEVER_UPLOADED_NAMES = frozenset({"desktop.ini", "thumbs.db", ".ds_store", "icon\r"})
NEVER_UPLOADED_PREFIXES = ("~$", ".~lock.")
NEVER_UPLOADED_SUFFIXES = (".tmp",)
#: OneDrive's hidden marker at the top of its folder: a dot and a GUID.
_MARKER = re.compile(r"^\.[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$", re.IGNORECASE)


@dataclass(slots=True)
class UnsyncedCount:
    """What the preview found in one sync folder."""

    root: str
    files: int = 0
    bytes: int = 0
    compressible_bytes: int = 0
    #: False when the client does not report sync state, so nothing could be
    #: told apart. The folder is then left out whole, as before.
    readable: bool = True
    #: False when counting stopped at the time budget. The figures are a
    #: floor; the backup still checks every file.
    complete: bool = True


def never_uploaded(name: str) -> bool:
    lowered = name.lower()
    return (
        lowered in NEVER_UPLOADED_NAMES
        or lowered.startswith(NEVER_UPLOADED_PREFIXES)
        or lowered.endswith(NEVER_UPLOADED_SUFFIXES)
        or bool(_MARKER.match(name))
    )


def reports_sync_state(root: Path, env: Environment) -> bool:
    """Does the client behind this folder say which files it has?

    Asked of the folder itself -- the top of a Cloud Files sync folder is
    marked as one -- and, failing that, of what is directly inside it. A
    folder where nothing at all is a placeholder is one whose client does not
    use Cloud Files, and "not a placeholder" there means nothing.
    """
    try:
        own = env.sync_state(root, os.stat(pathutil.extended(root), follow_symlinks=False))
    except OSError:
        return False
    if own is not None and own & (CF_STATE_SYNC_ROOT | CF_STATE_PLACEHOLDER):
        return True
    try:
        entries = list(os.scandir(pathutil.extended(root)))
    except OSError:
        return False
    for entry in entries:
        try:
            state = env.sync_state(root / entry.name, entry.stat(follow_symlinks=False))
        except OSError:
            continue
        if state is not None and state & CF_STATE_PLACEHOLDER:
            return True
    return False


def walk_unsynced(
    root: Path,
    env: Environment,
    config: ScanConfig,
    *,
    deadline: float | None = None,
    stopped: list[bool] | None = None,
) -> Iterator[CaptureFile]:
    """Every file under ``root`` that is on this disk and not in the cloud.

    The one implementation of the rule, for the preview and the backup alike,
    so the plan somebody approves and the files that are taken cannot differ.
    ``deadline`` (a ``time.monotonic()`` value) is for the preview only; when
    it passes, ``stopped`` gets True appended and the walk ends.
    """
    stack = [root]
    looked = 0
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(pathutil.extended(current)))
        except OSError:
            continue
        for entry in entries:
            looked += 1
            if deadline is not None and looked % 64 == 0 and time.monotonic() > deadline:
                if stopped is not None:
                    stopped.append(True)
                return
            # From the canonical parent, never entry.path: see walk_tree.
            path = current / entry.name
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            attributes = getattr(stat_result, "st_file_attributes", 0)
            tag = getattr(stat_result, "st_reparse_tag", 0)
            if attributes & FILE_ATTRIBUTE_REPARSE_POINT and tag & REPARSE_TAG_NAME_SURROGATE:
                continue  # a junction or link: somewhere else, not this folder
            if config.is_excluded(pathutil.relative_posix(path, env.profile_root), entry.name):
                continue
            if is_dir:
                stack.append(path)
                continue
            if attributes & CLOUD_PLACEHOLDER_MASK:
                continue  # its bytes are only in the cloud, so the cloud has it
            if never_uploaded(entry.name):
                continue
            state = env.sync_state(path, stat_result)
            if state is None:
                continue  # cannot tell; the folder-level check decides these
            if state & CF_STATE_PLACEHOLDER and state & CF_STATE_IN_SYNC:
                continue
            yield CaptureFile(
                path=path,
                relative=pathutil.relative_posix(path, root),
                size=stat_result.st_size,
            )


def count(
    root: Path, env: Environment, config: ScanConfig, budget_seconds: float = COUNT_BUDGET_SECONDS
) -> UnsyncedCount:
    """How much under ``root`` has not been uploaded, for the preview."""
    from .. import compression  # noqa: PLC0415

    result = UnsyncedCount(root=str(root))
    if not root.is_dir() or not reports_sync_state(root, env):
        result.readable = False
        return result
    stopped: list[bool] = []
    for found in walk_unsynced(
        root, env, config, deadline=time.monotonic() + budget_seconds, stopped=stopped
    ):
        result.files += 1
        result.bytes += found.size
        if not compression.is_incompressible(found.path.name):
            result.compressible_bytes += found.size
    result.complete = not stopped
    return result


def archive_folder(root: Path, env: Environment) -> str:
    """Where a sync folder's unsynced files sit in the bundle, and come back to.

    Its path inside the profile -- "OneDrive", "OneDrive - Contoso" -- so that
    restore puts them back in the same folder, where the client finds them.
    A sync folder outside the profile keeps its own name.
    """
    if pathutil.is_within(str(root), str(env.profile_root)):
        relative = pathutil.relative_posix(root, env.profile_root)
        if relative and relative != ".":
            return relative
    return root.name or "Synced folder"
