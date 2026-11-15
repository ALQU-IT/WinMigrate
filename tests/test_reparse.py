"""Reparse points: which ones are links, and which are just cloud-managed files."""

from __future__ import annotations

import os
import types
from pathlib import Path

from conftest import write

from winmigrate import platform_win
from winmigrate.config import ScanConfig
from winmigrate.models import SkipReason
from winmigrate.platform_win import FILE_ATTRIBUTE_REPARSE_POINT, Environment, is_link
from winmigrate.scan import userfiles
from winmigrate.scan.userfiles import CaptureFile

CLOUD = 0x9000001A          # IO_REPARSE_TAG_CLOUD
CLOUD_6 = 0x9000601A        # IO_REPARSE_TAG_CLOUD_6, what OneDrive usually uses
JUNCTION = 0xA0000003       # IO_REPARSE_TAG_MOUNT_POINT
SYMLINK = 0xA000000C
OFFLINE = 0x1000


def test_a_cloud_managed_file_is_not_a_link():
    assert not is_link(FILE_ATTRIBUTE_REPARSE_POINT, CLOUD)
    assert not is_link(FILE_ATTRIBUTE_REPARSE_POINT, CLOUD_6)
    assert is_link(FILE_ATTRIBUTE_REPARSE_POINT, JUNCTION)
    assert is_link(FILE_ATTRIBUTE_REPARSE_POINT, SYMLINK)
    # A tag nobody recognises is still not walked into on a guess.
    assert is_link(FILE_ATTRIBUTE_REPARSE_POINT, 0)
    assert is_link(FILE_ATTRIBUTE_REPARSE_POINT, 0x80000013)
    # And without the attribute there is nothing to ask about.
    assert not is_link(0, JUNCTION)


def windows_listing(monkeypatch, marks: dict[str, tuple[int, int]]) -> None:
    """Make directory listings report Windows attributes for the named entries."""
    real = os.scandir

    class Entry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name

        def is_dir(self, follow_symlinks=True):
            return self._entry.is_dir(follow_symlinks=follow_symlinks)

        def stat(self, follow_symlinks=True):
            plain = self._entry.stat(follow_symlinks=follow_symlinks)
            attributes, tag = marks.get(self.name, (0, 0))
            return types.SimpleNamespace(
                st_size=plain.st_size, st_file_attributes=attributes, st_reparse_tag=tag
            )

    monkeypatch.setattr(userfiles.os, "scandir", lambda path: [Entry(e) for e in real(path)])


def test_a_dropbox_style_folder_is_backed_up_not_skipped_as_a_junction(
    tmp_path: Path, monkeypatch
):
    """iCloud, Dropbox and Google Drive manage their folders through the same
    Windows mechanism as OneDrive, so every file and folder in them is a
    reparse point. They are not sync folders this tool knows to leave out, so
    they are backed up -- and used to be skipped whole, as "a junction"."""
    profile = tmp_path / "alice"
    write(profile, "Dropbox/Work/plan.docx", 500)
    write(profile, "Dropbox/Work/online-only.mp4", 0)
    write(profile, "Dropbox/Elsewhere/target.txt", 10)
    windows_listing(monkeypatch, {
        "Work": (FILE_ATTRIBUTE_REPARSE_POINT, CLOUD_6),
        "plan.docx": (FILE_ATTRIBUTE_REPARSE_POINT, CLOUD_6),
        "online-only.mp4": (FILE_ATTRIBUTE_REPARSE_POINT | OFFLINE, CLOUD_6),
        "Elsewhere": (FILE_ATTRIBUTE_REPARSE_POINT, JUNCTION),
    })
    env = Environment.fixture(profile)

    events = list(userfiles.walk_tree(profile / "Dropbox", ScanConfig(), env))

    captured = [e.relative for e in events if isinstance(e, CaptureFile)]
    skipped = {e.path.name: e.reason for e in events if not isinstance(e, CaptureFile)}
    assert captured == ["Work/plan.docx"]
    # Still not downloaded, and a real junction is still not followed.
    assert skipped == {
        "online-only.mp4": SkipReason.CLOUD_PLACEHOLDER,
        "Elsewhere": SkipReason.REPARSE_POINT,
    }


def test_asking_windows_to_show_placeholders_is_never_done_to_a_fixture(monkeypatch):
    monkeypatch.setattr(platform_win, "_EXPOSED", False)
    platform_win.expose_cloud_placeholders(Environment.fixture("/nowhere"))
    assert platform_win._EXPOSED is False
