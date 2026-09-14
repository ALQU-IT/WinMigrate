"""Reading a bundle back, to earn the right to wipe the machine it came from.

``inspect`` checks the ciphertext digest, which catches a transfer that went
wrong. That is not the question someone asks before reformatting a laptop. They
want to know the bundle opens, that everything the manifest describes is really
in it, and that each file still hashes to what it hashed to when it was read off
the disk about to be erased.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from winmigrate import bundle as bundle_mod
from winmigrate import capture as capture_mod
from winmigrate import crypto
from winmigrate import manifest as manifest_mod
from winmigrate import verify as verify_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.crypto import DecryptionError
from winmigrate.errors import IntegrityError
from winmigrate.platform_win import Environment
from winmigrate.scan import run_scan
from winmigrate.util.hashing import tree_digest

PASSPHRASE = "correct horse battery staple"

BASE_MANIFEST = {
    "schema_version": manifest_mod.SCHEMA_VERSION,
    "tool": {"name": "winmigrate", "version": "0.1.0"},
    "created_utc": "2026-09-14T00:00:00Z",
    "source": {"hostname": "h", "username": "u", "profile_path": "/p"},
    "totals": {},
    "followups": [],
    "notes": [],
}


def manifest_item(**overrides) -> dict:
    return {
        "category": "user_files",
        "title": "t",
        "action": "capture",
        "sensitivity": "normal",
        **overrides,
    }


def handmade_bundle(path: Path, items: list[dict], members: list[tuple[str, bytes]]) -> Path:
    """A bundle whose manifest says whatever the test needs it to say.

    Built by hand because the point is to disagree with the archive, and a
    capture cannot be made to lie about its own contents.
    """
    header = {
        "format": manifest_mod.BUNDLE_FORMAT_VERSION,
        "cipher": manifest_mod.CIPHER,
        "compression": "none",
        "kdf": crypto.kdf_params_to_json(crypto.default_kdf_params()),
    }
    with bundle_mod.BundleWriter(path, PASSPHRASE, header) as writer:
        for name, data in members:
            writer.add_bytes(name, data)
        writer.add_bytes(
            manifest_mod.MANIFEST_ARCHIVE_NAME,
            json.dumps({**BASE_MANIFEST, "items": items}).encode("utf-8"),
        )
    return path


DOCUMENT = [("data/user_files/Documents/a.txt", b"hello")]
HONEST_DIGEST = tree_digest([("a.txt", hashlib.sha256(b"hello").hexdigest())])


def test_an_honest_bundle_passes(tmp_path: Path):
    """First, because a check that fails everything is not a check. Every other
    test here is only meaningful if this one holds."""
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="files:documents", kind="tree",
                       archive_path="data/user_files/Documents", digest=HONEST_DIGEST)],
        DOCUMENT,
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert report.ok
    assert report.items_checked == 1
    assert report.files_checked == 1
    assert report.bytes_checked == 5


def test_a_tree_whose_contents_no_longer_match_is_caught(tmp_path: Path):
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="files:documents", kind="tree",
                       archive_path="data/user_files/Documents", digest="0" * 64)],
        DOCUMENT,
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert not report.ok
    assert report.mismatches == ["files:documents"]


def test_a_single_file_whose_contents_no_longer_match_is_caught(tmp_path: Path):
    """A single-file item's archive name is its path exactly, with no trailing
    separator. Prefix matching would never find it, and the item would be
    silently passed -- which is how a whole class of items once went unchecked
    in the restore's own verification."""
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="dev:gitconfig", kind="file",
                       archive_path="secrets/.gitconfig", digest="0" * 64)],
        [("secrets/.gitconfig", b"[user]")],
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert report.mismatches == ["dev:gitconfig"]


def test_an_item_the_archive_does_not_contain_is_caught(tmp_path: Path):
    """Different from a mismatch and worth saying differently: the files are not
    there at all, which a digest comparison alone would never notice."""
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="files:pictures", kind="tree",
                       archive_path="data/user_files/Pictures", digest="a" * 64)],
        DOCUMENT,
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert not report.ok
    assert report.missing == ["files:pictures"]
    assert not report.mismatches


def test_an_item_with_no_recorded_digest_is_reported_rather_than_passed(tmp_path: Path):
    """Nothing to compare against is not the same as compared and fine."""
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="files:documents", kind="tree",
                       archive_path="data/user_files/Documents")],
        DOCUMENT,
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert report.ok  # not a failure
    assert report.unchecked == ["files:documents"]
    assert report.items_checked == 0


def test_records_carry_nothing_to_check_and_are_not_complained_about(tmp_path: Path):
    """Printers and the software inventory live in the manifest itself. They
    have no archive path, so there is nothing to verify and nothing wrong."""
    bundle = handmade_bundle(
        tmp_path / "b.dat",
        [manifest_item(id="settings:printers", kind="record", category="printers")],
        DOCUMENT,
    )
    report = verify_mod.verify(bundle, PASSPHRASE)
    assert report.ok and not report.unchecked


# --- the three ways a bundle can be unreadable ------------------------------
@pytest.fixture
def real_bundle(tmp_path: Path) -> Path:
    profile = tmp_path / "alice"
    for folder in ("Documents", "Pictures"):
        (profile / folder).mkdir(parents=True)
        for index in range(3):
            (profile / folder / f"f{index}.bin").write_bytes(os.urandom(2000))
    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    bundle = tmp_path / "real.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False),
        config,
        env,
    )
    return bundle


def test_a_real_capture_verifies(real_bundle: Path):
    report = verify_mod.verify(real_bundle, PASSPHRASE)
    assert report.ok
    assert report.files_checked == 6
    assert report.sidecar_verified is True


def test_the_wrong_passphrase_is_not_a_finding_but_a_refusal(real_bundle: Path):
    """There is nothing to verify, so it raises rather than returning a report
    that says everything is fine except it could not look."""
    with pytest.raises(DecryptionError):
        verify_mod.verify(real_bundle, "not the passphrase")


def test_a_flipped_byte_is_caught_before_the_passphrase_is_even_used(
    real_bundle: Path, tmp_path: Path
):
    damaged = tmp_path / "damaged.dat"
    damaged.write_bytes(real_bundle.read_bytes())
    (tmp_path / "damaged.manifest.json").write_text(
        real_bundle.with_suffix(".manifest.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    data = bytearray(damaged.read_bytes())
    data[len(data) // 2] ^= 0xFF
    damaged.write_bytes(bytes(data))

    with pytest.raises(IntegrityError):
        verify_mod.verify(damaged, PASSPHRASE)


def test_a_truncated_bundle_is_caught(real_bundle: Path, tmp_path: Path):
    """The failure every size check and every "the file is there" check calls
    fine."""
    short = tmp_path / "short.dat"
    short.write_bytes(real_bundle.read_bytes()[:-500])
    with pytest.raises(IntegrityError):
        verify_mod.verify(short, PASSPHRASE)


def test_verification_writes_nothing(real_bundle: Path, tmp_path: Path):
    before = {p for p in tmp_path.rglob("*")}
    verify_mod.verify(real_bundle, PASSPHRASE)
    assert {p for p in tmp_path.rglob("*")} == before
