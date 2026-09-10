"""Path helpers: long-path handling, containment tests, portable normalization.

Everything here must work on non-Windows hosts too, so the scan stage can be
exercised against a fixture profile tree in tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePath, PureWindowsPath

#: Windows' classic MAX_PATH. Paths at or beyond this need the \\?\ prefix
#: unless the machine has long paths enabled system-wide.
MAX_PATH = 260

_EXTENDED_PREFIX = "\\\\?\\"
_UNC_EXTENDED_PREFIX = "\\\\?\\UNC\\"


def is_windows() -> bool:
    return sys.platform == "win32"


def extended(path: os.PathLike[str] | str) -> str:
    """Return ``path`` in a form the Win32 API accepts regardless of length.

    On non-Windows hosts (development and tests) this is the identity function,
    so callers can wrap unconditionally.
    """
    if not is_windows():
        return os.fspath(path)
    text = os.fspath(path)
    if text.startswith(_EXTENDED_PREFIX):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return _UNC_EXTENDED_PREFIX + absolute[2:]
    return _EXTENDED_PREFIX + absolute


def strip_extended(path: os.PathLike[str] | str) -> str:
    r"""Remove an extended-length prefix (``\\?\`` or ``\\?\UNC\``) if present.

    :func:`extended` output must never be stored -- ``os.DirEntry.path`` inherits
    the prefix from the directory that was scanned, and a stored prefixed path
    breaks every relative-path and containment computation downstream. This is
    the safety net for anywhere one slips through.
    """
    text = os.fspath(path)
    # Accept either separator: the prefix may survive a backslash-to-slash pass.
    probe = text.replace("/", "\\")
    if probe.startswith(_UNC_EXTENDED_PREFIX):
        return text[:2] + text[len(_UNC_EXTENDED_PREFIX) :]
    if probe.startswith(_EXTENDED_PREFIX):
        return text[len(_EXTENDED_PREFIX) :]
    return text


def needs_long_path_support(path: os.PathLike[str] | str) -> bool:
    """True when ``path`` would break tools that are not long-path aware."""
    return len(os.fspath(path)) >= MAX_PATH


def normalize_key(path: os.PathLike[str] | str) -> str:
    """Case- and separator-insensitive key for comparing Windows paths.

    Used for containment tests and de-duplication. Windows paths are compared
    case-insensitively because the filesystem is; POSIX fixture paths keep their
    case so tests stay meaningful on Linux.
    """
    text = strip_extended(path).replace("\\", "/").rstrip("/")
    if not text:
        text = "/"
    return text.casefold() if _looks_windows(text) or is_windows() else text


def _looks_windows(text: str) -> bool:
    return len(text) >= 2 and text[1] == ":" and text[0].isalpha()


def is_within(child: os.PathLike[str] | str, parent: os.PathLike[str] | str) -> bool:
    """True when ``child`` is ``parent`` or lives beneath it."""
    c = normalize_key(child)
    p = normalize_key(parent)
    return c == p or c.startswith(p.rstrip("/") + "/")


def to_posix(path: os.PathLike[str] | str) -> str:
    """Return ``path`` with ``/`` separators, preserving case and drive letter."""
    return strip_extended(path).replace("\\", "/")


def relative_posix(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> str:
    """Relative path from ``root`` to ``path`` using ``/`` separators.

    Archive paths are stored this way so a bundle written on one machine reads
    identically on another. Windows and POSIX inputs are both accepted on either
    host, so this never delegates to ``os.path.relpath``'s native separator.
    """
    child = to_posix(path).rstrip("/")
    parent = to_posix(root).rstrip("/")
    if is_within(child, parent):
        rel = child[len(parent) :].lstrip("/")
        return rel or "."
    # Not under the base. Exclusion matching still needs *something* stable, and
    # a scan must not die on one odd path, so fall back to the child with any
    # drive letter and leading separators removed.
    without_drive = child.split(":", 1)[1] if _looks_windows(child) else child
    return without_drive.lstrip("/") or child


def relative_within(path: os.PathLike[str] | str, root: os.PathLike[str] | str) -> str | None:
    """Relative path from ``root`` to ``path``, or ``None`` when it is outside.

    :func:`relative_posix` never fails, because exclusion matching needs *some*
    stable string for every path. Placement in the bundle is the opposite case:
    an archive path below ``data/`` or ``secrets/`` means "profile-relative", and
    restore joins it onto the destination profile. Handing it the drive-stripped
    fallback for something that never lived in the profile -- a Firefox profile
    on ``D:\\`` -- would silently move the data to a path that means nothing.
    So callers that decide *where a thing goes* ask this instead and handle the
    outside-the-profile case deliberately.
    """
    if not is_within(path, root):
        return None
    return relative_posix(path, root)


def expand(text: str, environ: dict[str, str] | None = None) -> str:
    """Expand ``%VAR%`` (and ``$VAR``) using ``environ`` or the process env."""
    if environ is None:
        return os.path.expandvars(text)
    result = text
    for name, value in environ.items():
        result = result.replace(f"%{name}%", value)
        result = result.replace(f"%{name.upper()}%", value)
        result = result.replace(f"%{name.lower()}%", value)
    return result


def display(path: os.PathLike[str] | str, home: os.PathLike[str] | str | None = None) -> str:
    """Shorten a path for console output by collapsing the profile root."""
    text = os.fspath(path)
    if home and is_within(text, home):
        rel = relative_posix(text, home)
        return "~" if rel == "." else f"~/{rel}"
    return text


def as_windows(path: os.PathLike[str] | str) -> PurePath:
    """Parse ``path`` as a Windows path even when running on POSIX."""
    return PureWindowsPath(os.fspath(path))


def ensure_directory(path: Path) -> Path:
    """Create ``path`` (and parents) if needed and return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path
