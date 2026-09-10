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


# --- tab counting ----------------------------------------------------------
def test_a_closed_tab_leaves_its_buffer_behind_and_is_not_counted(tmp_path: Path):
    """Taken verbatim from a real Windows 11 profile with three tabs open.

    Four GUID-named buffers were present. The three live tabs each had numbered
    companions -- .0.bin and .1.bin, which Notepad writes while you type and
    removes when a tab closes -- and the fourth had only its main .bin, left
    behind by a tab closed earlier and not yet cleaned up. Counting .bin files
    reported four tabs to a user looking at three.

    Note the two zero-length .1.bin companions: presence is the signal, not
    size, so they must still count as companions.
    """
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    tab_state = local_state(root) / "TabState"
    tab_state.mkdir(parents=True)
    listing = {
        "01735f75-64e0-4e33-a14c-74a60a856b35.0.bin": 20,
        "01735f75-64e0-4e33-a14c-74a60a856b35.1.bin": 0,
        "01735f75-64e0-4e33-a14c-74a60a856b35.bin": 132,
        "174dda67-2da9-4eeb-8ae5-71b8749f2fca.0.bin": 22,
        "174dda67-2da9-4eeb-8ae5-71b8749f2fca.1.bin": 22,
        "174dda67-2da9-4eeb-8ae5-71b8749f2fca.bin": 3865,
        "6ab07dd5-1bfb-46dd-9b6b-b799e8702fed.bin": 109,   # closed, no companions
        "a3a859b5-fbb7-4a54-ae6f-28db13664ad5.0.bin": 20,
        "a3a859b5-fbb7-4a54-ae6f-28db13664ad5.1.bin": 0,
        "a3a859b5-fbb7-4a54-ae6f-28db13664ad5.bin": 176,
    }
    for name, size in listing.items():
        (tab_state / name).write_bytes(b"x" * size)

    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert "3 tab" in items[0].title

    # The closed tab's buffer is still captured -- deciding which files Notepad
    # needs is not this tool's job. Only the reported number is affected.
    scanned = run_scan(ScanConfig(profile_root=root, include_software=False),
                       Environment.fixture(root, {}))
    session = next(i for i in scanned.items if i.id == "notepad:session")
    assert session.file_count == len(listing)


def test_companions_are_ignored_as_a_signal_when_they_distinguish_nothing(tmp_path: Path):
    """If every buffer has companions, or none does, they say nothing about
    which tabs are live -- so every buffer is counted rather than none."""
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    tab_state = local_state(root) / "TabState"
    tab_state.mkdir(parents=True)
    for index in range(3):
        guid = f"a1b2c3d4-0000-0000-0000-00000000000{index}"
        (tab_state / f"{guid}.bin").write_bytes(b"a note")
    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert "3 tab" in items[0].title

    for index in range(3):
        guid = f"a1b2c3d4-0000-0000-0000-00000000000{index}"
        (tab_state / f"{guid}.0.bin").write_bytes(b"pending")
    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert "3 tab" in items[0].title


def test_only_real_tab_buffers_are_counted(tmp_path: Path):
    """The count is the only thing telling the user whether their notes are in
    the bundle, so it has to match what Notepad shows them.

    TabState holds more than tabs: bookkeeping files that are not GUID-named,
    and GUID-named buffers Notepad has created but not written. Counting either
    reports more tabs than exist. Both are still *captured* -- the whole folder
    travels, because guessing which files Notepad needs is not this tool's job
    -- they just do not inflate the number.
    """
    root = tmp_path / "alice"
    state = build_session(root, tabs=3)
    tab_state = state / "TabState"
    for index in range(3):
        guid = f"a1b2c3d4-0000-0000-0000-00000000000{index}"
        (tab_state / f"{guid}.0.bin").write_bytes(b"pending edit")   # same tab
    (tab_state / "a1b2c3d4-0000-0000-0000-000000000099.bin").write_bytes(b"")  # empty
    (tab_state / "tabstate.metadata.bin").write_bytes(b"bookkeeping")  # not a tab

    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert "3 tab" in items[0].title


def test_an_unfamiliar_naming_scheme_counts_buffers_rather_than_reporting_none(
    tmp_path: Path,
):
    """The GUID naming is reverse-engineered, not documented, so it may change.

    If it does, reporting zero tabs for a folder that plainly holds buffers is
    the one answer that would make a user think their notes were not captured
    -- and they would be wrong, because the folder travels either way. So an
    unrecognised naming falls back to counting what is there.
    """
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    tab_state = local_state(root) / "TabState"
    tab_state.mkdir(parents=True)
    for name in ("tab_one.bin", "tab_two.bin"):
        (tab_state / name).write_bytes(b"a note")

    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert items and "2 tab" in items[0].title


# --- Notepad++ -------------------------------------------------------------
def notepadpp(root: Path, unsaved: int = 2) -> Path:
    config = root / "AppData" / "Roaming" / "Notepad++"
    (config / "backup").mkdir(parents=True)
    for index in range(unsaved):
        (config / "backup" / f"new {index + 1}@2026-09-10_12000{index}").write_text(
            f"unsaved buffer {index}"
        )
    (config / "session.xml").write_text("<NotepadPlus><Session/></NotepadPlus>")
    (config / "config.xml").write_text("<NotepadPlus/>")
    (config / "themes").mkdir()
    (config / "themes" / "Dark.xml").write_text("<x/>")
    return config


def test_notepad_plus_plus_session_and_settings_are_captured(tmp_path: Path):
    """Notepad++ has the same habit and the same consequence: text that was
    never saved to a file, held in a config folder nothing else backs up."""
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    notepadpp(root)

    items, followups = notepad.scan_notepad(Environment.fixture(root, {}))
    item = next(i for i in items if i.id == "notepadpp:session")
    assert item.sensitivity is Sensitivity.SECRET
    assert "2 unsaved buffer" in item.title
    assert item.archive_path == "secrets/AppData/Roaming/Notepad++"
    assert item.restore.target == "%APPDATA%\\Notepad++"
    assert any(f.id == "notepadpp:session" for f in followups)


def test_notepad_plus_plus_with_no_unsaved_buffers_still_carries_the_settings(
    tmp_path: Path,
):
    """Themes, shortcuts and preferences are worth carrying on their own, and
    the title should not claim buffers that are not there."""
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    config = notepadpp(root, unsaved=0)
    (config / "backup" / "stale").write_text("")  # empty: not a buffer

    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    item = next(i for i in items if i.id == "notepadpp:session")
    assert item.title == "Notepad++ configuration"
    assert item.record["unsaved_buffers"] == 0


def test_notepad_plus_plus_not_installed_produces_nothing(tmp_path: Path):
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    items, _ = notepad.scan_notepad(Environment.fixture(root, {}))
    assert not any(i.id == "notepadpp:session" for i in items)


def test_files_only_leaves_notepad_plus_plus_behind_too(tmp_path: Path):
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    notepadpp(root)
    items, followups = notepad.scan_notepad(Environment.fixture(root, {}), files_only=True)
    item = next(i for i in items if i.id == "notepadpp:session")
    assert item.action is Action.SKIP and item.skip_reason is SkipReason.FILES_ONLY_MODE
    assert not any(f.id == "notepadpp:session" for f in followups)


def test_both_editors_round_trip_to_their_real_locations(tmp_path: Path):
    root = tmp_path / "alice"
    build_session(root, tabs=2)
    notepadpp(root)

    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False),
        config,
        env,
    )
    destination = tmp_path / "restored"
    destination.mkdir()
    result = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok and not result.digest_mismatches
    assert (
        destination / "AppData" / "Local" / "Packages" / PACKAGE / "LocalState" / "TabState"
    ).is_dir()
    npp_backup = destination / "AppData" / "Roaming" / "Notepad++" / "backup"
    assert npp_backup.is_dir() and any(npp_backup.iterdir())
    assert (destination / "AppData" / "Roaming" / "Notepad++" / "themes" / "Dark.xml").is_file()
