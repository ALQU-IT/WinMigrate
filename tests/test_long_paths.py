r"""Regression tests for extended-length (``\\?\``) path handling.

On Windows, ``pathutil.extended()`` prefixes a path so the Win32 API accepts it
regardless of length -- and ``os.DirEntry.path`` inherits that prefix from the
directory that was scanned. Storing such a path corrupts every downstream
relative path, exclusion match and recorded source path.

On POSIX ``extended()`` is the identity function, so this class of bug is
invisible to the rest of the suite. These tests reproduce the Windows behaviour
explicitly.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from winmigrate.config import ScanConfig
from winmigrate.platform_win import Environment
from winmigrate.scan import userfiles
from winmigrate.util import paths as pathutil

WINDOWS_PREFIX = "\\\\?\\"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (r"\\?\C:\Users\a\x.tar", r"C:\Users\a\x.tar"),
        ("//?/C:/Users/a/x.tar", "C:/Users/a/x.tar"),
        (r"\\?\UNC\srv\share\f", r"\\srv\share\f"),
        (r"C:\Users\a", r"C:\Users\a"),
        ("/home/u/file", "/home/u/file"),
    ],
)
def test_strip_extended_handles_both_separator_forms(raw, expected):
    assert pathutil.strip_extended(raw) == expected


def test_relative_posix_survives_the_prefix_on_the_child():
    """The exact shape that crashed a real scan of C:\\Users\\<name>\\Desktop."""
    child = r"\\?\C:\Users\alessio\Desktop\alqu-nas-25.04.1.tar"
    assert pathutil.relative_posix(child, r"C:\Users\alessio") == "Desktop/alqu-nas-25.04.1.tar"


def test_relative_posix_never_raises_for_a_path_outside_its_base():
    # A scan must not die on one odd path; it falls back to something stable.
    assert pathutil.relative_posix("/other/place/f", "/home/u") == "other/place/f"
    assert pathutil.relative_posix(r"D:\data\f", r"C:\Users\a") == r"data\f".replace("\\", "/")


def test_normalize_key_ignores_the_prefix():
    assert pathutil.normalize_key(r"\\?\C:\Users\A") == pathutil.normalize_key(r"C:\Users\a")


def test_is_within_works_across_prefixed_and_plain_paths():
    assert pathutil.is_within(r"\\?\C:\Users\a\Documents\f", r"C:\Users\a")


class _PrefixedDirEntry:
    """A DirEntry whose ``.path`` carries the prefix, as Windows returns."""

    def __init__(self, real: os.DirEntry, prefix: str) -> None:
        self._real = real
        self.name = real.name
        self.path = prefix + real.path

    def is_dir(self, *, follow_symlinks: bool = True) -> bool:
        return self._real.is_dir(follow_symlinks=follow_symlinks)

    def is_file(self, *, follow_symlinks: bool = True) -> bool:
        return self._real.is_file(follow_symlinks=follow_symlinks)

    def stat(self, *, follow_symlinks: bool = True):
        return self._real.stat(follow_symlinks=follow_symlinks)


@pytest.fixture
def windows_like_scandir(monkeypatch):
    """Make os.scandir behave as it does under an extended-length open."""
    real_scandir = os.scandir

    def fake_scandir(path):
        cleaned = pathutil.strip_extended(os.fspath(path))
        return [_PrefixedDirEntry(entry, WINDOWS_PREFIX) for entry in real_scandir(cleaned)]

    monkeypatch.setattr(userfiles.os, "scandir", fake_scandir)
    return fake_scandir


def test_the_walk_is_unaffected_by_prefixed_dir_entries(
    profile: Path, env: Environment, windows_like_scandir
):
    """Exclusions and totals are identical whether or not entries are prefixed.

    Both layers of the defence are exercised here: the walk builds child paths
    from the canonical parent, and ``strip_extended`` catches a prefix that
    reaches a path helper anyway. The rule itself -- never store ``entry.path``
    -- is enforced by ``test_no_scan_module_stores_a_dir_entry_path``.
    """
    config = ScanConfig(profile_root=profile)
    measurement = userfiles.measure_tree(profile / "Documents", config, env)

    assert measurement.size_bytes == 5500  # node_modules pruned, junk excluded
    assert measurement.file_count == 3
    assert WINDOWS_PREFIX not in measurement.root
    for group in measurement.skipped:
        assert WINDOWS_PREFIX not in group.path


def test_no_scan_module_stores_a_dir_entry_path():
    """Guard the rule that produced this bug: extended() output is never stored.

    ``entry.path`` is prefixed whenever the directory was opened through
    ``pathutil.extended()``. Child paths must be built from the canonical parent
    (``current / entry.name``) instead. POSIX cannot catch a regression here, so
    the rule is enforced on the source itself.
    """
    package = Path(__file__).resolve().parent.parent / "winmigrate"
    offenders = []
    for source in package.rglob("*.py"):
        for number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            if "entry.path" in code:
                offenders.append(f"{source.name}:{number}: {line.strip()}")
    assert not offenders, "build child paths from the parent, not entry.path:\n" + "\n".join(offenders)
