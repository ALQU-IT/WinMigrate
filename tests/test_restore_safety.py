"""A bundle is data, not a trusted instruction.

Someone can hand a user a bundle. Restore must therefore refuse to write outside
the destination no matter what member names the archive contains, and must say
so when the files it wrote do not match the digests the manifest recorded.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate import bundle as bundle_mod
from winmigrate import crypto
from winmigrate import manifest as manifest_mod
from winmigrate import restore as restore_mod
from winmigrate.errors import IntegrityError
from winmigrate.restore import RestoreOptions, _target_for

PASSPHRASE = "pw"


@pytest.mark.parametrize(
    "archive_name",
    [
        "data/user_files/../../../evil.txt",
        "../evil.txt",
        "data/../../evil.txt",
        "C:/Windows/System32/evil.dll",
        "data/user_files/C:/evil.txt",
        # A drive letter that is not the first component. The check used to look
        # only at parts[0], saw an innocent "foo", and let this through.
        "data/foo/D:/evil.exe",
        "data/user_files/Documents/C:/evil.txt",
        "secrets/.ssh/D:/authorized_keys",
    ],
)
def test_paths_that_escape_the_destination_are_refused(archive_name, tmp_path: Path):
    assert _target_for(archive_name, tmp_path / "dest") is None


@pytest.mark.parametrize(
    "archive_name, expected",
    [
        ("/etc/passwd", "etc/passwd"),
        ("//server/share/x", "server/share/x"),
        ("data/./Documents/x", "Documents/x"),
    ],
)
def test_absolute_looking_members_are_neutralised_rather_than_refused(
    archive_name, expected, tmp_path: Path
):
    """A leading separator is meaningless once the member is treated as
    profile-relative, so it is dropped and the file lands inside the
    destination. Nothing escapes, and nothing is needlessly discarded."""
    destination = tmp_path / "dest"
    assert _target_for(archive_name, destination) == destination.joinpath(*expected.split("/"))


def test_a_drive_letter_after_the_first_component_really_does_escape_on_windows():
    r"""Why the test above cannot be satisfied by "it stayed under dest".

    Restore runs on Windows; the tests run on POSIX, where joining
    ``/dest`` with ``foo``, ``D:``, ``evil.exe`` gives the harmless
    ``/dest/foo/D:/evil.exe``. Windows joining treats a drive anywhere in the
    sequence as a fresh start, so the same member lands on another disk
    entirely -- which is exactly the case a POSIX assertion of "is it under the
    destination?" cannot see. Pinning the Windows semantics here means the
    escape is caught on the host the suite actually runs on.
    """
    from pathlib import PureWindowsPath

    assert str(PureWindowsPath(r"C:\dest").joinpath("foo", "D:", "evil.exe")) == r"D:evil.exe"


@pytest.mark.parametrize("archive_name", ["data/NUL", "data/foo/nul.txt", "data/x/COM1", "data/con"])
def test_members_named_after_windows_devices_are_refused(archive_name, tmp_path: Path):
    """Windows resolves CON, NUL, COM1 and friends to devices in every
    directory. A member named NUL would "restore" into the bit bucket: the write
    succeeds, the file is not there, and the report says everything is fine.
    No real capture can produce these names, so refusing them costs nothing."""
    assert _target_for(archive_name, tmp_path / "dest") is None


def test_a_normal_member_maps_under_the_destination(tmp_path: Path):
    destination = tmp_path / "dest"
    target = _target_for("data/user_files/Documents/a/b.txt", destination)
    assert target == destination / "Documents" / "a" / "b.txt"


def build_bundle(path: Path, members: dict[str, bytes], manifest: dict) -> None:
    kdf = crypto.default_kdf_params()
    header = {
        "format": manifest_mod.BUNDLE_FORMAT_VERSION,
        "schema_version": manifest_mod.SCHEMA_VERSION,
        "cipher": manifest_mod.CIPHER,
        "kdf": crypto.kdf_params_to_json(kdf),
    }
    with bundle_mod.BundleWriter(path, PASSPHRASE, header) as writer:
        for name, data in members.items():
            writer.add_bytes(name, data)
        writer.add_bytes(
            manifest_mod.MANIFEST_ARCHIVE_NAME, manifest_mod.dumps(manifest).encode("utf-8")
        )
    sidecar = manifest_mod.public_view(manifest)
    sidecar.setdefault("bundle", {})["ciphertext"] = {
        "sha256": writer.result.ciphertext_sha256,
        "size_bytes": writer.result.ciphertext_size,
    }
    path.with_suffix(".manifest.json").write_text(manifest_mod.dumps(sidecar), encoding="utf-8")


def minimal_manifest(items: list[dict]) -> dict:
    return {
        "schema_version": manifest_mod.SCHEMA_VERSION,
        "tool": {"name": "winmigrate", "version": "test"},
        "created_utc": "2026-01-01T00:00:00Z",
        "source": {"hostname": "src", "username": "u"},
        "items": items,
        "followups": [],
        "totals": {},
    }


def test_a_hostile_member_name_cannot_write_outside_the_destination(tmp_path: Path):
    bundle_path = tmp_path / "hostile.dat"
    build_bundle(
        bundle_path,
        {
            "data/user_files/Documents/fine.txt": b"ok",
            "data/user_files/../../../../pwned.txt": b"escaped",
        },
        minimal_manifest(
            [
                {
                    "id": "files:documents",
                    "category": "user_files",
                    "kind": "tree",
                    "title": "Documents",
                    "action": "capture",
                    "sensitivity": "normal",
                    "archive_path": "data/user_files/Documents",
                }
            ]
        ),
    )
    destination = tmp_path / "dest"
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle_path, passphrase=PASSPHRASE, destination=destination)
    )
    assert (destination / "Documents" / "fine.txt").read_bytes() == b"ok"
    assert not (tmp_path / "pwned.txt").exists()
    assert not (tmp_path.parent / "pwned.txt").exists()
    assert report.restored_files == 1


def test_a_digest_that_does_not_match_the_manifest_is_reported(tmp_path: Path):
    """Catches corruption that happened on the source side, before encryption."""
    bundle_path = tmp_path / "corrupt.dat"
    build_bundle(
        bundle_path,
        {"data/user_files/Documents/a.txt": b"actual content"},
        minimal_manifest(
            [
                {
                    "id": "files:documents",
                    "category": "user_files",
                    "kind": "tree",
                    "title": "Documents",
                    "action": "capture",
                    "sensitivity": "normal",
                    "archive_path": "data/user_files/Documents",
                    "digest": "0" * 64,
                    "digest_algo": "sha256-tree-v1",
                }
            ]
        ),
    )
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle_path, passphrase=PASSPHRASE, destination=tmp_path / "dest")
    )
    assert report.digest_mismatches == ["files:documents"]
    assert not report.ok


def test_a_bundle_without_a_manifest_is_refused(tmp_path: Path):
    bundle_path = tmp_path / "nomanifest.dat"
    kdf = crypto.default_kdf_params()
    header = {"format": 1, "cipher": manifest_mod.CIPHER, "kdf": crypto.kdf_params_to_json(kdf)}
    with bundle_mod.BundleWriter(bundle_path, PASSPHRASE, header) as writer:
        writer.add_bytes("data/user_files/Documents/a.txt", b"x")
    with pytest.raises(IntegrityError, match="no manifest"):
        restore_mod.restore(
            RestoreOptions(bundle=bundle_path, passphrase=PASSPHRASE, destination=tmp_path / "d")
        )


def test_a_file_that_is_not_a_bundle_is_rejected_clearly(tmp_path: Path):
    fake = tmp_path / "notabundle.dat"
    fake.write_bytes(b"this is just a text file, honestly" * 10)
    with pytest.raises(bundle_mod.BundleFormatError, match="not a WinMigrate bundle"):
        restore_mod.inspect(fake)


def test_inspect_reads_the_header_without_a_passphrase(tmp_path: Path):
    bundle_path = tmp_path / "b.dat"
    build_bundle(bundle_path, {"data/user_files/D/a.txt": b"x"}, minimal_manifest([]))
    header = restore_mod.inspect(bundle_path)
    assert header["cipher"] == "AES-256-GCM"
    assert header["kdf"]["salt_b64"]
    # The header describes how to derive a key, never what is inside.
    assert not any(key in header for key in ("items", "source", "totals", "payload"))
