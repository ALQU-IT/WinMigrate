"""Readable, passphrase-free bundle inspection."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from winmigrate import capture as capture_mod
from winmigrate import report
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.platform_win import Environment
from winmigrate.scan import run_scan

PASSPHRASE = "correct horse battery staple"


def make_bundle(tmp_path: Path) -> Path:
    root = tmp_path / "Users" / "a"
    for rel, data in {
        "Documents/report.txt": b"hello",
        ".ssh/id_rsa": b"PRIVATE",
        ".gitconfig": b"[user]",
    }.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    env = Environment.fixture(root, {})
    config = ScanConfig(profile_root=root, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env)
    return bundle


def summary_text(bundle: Path) -> str:
    header = restore_mod.inspect(bundle)
    sidecar = restore_mod.load_sidecar(bundle)
    console = Console(record=True, width=100)
    report.render_bundle_summary(header, sidecar, console)
    return console.export_text()


def test_inspect_summarises_without_a_passphrase(tmp_path: Path):
    text = summary_text(make_bundle(tmp_path))
    assert "WinMigrate bundle" in text
    assert "AES-256-GCM" in text
    assert "argon2id" in text
    assert "dev config" in text
    assert "user files" in text


def test_inspect_never_names_a_secret_item(tmp_path: Path):
    text = summary_text(make_bundle(tmp_path))
    # The dev-config secrets are counted, not named or pathed.
    assert ".ssh" not in text
    assert "id_rsa" not in text
    assert "encrypted-only" in text


def test_inspect_lists_followups_by_title(tmp_path: Path):
    text = summary_text(make_bundle(tmp_path))
    assert "follow-up" in text.lower()


def test_inspect_of_a_bundle_with_no_sidecar_still_reads_the_header(tmp_path: Path):
    bundle = make_bundle(tmp_path)
    bundle.with_suffix(".manifest.json").unlink()
    header = restore_mod.inspect(bundle)
    console = Console(record=True, width=100)
    report.render_bundle_summary(header, None, console)
    text = console.export_text()
    assert "AES-256-GCM" in text
    assert "only the header" in text.lower()
