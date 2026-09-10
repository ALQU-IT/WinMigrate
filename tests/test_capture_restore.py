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


def test_a_file_corrupted_on_disk_is_caught_even_when_it_is_skipped(captured, tmp_path: Path):
    """Skipping by size alone used to mean a same-size corruption was invisible."""
    report, _scan = captured
    destination = tmp_path / "restored"
    restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    target = destination / "Documents" / "notes.txt"
    original = target.read_bytes()
    target.write_bytes(b"X" * len(original))  # same size, different content

    checked = restore_mod.restore(
        RestoreOptions(bundle=report.bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert "files:documents" in checked.digest_mismatches
    assert not checked.ok


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
        scan, CaptureOptions(output=output, passphrase=PASSPHRASE, use_vss=False), config, env
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
