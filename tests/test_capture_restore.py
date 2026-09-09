"""End-to-end capture and restore, plus the failure modes that matter."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from winmigrate import bundle as bundle_mod
from winmigrate import capture as capture_mod
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
    sidecar = json.loads(report.manifest_path.read_text())
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
    text = console.export_text()
    assert "Dry run" in text and "nothing was written" in text.lower()
    assert "Restore complete" not in text
    assert "not created" in text
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
