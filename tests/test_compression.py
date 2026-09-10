"""Compressing only when there is something to gain.

A capture's throughput was set by gzip and nothing else. Measured here: gzip
level 9 manages ~35 MB/s on already-compressed data and returns it at a ratio
of 1.000. SHA-256 runs at ~1 GB/s and AES-256-GCM at ~550 MB/s, so on a profile
full of photos and video the pipeline spent all its time on the one step doing
no work.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from winmigrate import bundle as bundle_mod
from winmigrate import capture as capture_mod
from winmigrate import compression
from winmigrate import crypto
from winmigrate import manifest as manifest_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.platform_win import Environment
from winmigrate.restore import RestoreOptions
from winmigrate.scan import run_scan

PASSPHRASE = "correct horse battery staple"


@pytest.mark.parametrize(
    "name, incompressible",
    [
        ("holiday.jpg", True),
        ("clip.MP4", True),          # case does not matter
        ("backup.zip", True),
        ("report.pdf", True),
        ("sheet.xlsx", True),        # a zip container
        ("notes.txt", False),
        ("main.py", False),
        ("archive.pst", False),      # a mail store does compress
        ("Makefile", False),         # no extension at all
        ("a.b.c.jpg", True),         # only the last suffix counts
    ],
)
def test_extensions_that_are_already_compressed_are_recognised(name, incompressible):
    assert compression.is_incompressible(name) is incompressible


def test_auto_skips_compression_when_the_bytes_cannot_shrink():
    """The decision that matters: a personal profile is mostly media."""
    assert compression.choose("auto", 5, 1000) == (compression.NONE, 0)
    assert compression.choose("auto", 900, 1000)[0] == compression.GZIP
    # Level 1, never 9: same ratio to three decimals, 2.7x the throughput.
    assert compression.choose("auto", 900, 1000)[1] == compression.FAST_LEVEL
    # An empty plan must not divide by zero.
    assert compression.choose("auto", 0, 0)[0] == compression.GZIP


def test_explicit_settings_override_the_measurement():
    assert compression.choose("none", 1000, 1000) == (compression.NONE, 0)
    assert compression.choose("fast", 0, 1000) == (compression.GZIP, compression.FAST_LEVEL)
    assert compression.choose("best", 0, 1000) == (compression.GZIP, compression.BEST_LEVEL)


def test_the_scan_counts_which_bytes_could_actually_shrink(tmp_path: Path):
    profile = tmp_path / "alice"
    pictures = profile / "Pictures"
    documents = profile / "Documents"
    pictures.mkdir(parents=True)
    documents.mkdir(parents=True)
    (pictures / "IMG_0001.jpg").write_bytes(os.urandom(4096))
    (pictures / "clip.mp4").write_bytes(os.urandom(8192))
    (documents / "notes.txt").write_bytes(b"x" * 1024)

    env = Environment.fixture(profile, {})
    scan = run_scan(ScanConfig(profile_root=profile, include_software=False), env)
    totals = scan.totals()
    assert totals.capture_bytes == 4096 + 8192 + 1024
    assert totals.compressible_bytes == 1024


def round_trip(tmp_path: Path, setting: str) -> tuple[str, int]:
    """Capture a media-heavy profile with `setting`, restore it, return the
    algorithm used and the bundle size."""
    profile = tmp_path / f"alice-{setting}"
    pictures = profile / "Pictures"
    pictures.mkdir(parents=True)
    (profile / "Documents").mkdir()
    (pictures / "IMG_0001.jpg").write_bytes(os.urandom(200_000))
    (profile / "Documents" / "notes.txt").write_bytes(b"the quick brown fox " * 500)

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    bundle = tmp_path / f"{setting}.dat"
    report = capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(
            output=bundle, passphrase=PASSPHRASE, use_vss=False, compression=setting
        ),
        config,
        env,
    )
    destination = tmp_path / f"restored-{setting}"
    destination.mkdir()
    result = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    assert result.ok and not result.digest_mismatches, setting
    assert (destination / "Pictures" / "IMG_0001.jpg").stat().st_size == 200_000
    assert (destination / "Documents" / "notes.txt").read_bytes() == b"the quick brown fox " * 500
    return report.compression, report.bundle_bytes


def test_an_uncompressed_bundle_round_trips(tmp_path: Path):
    """"none" is a real format, not a degraded mode: the reader has to honour
    the header rather than assuming every payload is gzip."""
    algorithm, _size = round_trip(tmp_path, "none")
    assert algorithm == compression.NONE


def test_a_compressed_bundle_still_round_trips(tmp_path: Path):
    algorithm, _size = round_trip(tmp_path, "best")
    assert algorithm == compression.GZIP


def test_auto_picks_none_for_a_media_heavy_profile(tmp_path: Path):
    algorithm, _size = round_trip(tmp_path, "auto")
    assert algorithm == compression.NONE


def test_a_bundle_written_before_compression_was_a_choice_still_reads(tmp_path: Path):
    """Older bundles have no "compression" field at all and are gzip. A missing
    value must mean gzip, not none, or every existing bundle becomes unreadable
    at the first tar header."""
    kdf = crypto.default_kdf_params()
    header = {
        "format": manifest_mod.BUNDLE_FORMAT_VERSION,
        "cipher": manifest_mod.CIPHER,
        "kdf": crypto.kdf_params_to_json(kdf),
        # deliberately no "compression" key
    }
    bundle = tmp_path / "legacy.dat"
    with bundle_mod.BundleWriter(bundle, PASSPHRASE, header) as writer:
        writer.add_bytes("data/hello.txt", b"from an older version")
        writer.add_bytes(manifest_mod.MANIFEST_ARCHIVE_NAME, b'{"items": []}')

    stored, _digest, handle = bundle_mod.read_header(bundle)
    handle.close()
    assert "compression" not in stored  # nothing was invented on the way out

    with bundle_mod.BundleReader(bundle, PASSPHRASE) as reader:
        names = {info.name: stream.read() for info, stream in reader.members() if stream}
    assert names["data/hello.txt"] == b"from an older version"
