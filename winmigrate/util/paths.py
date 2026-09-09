"""Path helpers: long-path handling, containment tests, portable normalization.

Everything here must work on non-Windows hosts too, so the scan stage can be
exercised against a fixture profile tree in tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath

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


def needs_long_path_support(path: os.PathLike[str] | str) -> bool:
    """True when ``path`` would break tools that are not long-path aware."""
    return len(os.fspath(path)) >= MAX_PATH


def normalize_key(path: os.PathLike[str] | str) -> str:
    """Case- and separator-insensitive key for comparing Windows paths.

    Used for containment tests and de-duplication. Windows paths are compared
    case-insensitively because the filesystem is; POSIX fixture paths keep their
    case so tests stay meaningful on Linux.
    """
    text = os.fspath(path).replace("\\", "/").rstrip("/")
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
    return os.fspath(path).replace("\\", "/")


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
    return PurePosixPath(child).relative_to("/").as_posix() if child.startswith("/") else child


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
