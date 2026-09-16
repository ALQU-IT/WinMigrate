"""End-to-end capture and restore, plus the failure modes that matter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from winmigrate import bundle as bundle_mod
from winmigrate import capture as capture_mod
from winmigrate import manifest as manifest_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureError, CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.platform_win import Environment
from winmigrate.restore import RestoreError, RestoreOptions
from winmigrate.scan import run_scan

PASSPHRASE = "correct horse battery staple"


def digests(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture
def captured(profile: Path, env: Environment, tmp_path: Path):
    """A real bundle written from the fixture profile."""
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    output = tmp_path / "out" / "test.dat"
    options = CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False)
    report = capture_mod.capture(scan, options, config, env)
    return report, scan


def test_capture_writes_a_bundle_and_a_sidecar(captured):
    report, _scan = captured
    assert report.bundle_path.is_file()
    assert report.manifest_path.is_file()
    assert report.captured_files > 0
    assert report.bundle_bytes > 0


def test_the_sidecar_records_a_digest_that_matches_the_bundle(captured):
    report, _scan = captured
    sidecar = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    expected = sidecar["bundle"]["ciphertext"]["sha256"]
    bundle_mod.verify_ciphertext(report.bundle_path, expected)  # must not raise


def test_a_damaged_bundle_fails_its_sidecar_check_before_a_passphrase_is_needed(captured):
    report, _scan = captured
    raw = bytearray(report.bundle_path.read_bytes())
    raw[-40] ^= 0x01
    report.bundle_path.write_bytes(bytes(raw))
    checked, error = restore_mod.verify_sidecar(report.bundle_path)
    assert checked and error and "damaged or incomplete" in error


def test_round_trip_restores_byte_identical_files(captured, profile: Path, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "restored"
    result = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok
    assert not result.digest_mismatches

    source = digests(profile)
    expected = {
        name: value
        for name, value in source.items()
        if "node_modules" not in name
        and not name.startswith(("AppData/", "OneDrive/"))
        and Path(name).name not in {"Thumbs.db", "~$report.docx"}
    }
    assert digests(destination) == expected


def test_restore_puts_folders_back_under_their_real_names(captured, tmp_path: Path):
    """The archive path carries 'Documents', not the internal id 'documents'."""
    report, _scan = captured
    destination = tmp_path / "restored"
    restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert (destination / "Documents" / "report.docx").is_file()
    assert (destination / "Desktop" / "shortcut.lnk").is_file()
    assert (destination / "Projects" / "app" / "main.rs").is_file()


def test_a_wrong_passphrase_is_refused(captured, tmp_path: Path):
    report, _scan = captured
    with pytest.raises(Exception) as excinfo:
        restore_mod.restore(
            RestoreOptions(
                bundle=report.bundle_path, passphrase="wrong", destination=tmp_path / "r"
            )
        )
    assert "passphrase" in str(excinfo.value).lower()


def test_a_dry_run_writes_nothing(captured, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "dry"
    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            dry_run=True,
        )
    )
    assert result.restored_files > 0
    assert not destination.exists() or not any(destination.rglob("*"))


def test_an_interrupted_restore_resumes_instead_of_redoing_the_work(captured, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "restored"
    first = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    second = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert first.restored_files > 0
    assert second.restored_files == 0
    assert second.skipped_existing == first.restored_files


def test_an_existing_differing_file_is_kept_unless_overwrite_is_asked_for(captured, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "restored"
    target = destination / "Documents" / "report.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"my newer local edit")

    kept = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert kept.kept_existing == 1
    assert target.read_bytes() == b"my newer local edit"

    replaced = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            overwrite=True,
        )
    )
    assert replaced.kept_existing == 0
    assert target.read_bytes() != b"my newer local edit"


def test_restore_reproduces_the_followups_from_the_manifest(captured, tmp_path: Path):
    report, scan = captured
    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path, passphrase=PASSPHRASE, destination=tmp_path / "restored"
        )
    )
    assert [f.title for f in result.followups] == [f.title for f in scan.followups]
    assert result.followups[0].steps


def test_a_single_item_can_be_restored_on_its_own(captured, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "one"
    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            items=("files:desktop",),
        )
    )
    assert result.restored_files == 1
    assert (destination / "Desktop" / "shortcut.lnk").is_file()
    assert not (destination / "Documents").exists()


def test_a_single_file_item_can_be_restored_on_its_own(
    profile: Path, env: Environment, tmp_path: Path
):
    """Selecting by id matched ``archive_path + "/"``, which is how a tree's
    members are named -- and how a *file's* member is not. Every single-file
    item was therefore dropped from any restore that selected anything, the
    window's included, since it always names what it is putting back. The one
    that hurt was the password export the user had just gone through their
    browser to produce: listed, ticked, and silently not written.
    """
    from winmigrate import passwords as passwords_mod

    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    export = tmp_path / "chrome.csv"
    export.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")
    target = passwords_mod.ExportTarget(
        browser_key="chrome",
        title="Google Chrome",
        engine="chromium",
        export_page="chrome://password-manager/settings",
    )
    staged = passwords_mod.ingest_csv(target, export, scan)
    assert staged.ok

    output = tmp_path / "out" / "with-passwords.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    destination = tmp_path / "one"
    result = restore_mod.restore(
        RestoreOptions(
            bundle=output,
            passphrase=PASSPHRASE,
            destination=destination,
            items=(staged.item.id,),
        )
    )

    written = destination / "WinMigrate-Passwords" / "chrome-passwords.csv"
    assert result.restored_files == 1
    assert written.is_file()
    assert "https://x" in written.read_text(encoding="utf-8")
    # Selecting one item still means one item.
    assert not (destination / "Documents").exists()


def test_selecting_an_unknown_item_is_an_error(captured, tmp_path: Path):
    report, _scan = captured
    with pytest.raises(RestoreError, match="no such item"):
        restore_mod.restore(
            RestoreOptions(
                bundle=report.bundle_path,
                passphrase=PASSPHRASE,
                destination=tmp_path / "x",
                items=("files:nonsense",),
            )
        )


def test_the_space_pre_check_refuses_before_writing_anything(profile: Path, env: Environment, tmp_path: Path, monkeypatch):
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    monkeypatch.setattr(capture_mod.shutil, "disk_usage", lambda path: _tiny())
    output = tmp_path / "out.dat"
    with pytest.raises(CaptureError, match="not enough free space"):
        capture_mod.capture(
            scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
        )
    assert not output.exists()


class _tiny:
    """A volume with essentially nothing free."""

    total = 1000
    used = 999
    free = 1


def test_the_space_pre_check_can_be_overridden(profile: Path, env: Environment, tmp_path: Path, monkeypatch):
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    monkeypatch.setattr(capture_mod.shutil, "disk_usage", lambda path: _tiny())
    report = capture_mod.capture(
        scan,
        CaptureOptions(
            output=tmp_path / "out.dat",
            passphrase=PASSPHRASE,
            use_vss=False,
            skip_space_check=True,
        ),
        config,
        env,
    )
    assert report.bundle_path.is_file()


def test_a_failed_capture_leaves_no_bundle_behind(profile: Path, env: Environment, tmp_path: Path, monkeypatch):
    """A half-written bundle looks restorable, which is worse than none."""
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    output = tmp_path / "broken.dat"

    original = bundle_mod.BundleWriter.add_file

    def explode(self, source, archive_name):
        raise RuntimeError("disk went away")

    monkeypatch.setattr(bundle_mod.BundleWriter, "add_file", explode)
    with pytest.raises(RuntimeError):
        capture_mod.capture(
            scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
        )
    assert not output.exists()
    monkeypatch.setattr(bundle_mod.BundleWriter, "add_file", original)


def test_an_unreadable_file_is_recorded_without_losing_the_capture(profile: Path, env: Environment, tmp_path: Path, monkeypatch):
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    original = bundle_mod.BundleWriter.add_file

    def fail_one(self, source, archive_name):
        if archive_name.endswith("report.docx"):
            raise OSError(13, "Permission denied")
        return original(self, source, archive_name)

    monkeypatch.setattr(bundle_mod.BundleWriter, "add_file", fail_one)
    report = capture_mod.capture(
        scan,
        CaptureOptions(output=tmp_path / "partial.dat", passphrase=PASSPHRASE, use_vss=False),
        config,
        env,
    )
    assert len(report.failures) == 1
    assert "Permission denied" in report.failures[0][1]
    assert report.captured_files > 0
    assert report.bundle_path.is_file()


def test_a_dry_run_does_not_create_the_destination_directory(captured, tmp_path: Path):
    """Reported as a bug when C:\\temp did not appear after a --dry-run restore.

    The behaviour was right; the report claimed "Restore complete", which is
    what made it look wrong.
    """
    report, _scan = captured
    destination = tmp_path / "does-not-exist-yet"
    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            dry_run=True,
        )
    )
    assert not destination.exists()
    assert result.dry_run is True
    assert result.destination == destination


def test_the_dry_run_report_says_nothing_was_written(captured, tmp_path: Path):
    from rich.console import Console

    from winmigrate import report as report_mod

    capture_report, _scan = captured
    destination = tmp_path / "nowhere"
    result = restore_mod.restore(
        RestoreOptions(
            bundle=capture_report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            dry_run=True,
        )
    )
    console = Console(record=True, width=120)
    report_mod.render_restore_report(result, console)
    # Collapsed, because rich wraps to the console width and a long temp path
    # pushes the sentence over a line break -- which is a fact about the
    # terminal, not about what the report says.
    text = " ".join(console.export_text().split())
    assert "Dry run" in text and "nothing was written" in text.lower()
    assert "Restore complete" not in text
    assert "was not created" in text
    assert str(destination) in text


def test_a_real_restore_still_reports_as_complete(captured, tmp_path: Path):
    from rich.console import Console

    from winmigrate import report as report_mod

    capture_report, _scan = captured
    result = restore_mod.restore(
        RestoreOptions(
            bundle=capture_report.bundle_path,
            passphrase=PASSPHRASE,
            destination=tmp_path / "real",
        )
    )
    console = Console(record=True, width=120)
    report_mod.render_restore_report(result, console)
    text = console.export_text()
    assert "Restore complete" in text
    assert "nothing was written" not in text.lower()


def test_a_resumed_restore_does_not_report_corruption_that_is_not_there(captured, tmp_path: Path):
    """The digest check must cover the item's whole file set, not just this run.

    A resume skips files already in place; comparing that partial set against a
    whole-tree digest reported a mismatch -- and told the user not to delete the
    source machine -- on a perfectly good restore.
    """
    report, _scan = captured
    destination = tmp_path / "restored"
    first = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert first.ok and not first.digest_mismatches

    # Interrupted: one file went missing, the rest are already in place.
    (destination / "Documents" / "report.docx").unlink()
    resumed = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert resumed.restored_files == 1
    assert resumed.skipped_existing > 0
    assert resumed.digest_mismatches == []
    assert resumed.ok


def test_a_file_of_the_same_size_is_not_assumed_to_be_the_same_file(captured, tmp_path: Path):
    """Two files of one length are not one file.

    Deciding "already restored" on the size alone meant a note edited on the new
    machine without changing its length was never written -- not even with
    --overwrite -- and was then reported as a digest mismatch, which accuses the
    backup of corruption for a file that never left it. A file that differs is
    the user's, kept and reported as kept, exactly as it already was when the
    sizes happened to differ.
    """
    report, _scan = captured
    destination = tmp_path / "restored"
    restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    target = destination / "Documents" / "notes.txt"
    original = target.read_bytes()
    target.write_bytes(b"X" * len(original))  # same size, different content

    kept = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert kept.kept_existing == 1
    assert target.read_bytes() == b"X" * len(original)
    # Not a mismatch: the bundle is fine, this file is simply not from it.
    assert kept.digest_mismatches == []
    assert any("notes.txt" in note.message for note in kept.notes)

    replaced = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            overwrite=True,
        )
    )
    assert target.read_bytes() == original
    assert replaced.digest_mismatches == [] and replaced.ok


def test_a_file_the_user_kept_is_partially_verified_not_a_mismatch(captured, tmp_path: Path):
    report, _scan = captured
    destination = tmp_path / "restored"
    restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    # A different size, so it is "differs" and kept rather than skipped.
    (destination / "Documents" / "notes.txt").write_bytes(b"my own much longer edit here")

    kept = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert kept.kept_existing == 1
    assert kept.digest_mismatches == []
    assert any("partially verified" in note.message for note in kept.notes)


def test_the_bundle_is_left_out_of_its_own_capture(tmp_path: Path):
    """Writing the bundle onto the Desktop is the normal habit, and the Desktop
    is captured.

    Without this the walk reaches the bundle while it is still being written and
    copies it into itself. tarfile is told a size from the stat and the file
    keeps growing, so what lands inside is a truncated, unopenable copy -- which
    then restores onto the new machine as a file called backup.dat, looking
    exactly like a real backup for as long as it takes someone to try it.
    """
    profile = tmp_path / "alice"
    (profile / "Desktop").mkdir(parents=True)
    (profile / "Desktop" / "note.txt").write_text("real user data", encoding="utf-8")
    # An unrelated file that merely shares the sidecar's suffix must still travel.
    (profile / "Desktop" / "notes.manifest.json").write_text('{"mine": true}', encoding="utf-8")

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    output = profile / "Desktop" / "backup.dat"
    report = capture_mod.capture(
        scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    assert not report.failures
    assert any("inside the profile" in (note.message or "") for note in report.notes)

    destination = tmp_path / "restored"
    result = restore_mod.restore(
        RestoreOptions(bundle=output, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok and not result.digest_mismatches
    names = {f.name for f in destination.rglob("*") if f.is_file()}
    assert names == {"note.txt", "notes.manifest.json"}


def test_recapturing_over_last_weeks_bundle_does_not_swallow_it(tmp_path: Path):
    """The second run is the more likely one: the old bundle is already there
    when the scan measures the folder, so it is in the plan. Capture has to drop
    it without leaving the item's digest tree disagreeing with what was written
    -- otherwise the restore reports corruption that is not there."""
    profile = tmp_path / "alice"
    (profile / "Desktop").mkdir(parents=True)
    (profile / "Desktop" / "note.txt").write_text("real user data", encoding="utf-8")
    output = profile / "Desktop" / "backup.dat"
    output.write_bytes(b"LAST-WEEKS-BUNDLE" * 100)
    (profile / "Desktop" / "backup.manifest.json").write_text('{"old": true}', encoding="utf-8")

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    assert scan.totals().capture_files == 3  # the plan does count the old pair

    capture_mod.capture(
        scan,
        # Replacing last week's bundle is the point of this run, and saying so
        # is now required: without it capture refuses rather than destroying a
        # backup because a name was reused.
        CaptureOptions(
            output=output, passphrase=PASSPHRASE, use_vss=False, overwrite=True
        ),
        config,
        env,
    )
    destination = tmp_path / "restored"
    result = restore_mod.restore(
        RestoreOptions(bundle=output, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok and not result.digest_mismatches
    assert {f.name for f in destination.rglob("*") if f.is_file()} == {"note.txt"}


def test_a_file_the_snapshot_predates_is_read_from_the_live_volume(tmp_path: Path):
    """The walk enumerates the live volume; the reads come from the snapshot.

    Those are not the same filesystem. A file created after the snapshot was
    taken is listed by the walk and is simply not in the snapshot -- which is
    not an error, it is Brave compacting its LevelDB while the capture runs.
    Reported as "could not capture: the system cannot find the file specified",
    it reads as data loss; read from the live volume instead, it is just a file.
    """
    profile = tmp_path / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "Documents" / "steady.txt").write_text("was there all along", encoding="utf-8")
    (profile / "Documents" / "new.txt").write_text("created after the snapshot", encoding="utf-8")

    # A snapshot that has the profile but not the newer file, exactly as a
    # real one taken moments earlier would be.
    snapshot = tmp_path / "snap"
    (snapshot / "Documents").mkdir(parents=True)
    (snapshot / "Documents" / "steady.txt").write_text("was there all along", encoding="utf-8")

    class Snapshot:
        def map(self, path):
            return str(snapshot / Path(path).relative_to(profile))

        def remove(self):
            pass

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    with bundle_mod.BundleWriter(bundle, PASSPHRASE, _header()) as writer:
        report = capture_mod.CaptureReport(
            bundle_path=bundle, manifest_path=tmp_path / "b.manifest.json"
        )
        for item in scan.items:
            if item.archive_path and item.source_path:
                capture_mod._capture_item(
                    item, writer, scan, config, env, Snapshot(), report, None
                )
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b'{"items": []}')

    assert not report.failures, report.failures
    assert not report.vanished
    assert report.captured_files == 2  # both, the newer one via the live volume


def test_a_file_that_is_in_neither_is_reported_as_vanished_not_as_a_failure(tmp_path: Path):
    """Created and deleted inside the capture window. Normal for a database's
    scratch files, and reporting it as "could not capture" trains people to
    skim past the list that does matter."""
    profile = tmp_path / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "Documents" / "022352.log").write_text("leveldb scratch", encoding="utf-8")

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)

    # The window that matters is between the walk yielding the path and the read
    # opening it -- too narrow to hit by deleting the file here, since the
    # capture re-walks and would simply never list it.
    real_add = bundle_mod.BundleWriter.add_file

    def vanish(self, source, archive_name):
        if str(source).endswith("022352.log"):
            raise FileNotFoundError(2, "The system cannot find the file specified")
        return real_add(self, source, archive_name)

    bundle = tmp_path / "b.dat"
    with bundle_mod.BundleWriter(bundle, PASSPHRASE, _header()) as writer:
        report = capture_mod.CaptureReport(
            bundle_path=bundle, manifest_path=tmp_path / "b.manifest.json"
        )
        bundle_mod.BundleWriter.add_file = vanish
        try:
            for item in scan.items:
                if item.archive_path and item.source_path:
                    capture_mod._capture_item(
                        item, writer, scan, config, env, None, report, None
                    )
        finally:
            bundle_mod.BundleWriter.add_file = real_add
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b'{"items": []}')

    assert not report.failures
    assert [Path(p).name for p in report.vanished] == ["022352.log"]


def test_a_locked_file_is_still_a_real_failure(tmp_path: Path):
    """The point of separating "vanished" is to keep the failure list worth
    reading, not to empty it. A permission error is not a vanished file."""
    profile = tmp_path / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "Documents" / "locked.bin").write_bytes(b"data")

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)

    real_add = bundle_mod.BundleWriter.add_file

    def refuse(self, source, archive_name):
        if str(source).endswith("locked.bin"):
            raise PermissionError(13, "Access is denied")
        return real_add(self, source, archive_name)

    bundle = tmp_path / "b.dat"
    with bundle_mod.BundleWriter(bundle, PASSPHRASE, _header()) as writer:
        report = capture_mod.CaptureReport(
            bundle_path=bundle, manifest_path=tmp_path / "b.manifest.json"
        )
        bundle_mod.BundleWriter.add_file = refuse
        try:
            for item in scan.items:
                if item.archive_path and item.source_path:
                    capture_mod._capture_item(
                        item, writer, scan, config, env, None, report, None
                    )
        finally:
            bundle_mod.BundleWriter.add_file = real_add
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b'{"items": []}')

    assert not report.vanished
    assert [Path(p).name for p, _ in report.failures] == ["locked.bin"]
    assert report.failures[0][1] == "Access is denied"


def _header() -> dict:
    from winmigrate import crypto

    return {
        "format": manifest_mod.BUNDLE_FORMAT_VERSION,
        "cipher": manifest_mod.CIPHER,
        "compression": "none",
        "kdf": crypto.kdf_params_to_json(crypto.default_kdf_params()),
    }


def test_capture_refuses_to_write_over_a_backup_unless_told_to(
    profile: Path, env: Environment, tmp_path: Path
):
    """The file it would destroy is a backup: the one kind of file whose whole
    purpose is being there when something else is not. Afterwards nothing says
    what was lost -- same name, same shape, none of the old data."""
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    output = tmp_path / "backup.dat"
    output.write_bytes(b"LAST WEEK'S BUNDLE")

    with pytest.raises(CaptureError, match="already there"):
        capture_mod.capture(
            scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
        )
    assert output.read_bytes() == b"LAST WEEK'S BUNDLE"

    capture_mod.capture(
        scan,
        CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False, overwrite=True),
        config,
        env,
    )
    assert output.read_bytes() != b"LAST WEEK'S BUNDLE"


def test_a_stray_sidecar_also_counts_as_a_backup_being_there(
    profile: Path, env: Environment, tmp_path: Path
):
    config = ScanConfig(profile_root=profile)
    scan = run_scan(config, env)
    output = tmp_path / "backup.dat"
    output.with_suffix(".manifest.json").write_text('{"old": true}', encoding="utf-8")

    with pytest.raises(CaptureError, match="already there"):
        capture_mod.capture(
            scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
        )


def test_a_restore_that_cannot_fit_is_refused_before_it_writes(
    captured, tmp_path: Path, monkeypatch
):
    """Capture has always checked for room and restore never did, which is the
    wrong way round: a capture that runs out of space wastes an hour, and a
    restore that runs out fills the disk of the machine someone is standing in
    front of -- usually the new one, mid-migration, with the old one wiped."""
    import shutil as _shutil
    from collections import namedtuple

    report, _scan = captured
    destination = tmp_path / "restored"
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda path: Usage(1_000, 999, 1))

    with pytest.raises(RestoreError, match="not enough free space"):
        restore_mod.restore(
            RestoreOptions(
                bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination
            )
        )
    assert not destination.exists() or not any(destination.rglob("*"))

    # And it is a check, not a rule: --no-space-check still goes ahead.
    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=destination,
            space_check=False,
        )
    )
    assert result.restored_files > 0


def test_a_practice_run_needs_no_room_at_all(captured, tmp_path: Path, monkeypatch):
    """It writes nothing, so a full disk is no reason to refuse to report."""
    import shutil as _shutil
    from collections import namedtuple

    report, _scan = captured
    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(_shutil, "disk_usage", lambda path: Usage(1_000, 999, 1))

    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=tmp_path / "dry",
            dry_run=True,
        )
    )
    assert result.restored_files > 0


def test_choosing_fewer_items_needs_less_room(captured, tmp_path: Path, monkeypatch):
    """The check counts what was selected, not the whole bundle -- otherwise
    "restore just my Desktop onto this small laptop" is refused for the size of
    everything the user did not ask for."""
    import shutil as _shutil
    from collections import namedtuple

    report, _scan = captured
    sidecar = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    desktop = next(
        item for item in sidecar["items"] if item.get("id") == "files:desktop"
    )
    Usage = namedtuple("Usage", "total used free")
    room = desktop["size_bytes"] + restore_mod.FREE_SPACE_MARGIN + 1
    monkeypatch.setattr(_shutil, "disk_usage", lambda path: Usage(room, 0, room))

    result = restore_mod.restore(
        RestoreOptions(
            bundle=report.bundle_path,
            passphrase=PASSPHRASE,
            destination=tmp_path / "one",
            items=("files:desktop",),
        )
    )
    assert result.restored_files == 1

    with pytest.raises(RestoreError, match="not enough free space"):
        restore_mod.restore(
            RestoreOptions(
                bundle=report.bundle_path, passphrase=PASSPHRASE, destination=tmp_path / "all"
            )
        )


def test_a_resumed_restore_still_skips_what_is_genuinely_there(captured, tmp_path: Path):
    """The reason the size check existed. Reading the file to be sure costs
    nothing overall -- a skipped file was hashed from disk at verification time
    anyway -- and now the digest is known, so it is not read twice."""
    report, _scan = captured
    destination = tmp_path / "restored"
    first = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    before = digests(destination)

    second = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )

    assert second.restored_files == 0
    assert second.skipped_existing == first.restored_files
    assert second.digest_mismatches == [] and second.ok
    # Nothing was rewritten, and no part-files were left behind.
    assert digests(destination) == before
    assert not list(destination.rglob("*.winmigrate-part"))


def test_a_failed_write_leaves_no_part_file_behind(captured, tmp_path: Path, monkeypatch):
    """Litter in someone's Documents folder, with a name that means nothing to
    them, left by the one run that also told them something went wrong."""
    report, _scan = captured
    destination = tmp_path / "restored"
    real_replace = restore_mod.os.replace

    def failing_replace(source, target, *args, **kwargs):
        if str(target).endswith("report.docx"):
            raise OSError(13, "Permission denied")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(restore_mod.os, "replace", failing_replace)

    result = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )

    assert any("report.docx" in path for path, _reason in result.failures)
    assert not list(destination.rglob("*.winmigrate-part"))


def test_restoring_one_item_does_not_re_apply_every_setting(
    profile: Path, env: Environment, tmp_path: Path, monkeypatch
):
    """"Put back just my Desktop" is a sentence about one folder. It used to
    add the printers and rewrite the environment variables too, because the
    records were read straight out of the manifest without asking what the
    restore had been asked for."""
    from winmigrate import apply as apply_mod
    from winmigrate.models import Category, Item, Kind

    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    scan.items.append(
        Item(
            id="settings:env_vars",
            category=Category.ENV_VARS,
            kind=Kind.RECORD,
            title="Environment variables",
            record={"variables": {"MY_TOOL_HOME": r"C:\tools"}},
        )
    )
    bundle = tmp_path / "with-records.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    applied: list[str] = []
    monkeypatch.setattr(
        apply_mod, "apply_environment", lambda record, **kw: applied.append("env") or []
    )

    restore_mod.restore(
        RestoreOptions(
            bundle=bundle,
            passphrase=PASSPHRASE,
            destination=tmp_path / "one",
            items=("files:desktop",),
        )
    )
    assert applied == []

    # Naming the record restores that one.
    restore_mod.restore(
        RestoreOptions(
            bundle=bundle,
            passphrase=PASSPHRASE,
            destination=tmp_path / "two",
            items=("files:desktop", "settings:env_vars"),
        )
    )
    assert applied == ["env"]

    # And a restore that named nothing still applies everything it has.
    applied.clear()
    restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=tmp_path / "all")
    )
    assert applied == ["env"]


def test_a_restore_that_re_applied_a_setting_stops_telling_you_to(
    profile: Path, env: Environment, tmp_path: Path, monkeypatch
):
    """End to end, because the taking-back only happens if the restore calls
    for it: the capture writes "Re-apply: Mapped network drives (1)" into the
    bundle, the restore maps the drive, and the report must then not hand that
    instruction to the user under a heading reading "These need you rather than
    the tool"."""
    from winmigrate import apply as apply_mod
    from winmigrate.apply import Outcome, Result
    from winmigrate.models import Category, Followup, Item, Kind, RestoreSpec, RestoreStrategy

    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    scan.items.append(
        Item(
            id="settings:mapped_drives",
            category=Category.MAPPED_DRIVES,
            kind=Kind.RECORD,
            title="Mapped network drives (1)",
            record={"drives": {"Z": r"\\server\share"}},
            record_public=True,
            restore=RestoreSpec(
                target="net use",
                strategy=RestoreStrategy.GUIDED,
                notes=["Reconnect each with: net use <letter>: <path>"],
            ),
        )
    )
    scan.followups.append(
        Followup(
            id="settings:mapped_drives:guided",
            title="Re-apply: Mapped network drives (1)",
            why="recorded in the bundle and re-applied by hand on the new machine",
            steps=["Reconnect each with: net use <letter>: <path>"],
        )
    )
    bundle = tmp_path / "drives.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    monkeypatch.setattr(
        apply_mod,
        "apply_mapped_drives",
        lambda record, **kw: [Result("drive", r"Z: \\server\share", Outcome.APPLIED)],
    )

    report = restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase=PASSPHRASE, destination=tmp_path / "done"
        )
    )

    assert [r.outcome for r in report.applied if r.kind == "drive"] == [Outcome.APPLIED]
    assert not [f for f in report.followups if f.id == "settings:mapped_drives:guided"]


def test_the_desktop_background_comes_back_as_a_background(
    profile: Path, env: Environment, tmp_path: Path
):
    """End to end: the picture has to survive the bundle *and* be pointed at.
    A file restored into a folder nobody looks in is not a background."""
    from winmigrate import apply as apply_mod
    from winmigrate.models import Category, Item, Kind

    picture = tmp_path / "lake.jpg"
    picture.write_bytes(b"\xff\xd8\xff" + b"0" * 4096)
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    scan.items.append(
        Item(
            id="settings:wallpaper",
            category=Category.WALLPAPER,
            kind=Kind.FILE,
            title="Desktop background (lake.jpg)",
            source_path=str(picture),
            archive_path="WinMigrate-Wallpaper/lake.jpg",
            record={"type": "picture", "style": "10", "tile": "0", "file_name": "lake.jpg"},
            record_public=True,
        )
    )
    bundle = tmp_path / "background.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    seen: list[tuple[dict, Path | None]] = []
    original = apply_mod.apply_wallpaper
    destination = tmp_path / "new"

    def watched(record, image, env=None):
        seen.append((record, image))
        return original(record, image, Environment.fixture(destination, {}))

    from unittest.mock import patch

    with patch.object(apply_mod, "apply_wallpaper", watched):
        report = restore_mod.restore(
            RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
        )

    restored = destination / "WinMigrate-Wallpaper" / "lake.jpg"
    assert restored.is_file()
    assert restored.read_bytes() == picture.read_bytes()
    # And the applier was handed that file, not the name of one.
    (record, image) = seen[0]
    assert record["style"] == "10"
    assert image == restored
    assert [r.name for r in report.applied if r.kind == "background"] == ["lake.jpg"]


def test_a_background_image_from_a_previous_migration_is_not_mistaken_for_this_one(
    tmp_path: Path
):
    """The name comes from the record, not from whatever is in the folder: a
    destination that already holds an older WinMigrate-Wallpaper would otherwise
    have the last migration's picture set as this one's."""
    destination = tmp_path / "new"
    (destination / "WinMigrate-Wallpaper").mkdir(parents=True)
    (destination / "WinMigrate-Wallpaper" / "older.jpg").write_bytes(b"\xff\xd8\xff")

    assert restore_mod._restored_wallpaper({"file_name": "lake.jpg"}, destination) is None
    assert restore_mod._restored_wallpaper({"file_name": "older.jpg"}, destination) == (
        destination / "WinMigrate-Wallpaper" / "older.jpg"
    )
    # And a name out of a bundle is data: it does not get to name a directory.
    assert restore_mod._restored_wallpaper({"file_name": "../../evil.jpg"}, destination) is None


def test_the_programs_holding_those_files_open_are_named(tmp_path: Path):
    """Restoring a browser profile into a running browser is the one way this
    tool can damage something: the browser holds those files open, rewrites
    them on its own schedule, and half its database arriving underneath it is
    worse than the restore failing outright."""
    manifest = {
        "items": [
            {"id": "browser:chrome:default", "category": "browser_profile",
             "title": "Google Chrome — Person 1", "action": "capture"},
            {"id": "browser:passwords-csv:brave", "category": "browser_passwords",
             "title": "Brave — exported passwords (CSV)", "action": "capture"},
            {"id": "settings:outlook_pst:archive", "category": "outlook",
             "title": "Outlook data file", "action": "capture"},
            {"id": "files:documents", "category": "user_files",
             "title": "Documents", "action": "capture"},
            {"id": "browser:firefox:x", "category": "browser_profile",
             "title": "Mozilla Firefox — default", "action": "skip"},
        ]
    }

    assert restore_mod.programs_to_close(manifest) == ["Brave", "Google Chrome", "Outlook"]

    # Only what is being put back: naming items narrows it.
    assert restore_mod.programs_to_close(manifest, ("files:documents",)) == []
    assert restore_mod.programs_to_close(manifest, ("browser:chrome:default",)) == [
        "Google Chrome"
    ]


def test_a_restore_of_files_only_asks_nobody_to_close_anything(captured, tmp_path: Path):
    """The fixture profile has no browser, so the page must stay quiet rather
    than warn about programs that are not in the backup."""
    report, _scan = captured
    sidecar = json.loads(report.manifest_path.read_text(encoding="utf-8"))
    assert restore_mod.programs_to_close(sidecar) == []


def test_restoring_everything_does_not_decrypt_the_bundle_twice(
    profile: Path, env: Environment, tmp_path: Path, monkeypatch
):
    """The window always names every item it restores -- that is how it puts
    records back alongside files -- so "everything" arrives as a selection. A
    bundle holding any encrypted-only item then sent that through the manifest
    pre-read: a second full decrypt of the whole thing, before a byte is
    written, with no progress against it. On a large backup that is minutes of
    a window that looks like it has hung, in the common case rather than a
    corner of one."""
    from winmigrate.models import Category, Item, Kind, Sensitivity

    secret = tmp_path / "sign-ins.crd"
    secret.write_bytes(b"\\x01\\x02encrypted")
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    scan.items.append(
        Item(
            id="credentials:backup",
            category=Category.CREDENTIALS,
            kind=Kind.FILE,
            title="Saved Windows sign-ins",
            source_path=str(secret),
            archive_path="secrets/WinMigrate-Credentials/sign-ins.crd",
            sensitivity=Sensitivity.SECRET,
        )
    )
    bundle = tmp_path / "with-secret.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    reads: list[str] = []
    original = restore_mod.read_manifest
    monkeypatch.setattr(
        restore_mod, "read_manifest",
        lambda path, phrase: reads.append("pre-read") or original(path, phrase),
    )

    every_id = tuple(item.id for item in scan.items)
    report = restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase=PASSPHRASE,
            destination=tmp_path / "all", items=every_id,
        )
    )

    assert reads == []
    # And the encrypted-only file still lands, which is the thing the pre-read
    # existed to make possible.
    assert (tmp_path / "all" / "WinMigrate-Credentials" / "sign-ins.crd").is_file()
    assert report.ok


def test_restoring_one_secret_item_still_reads_the_manifest_for_it(
    profile: Path, env: Environment, tmp_path: Path, monkeypatch
):
    """The pre-read is not gone, it is no longer the default: a genuine
    selection of a redacted item has nowhere else to learn its path from."""
    from winmigrate.models import Category, Item, Kind, Sensitivity

    secret = tmp_path / "sign-ins.crd"
    secret.write_bytes(b"\\x01\\x02encrypted")
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)
    scan.items.append(
        Item(
            id="credentials:backup",
            category=Category.CREDENTIALS,
            kind=Kind.FILE,
            title="Saved Windows sign-ins",
            source_path=str(secret),
            archive_path="secrets/WinMigrate-Credentials/sign-ins.crd",
            sensitivity=Sensitivity.SECRET,
        )
    )
    bundle = tmp_path / "one-secret.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    reads: list[str] = []
    original = restore_mod.read_manifest
    monkeypatch.setattr(
        restore_mod, "read_manifest",
        lambda path, phrase: reads.append("pre-read") or original(path, phrase),
    )

    restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase=PASSPHRASE,
            destination=tmp_path / "just-one", items=("credentials:backup",),
        )
    )

    assert reads == ["pre-read"]
    assert (tmp_path / "just-one" / "WinMigrate-Credentials" / "sign-ins.crd").is_file()
