"""Files OneDrive has not uploaded yet: backed up, not left to OneDrive."""

from __future__ import annotations

from pathlib import Path

from conftest import write

from winmigrate import capture as capture_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.models import Action
from winmigrate.platform_win import (
    CF_STATE_IN_SYNC,
    CF_STATE_PLACEHOLDER,
    CF_STATE_SYNC_ROOT,
    Environment,
)
from winmigrate.restore import RestoreOptions
from winmigrate.scan import run_scan
from winmigrate.scan import unsynced
from winmigrate.util import paths as pathutil

PASSPHRASE = "correct horse battery staple"
SYNCED = CF_STATE_PLACEHOLDER | CF_STATE_IN_SYNC
PENDING = CF_STATE_PLACEHOLDER          # known to OneDrive, edited since
NEW = 0                                 # never uploaded at all


def with_states(env: Environment, states: dict[Path, int]) -> Environment:
    """Tell the fixture what Windows would say about each file."""
    env.cloud_states = {pathutil.normalize_key(path): state for path, state in states.items()}
    return env


def onedrive(profile: Path, env: Environment) -> Path:
    """The fixture's OneDrive: two synced files, one edited, one new, and junk."""
    root = profile / "OneDrive"
    edited = write(root, "Documents/budget.xlsx", 900)
    new = write(root, "Documents/Neu/letter.docx", 1200)
    marker = write(root, ".849C9593-D756-4E56-8D6E-42412F2A707B", 80)
    lock = write(root, "Documents/~$letter.docx", 10)
    with_states(env, {
        root: CF_STATE_SYNC_ROOT | CF_STATE_PLACEHOLDER,
        root / "Documents": SYNCED,
        root / "Pictures": SYNCED,
        root / "Documents" / "synced.docx": SYNCED,
        root / "Pictures" / "photo.jpg": SYNCED,
        edited: PENDING,
        root / "Documents" / "Neu": NEW,
        new: NEW,
        marker: NEW,
        lock: NEW,
    })
    return root


def test_only_what_onedrive_does_not_have_is_found(profile: Path, env: Environment):
    root = onedrive(profile, env)

    found = sorted(f.relative for f in unsynced.walk_unsynced(root, env, ScanConfig()))

    # The edited file and the new one. Not what is in sync, not OneDrive's own
    # marker or Office's lock file, which are never uploaded and would
    # otherwise be "unsynced" in every backup for ever.
    assert found == ["Documents/Neu/letter.docx", "Documents/budget.xlsx"]


def test_a_client_that_does_not_report_sync_state_is_left_alone(profile: Path, env: Environment):
    """Without Cloud Files, "not a placeholder" means nothing -- every file
    would look unsynced, and the whole folder would land in the backup."""
    env.cloud_states = {}
    result = unsynced.count(profile / "OneDrive", env, ScanConfig())
    assert result.readable is False and result.files == 0


def test_the_preview_stops_counting_at_its_budget(profile: Path, env: Environment):
    root = onedrive(profile, env)
    for number in range(80):
        write(root, f"Bulk/file{number}.txt", 10)

    result = unsynced.count(root, env, ScanConfig(), budget_seconds=-1)

    assert result.complete is False


def test_the_bundle_keeps_the_folder_name_inside_the_profile(profile: Path, env: Environment):
    assert unsynced.archive_folder(profile / "OneDrive - Contoso", env) == "OneDrive - Contoso"
    assert unsynced.archive_folder(Path("/elsewhere/OneDrive"), env) == "OneDrive"


def test_the_plan_shows_them_and_the_sync_folder_says_so(profile: Path, env: Environment):
    onedrive(profile, env)

    scan = run_scan(ScanConfig(profile_root=profile, include_software=False), env)

    item = next(item for item in scan.items if item.id == "files:unsynced:0")
    assert item.action is Action.CAPTURE
    assert item.file_count == 2 and item.size_bytes == 900 + 1200
    assert item.archive_path == "data/unsynced/OneDrive"
    assert "not in onedrive yet" in item.notes[0].message.lower()
    report = next(item for item in scan.items if item.id.startswith("sync:onedrive"))
    assert any("not uploaded yet" in note.message for note in report.notes)


def test_nothing_is_added_when_onedrive_has_everything(profile: Path, env: Environment):
    root = profile / "OneDrive"
    with_states(env, {
        root: CF_STATE_SYNC_ROOT | CF_STATE_PLACEHOLDER,
        root / "Documents": SYNCED,
        root / "Pictures": SYNCED,
        root / "Documents" / "synced.docx": SYNCED,
        root / "Pictures" / "photo.jpg": SYNCED,
    })
    scan = run_scan(ScanConfig(profile_root=profile, include_software=False), env)
    assert not any(item.id.startswith("files:unsynced") for item in scan.items)


def test_they_travel_and_come_back_into_the_same_folder(
    profile: Path, env: Environment, tmp_path: Path
):
    root = onedrive(profile, env)
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "unsynced.dat"
    # Saved after the preview and before the backup: exactly the file this is for.
    late = write(root, "Documents/late.txt", 33)
    env.cloud_states[pathutil.normalize_key(late)] = NEW
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    destination = tmp_path / "new"
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )

    back = destination / "OneDrive" / "Documents"
    assert (back / "budget.xlsx").read_bytes() == b"x" * 900
    assert (back / "Neu" / "letter.docx").stat().st_size == 1200
    assert (back / "late.txt").exists()
    # What OneDrive has stays with OneDrive.
    assert not (back / "synced.docx").exists()
    assert not report.digest_mismatches


def test_a_file_already_on_the_new_machine_is_kept_and_the_newer_one_goes_beside_it(
    profile: Path, env: Environment, tmp_path: Path
):
    """The new machine's OneDrive may already have downloaded the cloud's older
    copy. Keeping it and dropping the backup's would lose the edit this exists
    to save; replacing it would lose the other. Both stay."""
    onedrive(profile, env)
    config = ScanConfig(profile_root=profile, include_software=False)
    bundle = tmp_path / "unsynced.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env,
    )
    destination = tmp_path / "new"
    cloud_copy = write(destination, "OneDrive/Documents/budget.xlsx", 400)
    same_size = destination / "OneDrive" / "Documents" / "Neu" / "letter.docx"
    write(destination, "OneDrive/Documents/Neu/letter.docx", 0)
    same_size.write_bytes(b"y" * 1200)

    report = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )

    assert cloud_copy.read_bytes() == b"x" * 400
    beside = cloud_copy.with_name("budget (from the old PC).xlsx")
    assert beside.read_bytes() == b"x" * 900
    assert same_size.read_bytes() == b"y" * 1200
    assert same_size.with_name("letter (from the old PC).docx").read_bytes() == b"x" * 1200
    assert report.kept_both == 2 and report.kept_existing == 0
    assert not report.digest_mismatches

    # And running it again does not pile up copies.
    again = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    names = sorted(p.name for p in (destination / "OneDrive" / "Documents").iterdir())
    assert names == ["Neu", "budget (from the old PC).xlsx", "budget.xlsx"]
    names = sorted(p.name for p in (destination / "OneDrive" / "Documents" / "Neu").iterdir())
    assert names == ["letter (from the old PC).docx", "letter.docx"]
    assert again.kept_both == 0 and not again.digest_mismatches


def test_the_bundle_path_maps_back_into_the_profile(tmp_path: Path):
    target = restore_mod._target_for("data/unsynced/OneDrive/Documents/a.txt", tmp_path)
    assert target == tmp_path / "OneDrive" / "Documents" / "a.txt"
    assert restore_mod._target_for("data/unsynced/../evil", tmp_path) is None


def test_files_the_clients_never_upload_are_not_flagged():
    for name in ("desktop.ini", "Thumbs.db", "~$letter.docx", "x.TMP",
                 ".849C9593-D756-4E56-8D6E-42412F2A707B"):
        assert unsynced.never_uploaded(name), name
    assert not unsynced.never_uploaded("letter.docx")
    assert not unsynced.never_uploaded(".gitignore")


def test_an_unfinished_count_still_puts_the_folder_in_the_backup(
    profile: Path, env: Environment, monkeypatch
):
    """Nothing found before the preview ran out of time is not "nothing to
    find": the backup checks every file, so the item has to be there for it."""
    monkeypatch.setattr(
        unsynced, "count",
        lambda root, env, config: unsynced.UnsyncedCount(root=str(root), complete=False),
    )
    scan = run_scan(ScanConfig(profile_root=profile, include_software=False), env)
    item = next(item for item in scan.items if item.id == "files:unsynced:0")
    assert item.action is Action.CAPTURE
    assert "at least" in item.notes[0].message
