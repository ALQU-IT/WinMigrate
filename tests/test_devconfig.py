"""Developer and credential config: the encrypted-only material.

These tests assert the property that matters most for this module -- that
secret material never reaches the plaintext sidecar or the log -- alongside the
ordinary capture/restore behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path


from winmigrate import capture as capture_mod
from winmigrate import manifest as manifest_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.models import Action, Category, Sensitivity, SkipReason
from winmigrate.platform_win import Environment
from winmigrate.restore import RestoreOptions
from winmigrate.scan import run_scan
from winmigrate.scan import devconfig

PASSPHRASE = "correct horse battery staple"


def make_profile(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    for rel, data in {
        ".ssh/id_rsa": b"PRIVATE KEY",
        ".ssh/known_hosts": b"host key",
        ".aws/credentials": b"[default]\naws_secret_access_key=SHHH",
        ".gitconfig": b"[user]\n name = Alice",
        ".npmrc": b"//registry/:_authToken=TOK",
    }.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    return root


def dev_items(root: Path, **overrides):
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False, **overrides)
    return run_scan(config, env)


def test_credential_locations_are_found_and_marked_secret(tmp_path: Path):
    result = dev_items(make_profile(tmp_path))
    by_id = {item.id: item for item in result.items if item.category is Category.DEV_CONFIG}
    assert by_id["dev:ssh"].sensitivity is Sensitivity.SECRET
    assert by_id["dev:aws"].sensitivity is Sensitivity.SECRET
    assert by_id["dev:npmrc"].sensitivity is Sensitivity.SECRET
    # .gitconfig is preferences, not a credential.
    assert by_id["dev:gitconfig"].sensitivity is Sensitivity.NORMAL
    assert result.totals().secret_item_count >= 3


def test_absent_locations_are_simply_not_listed(tmp_path: Path):
    root = tmp_path / "empty"
    (root / "Documents").mkdir(parents=True)
    result = dev_items(root)
    assert not [i for i in result.items if i.category is Category.DEV_CONFIG]


def test_secret_paths_and_contents_never_reach_the_public_sidecar(tmp_path: Path):
    result = dev_items(make_profile(tmp_path))
    public = manifest_mod.public_view(manifest_mod.build(result))
    blob = json.dumps(public)
    # Item titles are intentionally public ("AWS credentials and config"), so a
    # plain English word is not a leak. Actual secret values, filenames and the
    # dot-paths they live at must never appear.
    for needle in ("SHHH", "aws_secret_access_key", "id_rsa", "TOK", ".ssh/", ".aws/"):
        assert needle not in blob, f"sidecar leaked {needle!r}"
    ssh = next(i for i in public["items"] if i["id"] == "dev:ssh")
    assert ssh["redacted"] is True
    assert "source_path" not in ssh and "archive_path" not in ssh


def test_files_only_mode_drops_the_secrets_but_still_reports_them(tmp_path: Path):
    result = dev_items(make_profile(tmp_path), files_only=True)
    ssh = next(i for i in result.items if i.id == "dev:ssh")
    assert ssh.action is Action.SKIP
    assert ssh.skip_reason is SkipReason.FILES_ONLY_MODE
    # A non-secret preference file is still captured.
    gitconfig = next(i for i in result.items if i.id == "dev:gitconfig")
    assert gitconfig.action is Action.CAPTURE
    assert any("files-only" in note.message for note in result.notes)


def test_capturing_secrets_produces_a_followup(tmp_path: Path):
    result = dev_items(make_profile(tmp_path))
    followup = next(f for f in result.followups if f.id == "dev:secrets")
    assert "credential" in followup.why.lower()
    assert any("rotate" in step.lower() for step in followup.steps)


def test_a_single_dotfile_round_trips_byte_identical(tmp_path: Path):
    root = make_profile(tmp_path)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env)

    destination = tmp_path / "restored"
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    assert report.ok
    assert not report.digest_mismatches
    assert (destination / ".gitconfig").read_bytes() == b"[user]\n name = Alice"
    assert (destination / ".ssh" / "id_rsa").read_bytes() == b"PRIVATE KEY"
    assert (destination / ".aws" / "credentials").read_bytes() == b"[default]\naws_secret_access_key=SHHH"


def test_the_bundle_sidecar_written_by_capture_does_not_name_secret_files(tmp_path: Path):
    root = make_profile(tmp_path)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env)
    sidecar = bundle.with_suffix(".manifest.json").read_text(encoding="utf-8")
    # As above: titles are public; secret values, filenames and paths are not.
    for needle in (".ssh/", ".aws/", "id_rsa", "SHHH", "aws_secret_access_key"):
        assert needle not in sidecar


def test_wsl_distributions_are_recorded_for_reporting_only(tmp_path: Path):
    root = make_profile(tmp_path)
    env = Environment.fixture(
        root,
        {
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss\{a}": {
                "DistributionName": "Ubuntu-24.04"
            },
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Lxss\{b}": {
                "DistributionName": "Debian"
            },
        },
    )
    result = run_scan(ScanConfig(profile_root=root, include_software=False), env)
    wsl = next(i for i in result.items if i.id == "dev:wsl")
    assert wsl.record["distributions"] == ["Debian", "Ubuntu-24.04"]
    assert wsl.action is Action.CAPTURE  # record item, nothing copied


def test_dev_targets_have_unique_slots_and_archive_paths():
    slots = [target.slot for target in devconfig.DEV_TARGETS]
    assert len(slots) == len(set(slots))
    relatives = [target.relative for target in devconfig.DEV_TARGETS]
    assert len(relatives) == len(set(relatives))


def test_a_secret_item_can_be_restored_on_its_own(tmp_path: Path):
    """The sidecar redacts secret items, so their archive path is only in the
    encrypted manifest; selecting one by id must still work."""
    root = make_profile(tmp_path)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    destination = tmp_path / "only-ssh"
    report = restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase=PASSPHRASE, destination=destination, items=("dev:ssh",)
        )
    )
    assert report.restored_files == 2
    assert (destination / ".ssh" / "id_rsa").read_bytes() == b"PRIVATE KEY"
    assert not (destination / ".gitconfig").exists()


def test_selecting_a_secret_item_works_without_a_sidecar(tmp_path: Path):
    root = make_profile(tmp_path)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )
    bundle.with_suffix(".manifest.json").unlink()

    report = restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase=PASSPHRASE, destination=tmp_path / "d", items=("dev:ssh",)
        )
    )
    assert report.restored_files == 2


def test_an_unknown_item_id_is_still_rejected(tmp_path: Path):
    import pytest

    from winmigrate.restore import RestoreError

    root = make_profile(tmp_path)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )
    with pytest.raises(RestoreError, match="no such item"):
        restore_mod.restore(
            RestoreOptions(
                bundle=bundle, passphrase=PASSPHRASE, destination=tmp_path / "d", items=("nope",)
            )
        )
