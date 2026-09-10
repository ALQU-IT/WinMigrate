"""Exclusion presets: the large, optional things a profile carries."""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate import presets
from winmigrate.config import ScanConfig
from winmigrate.models import Action
from winmigrate.platform_win import Environment
from winmigrate.scan import run_scan


def build_profile(tmp_path: Path) -> Path:
    root = tmp_path / "Users" / "a"

    def w(rel: str, size: int) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)

    w("Documents/report.txt", 100)
    w("Apple/MobileSync/Backup/dev/data.mbdb", 5_000_000)  # iOS backup
    w("Apple/Preferences/settings.plist", 200)             # small, kept
    w("VirtualBox VMs/Win11/disk.vdi", 8_000_000)          # VM disk
    return root


def scan(root: Path, preset_names: list[str]):
    patterns = presets.resolve(preset_names)
    config = ScanConfig(
        profile_root=root,
        include_software=False,
        extra_excludes=patterns,
        active_presets=tuple(preset_names),
    )
    return run_scan(config, Environment.fixture(root, {}))


def test_by_default_large_optional_things_are_kept(tmp_path: Path):
    root = build_profile(tmp_path)
    result = run_scan(ScanConfig(profile_root=root, include_software=False), Environment.fixture(root, {}))
    apple = next(i for i in result.items if i.id == "files:other:apple")
    vm = next(i for i in result.items if i.id == "files:other:virtualbox vms")
    assert apple.size_bytes == 5_000_200
    assert vm.size_bytes == 8_000_000


def test_device_backups_preset_drops_the_ios_backup_but_keeps_other_apple_data(tmp_path: Path):
    root = build_profile(tmp_path)
    result = scan(root, ["device-backups"])
    apple = next(i for i in result.items if i.id == "files:other:apple")
    # The 5 MB MobileSync backup is gone; the 200-byte plist stays.
    assert apple.size_bytes == 200


def test_vm_images_preset_drops_the_vm_folder(tmp_path: Path):
    root = build_profile(tmp_path)
    result = scan(root, ["vm-images"])
    # The whole VirtualBox VMs folder is excluded at the top level.
    vm = [i for i in result.items if i.id == "files:other:virtualbox vms"]
    assert not vm or vm[0].action is Action.SKIP


def test_presets_stack(tmp_path: Path):
    root = build_profile(tmp_path)
    result = scan(root, ["device-backups", "vm-images"])
    totals = result.totals()
    assert totals.capture_bytes == 300  # report.txt + the small plist


def test_active_presets_are_announced_in_the_scan_notes(tmp_path: Path):
    root = build_profile(tmp_path)
    result = scan(root, ["vm-images"])
    assert any(
        note.message.startswith("Exclusion preset(s) active") and "vm-images" in note.message
        for note in result.notes
    )


def test_resolve_flattens_and_dedups():
    patterns = presets.resolve(["vm-images", "vm-images"])
    assert patterns == presets.resolve(["vm-images"])
    assert "*.vhdx" in patterns


def test_resolve_rejects_an_unknown_preset():
    with pytest.raises(presets.UnknownPreset):
        presets.resolve(["not-a-preset"])


def test_every_preset_has_a_summary_and_patterns():
    for preset in presets.list_presets():
        assert preset.summary
        assert preset.patterns
