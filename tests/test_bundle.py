"""Bundle container behaviour under a profile that does not hold still."""

from __future__ import annotations

from pathlib import Path

from winmigrate import bundle as bundle_mod


def _header() -> dict:
    from winmigrate import crypto
    from winmigrate import manifest as manifest_mod

    kdf = crypto.default_kdf_params()
    return {
        "format": 1,
        "cipher": manifest_mod.CIPHER,
        "kdf": crypto.kdf_params_to_json(kdf),
    }




def test_a_file_that_shrinks_mid_read_does_not_corrupt_the_bundle(tmp_path: Path, monkeypatch):
    """A tar member declares its length and tarfile copies exactly that much.

    A live profile does not hold still -- a log rotates, a browser rewrites its
    database -- so a file can shrink between the stat that sizes the header and
    the read that fills it. tarfile raises *after* writing the header, which
    used to desynchronise the stream and make every later member, the manifest
    included, unreadable: a bundle that reported success and could not be read.
    """
    import os

    from winmigrate import crypto
    from winmigrate import manifest as manifest_mod

    source = tmp_path / "shrank.bin"
    source.write_bytes(b"y" * 10)  # only 10 bytes are actually there

    kdf = crypto.default_kdf_params()
    header = {
        "format": 1,
        "cipher": manifest_mod.CIPHER,
        "kdf": crypto.kdf_params_to_json(kdf),
    }
    bundle = tmp_path / "b.dat"

    real_stat = os.stat

    def oversized_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path).endswith("shrank.bin"):
            return type("S", (), {"st_size": 10_000, "st_mtime": result.st_mtime})()
        return result

    with bundle_mod.BundleWriter(bundle, "pw", header) as writer:
        writer.add_bytes("data/before.txt", b"BEFORE")
        monkeypatch.setattr(os, "stat", oversized_stat)
        writer.add_file(source, "data/shrank.bin")
        monkeypatch.undo()
        writer.add_bytes("data/after.txt", b"AFTER")
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b'{"items":[]}')

    assert str(source) in writer.result.changed_while_reading

    names = []
    with bundle_mod.BundleReader(bundle, "pw") as reader:
        for info, stream in reader.members():
            data = stream.read() if stream else b""
            assert len(data) == info.size, f"{info.name} is unreadable"
            names.append(info.name)
    # Everything after the changed file survived, manifest included.
    assert names == [
        "data/before.txt",
        "data/shrank.bin",
        "data/after.txt",
        manifest_mod.MANIFEST_ARCHIVE_NAME,
    ]


def test_a_file_that_grows_mid_read_is_truncated_to_its_declared_size(tmp_path: Path):
    """The other side of the same race: the member must not overrun its header."""
    from winmigrate import crypto
    from winmigrate import manifest as manifest_mod

    source = tmp_path / "grew.bin"
    source.write_bytes(b"z" * 100)
    kdf = crypto.default_kdf_params()
    header = {"format": 1, "cipher": manifest_mod.CIPHER, "kdf": crypto.kdf_params_to_json(kdf)}
    bundle = tmp_path / "b.dat"

    with bundle_mod.BundleWriter(bundle, "pw", header) as writer:
        writer.add_file(source, "data/grew.bin")
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b"{}")

    with bundle_mod.BundleReader(bundle, "pw") as reader:
        for info, stream in reader.members():
            if info.name == "data/grew.bin":
                assert len(stream.read()) == info.size == 100


def test_a_file_that_grew_while_being_read_is_reported_like_one_that_shrank(
    tmp_path: Path, monkeypatch
):
    """Half a file, with a digest that matches it and nothing to say so. The
    shrinking case was reported from the start; growth -- a log being appended
    to while the backup runs -- was silently truncated to the size the scan
    saw, which is the same problem with a tidier shape.
    """
    import types

    source = tmp_path / "app.log"
    source.write_bytes(b"A" * 500)
    real_stat = bundle_mod.os.stat

    def shrinking_stat(path, *args, **kwargs):
        result = real_stat(path, *args, **kwargs)
        if str(path).endswith("app.log"):
            # What the scan saw a moment before the file was appended to.
            return types.SimpleNamespace(st_size=200, st_mtime=result.st_mtime)
        return result

    monkeypatch.setattr(bundle_mod.os, "stat", shrinking_stat)

    output = tmp_path / "b.dat"
    with bundle_mod.BundleWriter(output, "pw", _header()) as writer:
        writer.add_file(source, "data/app.log")

    assert writer.result.changed_while_reading == [str(source)]

    # And the bundle is still readable, holding exactly the declared length.
    with bundle_mod.BundleReader(output, "pw") as reader:
        members = {info.name: stream.read() for info, stream in reader.members() if stream}
    assert members["data/app.log"] == b"A" * 200
