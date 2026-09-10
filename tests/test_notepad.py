"""Notepad's running session -- the tabs that were never saved to a file.

The whole point of capturing this is that nothing else does: an unsaved tab is
not a file the user has named, so it is invisible to every folder-based backup
and is lost the moment the old machine goes. It is also, for exactly the same
reason, where a pasted password tends to sit, so it travels encrypted-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate import capture as capture_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.models import Action, Sensitivity, SkipReason
from winmigrate.platform_win import Environment
from winmigrate.restore import RestoreOptions
from winmigrate.scan import notepad, run_scan

PASSPHRASE = "correct horse battery staple"
PACKAGE = "Microsoft.WindowsNotepad_8wekyb3d8bbwe"


def local_state(root: Path) -> Path:
    return root / "AppData" / "Local" / "Packages" / PACKAGE / "LocalState"


def build_session(root: Path, tabs: int = 2) -> Path:
    """A profile with Notepad holding `tabs` unsaved tabs."""
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (root / "Documents" / "saved.txt").write_text("an ordinary file")
    state = local_state(root)
    tab_state = state / "TabState"
    tab_state.mkdir(parents=True)
    for index in range(tabs):
        guid = f"a1b2c3d4-0000-0000-0000-00000000000{index}"
        (tab_state / f"{guid}.bin").write_bytes(b"NP\x00unsaved note " + str(index).encode())
    return state


def test_a_session_with_unsaved_tabs_is_captured_encrypted_only(tmp_path: Path):
    root = tmp_path / "alice"
    build_session(root)
    items, followups = notepad.scan_notepad(Environment.fixture(root, {}))

    item = items[0]
    assert item.id == "notepad:session"
    assert item.sensitivity is Sensitivity.SECRET
    assert "2 tab" in item.title
    # Placement mirrors the real location, so it restores where Notepad looks.
    assert item.archive_path == f"secrets/AppData/Local/Packages/{PACKAGE}/LocalState"
    assert item.restore.target == f"%LOCALAPPDATA%\\Packages\\{PACKAGE}\\LocalState"
    assert [f.id for f in followups] == ["notepad:session"]


def test_pending_edit_side_files_are_not_counted_as_extra_tabs(tmp_path: Path):
    """Notepad writes <guid>.1.bin alongside <guid>.bin for unflushed edits.
    They are the same tab, and counting them would tell the user they have twice
    as many notes as they do."""
    root = tmp_path / "alice"
    state = build_session(root, tabs=1)
    guid = "a1b2c3d4-0000-0000-0000-000000000000"
    (state / "TabState" / f"{guid}.1.bin").write_bytes(b"pending")
    (state / "TabState" / f"{guid}.2.bin").write_bytes(b"pending too")

    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert "1 tab" in items[0].title


def test_notepad_that_has_run_but_holds_no_session_produces_nothing(tmp_path: Path):
    """The folder exists as soon as Notepad has been opened once. Capturing an
    empty LocalState would do nothing but overwrite a good session on the new
    machine."""
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    (local_state(root) / "TabState").mkdir(parents=True)

    assert notepad.scan_notepad(Environment.fixture(root, {})) == ([], [])


def test_notepad_never_run_produces_nothing(tmp_path: Path):
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    assert notepad.scan_notepad(Environment.fixture(root, {})) == ([], [])


def test_files_only_leaves_the_session_behind(tmp_path: Path):
    """An unsaved tab is text the user wrote, but it is also the classic place
    to paste a password, so it follows every other SECRET item out of a
    files-only migration."""
    root = tmp_path / "alice"
    build_session(root)
    items, followups = notepad.scan_notepad(Environment.fixture(root, {}), files_only=True)

    assert items[0].action is Action.SKIP
    assert items[0].skip_reason is SkipReason.FILES_ONLY_MODE
    assert followups == []


def test_round_trip_puts_the_session_back_where_notepad_looks(tmp_path: Path):
    root = tmp_path / "alice"
    state = build_session(root)
    (state / "WindowState").mkdir()
    (state / "WindowState" / "1.bin").write_bytes(b"window layout")
    (state / "TabState" / "scratch.tmp").write_bytes(b"junk")  # excluded as *.tmp

    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    # The plaintext sidecar must describe it without naming or revealing it.
    sidecar = json.loads((tmp_path / "b.manifest.json").read_text())
    entry = next(i for i in sidecar["items"] if i["id"] == "notepad:session")
    assert entry["redacted"] is True
    assert "source_path" not in entry and "archive_path" not in entry
    assert "unsaved note" not in (tmp_path / "b.manifest.json").read_text()

    destination = tmp_path / "restored"
    result = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok and not result.digest_mismatches
    restored = destination / "AppData" / "Local" / "Packages" / PACKAGE / "LocalState"
    guid = "a1b2c3d4-0000-0000-0000-000000000000"
    assert (restored / "TabState" / f"{guid}.bin").read_bytes() == b"NP\x00unsaved note 0"
    assert (restored / "WindowState" / "1.bin").is_file()
    assert not (restored / "TabState" / "scratch.tmp").exists()


def test_no_notepad_leaves_the_session_out_of_the_scan(tmp_path: Path):
    """The one case an opt-out is for: a tab holding something the user would
    rather did not travel at all, not even encrypted."""
    root = tmp_path / "alice"
    build_session(root)
    env = Environment.fixture(root, {})

    on = run_scan(ScanConfig(profile_root=root, include_software=False), env)
    assert any(i.id == "notepad:session" for i in on.items)

    off = run_scan(
        ScanConfig(profile_root=root, include_software=False, include_notepad=False), env
    )
    assert not any(i.id == "notepad:session" for i in off.items)
    assert not any(f.id == "notepad:session" for f in off.followups)
