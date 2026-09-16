"""The program data under AppData, named one program at a time."""

from __future__ import annotations

from pathlib import Path

from winmigrate.models import Action, Category, Kind, Sensitivity, SkipReason
from winmigrate.platform_win import Environment
from winmigrate.scan import appdata


def profile_with(root: Path, *relative: str) -> Environment:
    for entry in relative:
        folder = root / entry
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "settings.dat").write_bytes(b"data")
    return Environment.fixture(root)


ROAMING = "AppData/Roaming"
LOCAL = "AppData/Local"


def test_a_named_location_travels_although_appdata_does_not(tmp_path: Path):
    """AppData is excluded from the file scan, and that default is right: it is
    where every program keeps its caches and machine-bound tokens. It is also
    where Thunderbird keeps entire mailboxes, which no installer brings back."""
    env = profile_with(tmp_path, f"{ROAMING}/Thunderbird")

    items, _ = appdata.scan_app_data(env)

    (item,) = items
    assert item.id == "appdata:thunderbird"
    assert item.category is Category.APP_DATA
    assert item.kind is Kind.TREE
    assert item.archive_path == "data/AppData/Roaming/Thunderbird"


def test_nothing_is_carried_for_a_program_that_is_not_installed(tmp_path: Path):
    items, _ = appdata.scan_app_data(Environment.fixture(tmp_path))
    assert items == []


def test_an_empty_folder_left_behind_by_an_uninstall_is_not_carried(tmp_path: Path):
    (tmp_path / ROAMING / "vlc").mkdir(parents=True)
    items, _ = appdata.scan_app_data(Environment.fixture(tmp_path))
    assert items == []


def test_a_store_app_is_found_under_local_rather_than_roaming(tmp_path: Path):
    """Sticky Notes are usually the only copy of what is on them."""
    env = profile_with(
        tmp_path, f"{LOCAL}/Packages/Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe/LocalState"
    )

    (item,), _ = appdata.scan_app_data(env)

    assert item.id == "appdata:sticky_notes"
    assert item.archive_path.startswith("data/AppData/Local/Packages/")


def test_a_program_that_stores_passwords_travels_encrypted_only(tmp_path: Path):
    """FileZilla's Site Manager keeps server passwords in the clear. That makes
    it credential material, and credential material rides in the encrypted
    payload with a redacted stub in the sidecar -- the same rule as SSH keys."""
    env = profile_with(tmp_path, f"{ROAMING}/FileZilla")

    (item,), _ = appdata.scan_app_data(env)

    assert item.sensitivity is Sensitivity.SECRET
    # Profile-relative under the secrets prefix, so it rebuilds against the new
    # profile rather than landing in the profile root.
    assert item.archive_path == "secrets/AppData/Roaming/FileZilla"


def test_files_only_drops_the_one_that_holds_passwords(tmp_path: Path):
    """--files-only promises no credential material travels."""
    env = profile_with(tmp_path, f"{ROAMING}/FileZilla", f"{ROAMING}/vlc")

    items, _ = appdata.scan_app_data(env, files_only=True)
    by_id = {item.id: item for item in items}

    assert by_id["appdata:filezilla"].action is Action.SKIP
    assert by_id["appdata:filezilla"].skip_reason is SkipReason.FILES_ONLY_MODE
    assert by_id["appdata:vlc"].action is Action.CAPTURE


def test_every_entry_says_why_somebody_would_miss_it():
    """The table is the intended way to make a migration more complete, so an
    entry without a reason is an entry nobody can review."""
    for location in appdata.LOCATIONS:
        assert location.why.strip().endswith("."), location.slot
        assert len(location.why) > 30, location.slot
        assert location.title and not location.title.endswith("."), location.slot


def test_the_ones_that_need_their_program_first_say_so(tmp_path: Path):
    """Opening Thunderbird before its profile is back means Thunderbird writes a
    new empty one over it."""
    env = profile_with(tmp_path, f"{ROAMING}/Thunderbird")

    (item,), _ = appdata.scan_app_data(env)

    assert any("before opening it" in note.message for note in item.notes)
