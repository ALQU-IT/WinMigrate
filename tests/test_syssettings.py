"""System settings captured as records or small trees."""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate import manifest as manifest_mod
from winmigrate.models import Category, Kind, ScanResult
from winmigrate.platform_win import Environment
from winmigrate.scan import syssettings


def env_with(registry: dict, root: Path | None = None) -> Environment:
    return Environment.fixture(root or Path("/tmp/p"), registry)


def item(items, item_id):
    return next(i for i in items if i.id == item_id)


def test_environment_variables_are_recorded_from_the_registry():
    env = env_with({"HKCU\\Environment": {"PATH": "C:\\bin", "MYTOKEN": "abc"}})
    items, _ = syssettings.scan_system_settings(env)
    env_item = item(items, "settings:env_vars")
    assert env_item.category is Category.ENV_VARS
    assert env_item.record["variables"] == {"MYTOKEN": "abc", "PATH": "C:\\bin"}


def test_environment_variables_stay_out_of_the_public_sidecar():
    """A user PATH or custom var can hold a token, so the record is not public."""
    env = env_with({"HKCU\\Environment": {"SECRET_TOKEN": "ghp_abcdef"}})
    items, _ = syssettings.scan_system_settings(env)
    result = ScanResult(source=manifest_mod.detect_source_machine("/tmp/p"))
    result.items = items
    public = manifest_mod.public_view(manifest_mod.build(result))
    assert "ghp_abcdef" not in json.dumps(public)
    stub = next(i for i in public["items"] if i["id"] == "settings:env_vars")
    assert stub.get("record_withheld") is True


def test_mapped_drives_are_read_from_the_network_key():
    env = env_with({
        r"HKCU\Network\Z": {"RemotePath": r"\\server\share"},
        r"HKCU\Network\Y": {"RemotePath": r"\\nas\media"},
    })
    items, _ = syssettings.scan_system_settings(env)
    drives = item(items, "settings:mapped_drives").record["drives"]
    assert drives == {"Z": r"\\server\share", "Y": r"\\nas\media"}


def test_printers_and_default_are_read():
    env = env_with({
        r"HKCU\Printers\Connections\,,printserver,HP-Laser": {},
        r"HKCU\Software\Microsoft\Windows NT\CurrentVersion\Windows": {
            "Device": "HP-Laser,winspool,Ne00:"
        },
    })
    items, _ = syssettings.scan_system_settings(env)
    printers = item(items, "settings:printers").record
    assert printers["default"] == "HP-Laser"
    # A full UNC path, so it can be pasted straight into Add Printer.
    assert printers["connections"] == [r"\\printserver\HP-Laser"]


def test_absent_settings_produce_no_items(tmp_path: Path):
    (tmp_path / "Documents").mkdir()
    items, _ = syssettings.scan_system_settings(Environment.fixture(tmp_path, {}))
    assert items == []


def test_per_user_fonts_are_captured_as_a_tree(tmp_path: Path):
    fonts = tmp_path / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "MyFont.ttf").write_bytes(b"font")
    items, _ = syssettings.scan_system_settings(Environment.fixture(tmp_path, {}))
    fonts_item = item(items, "settings:fonts")
    assert fonts_item.kind is Kind.TREE
    assert fonts_item.archive_path == "data/AppData/Local/Microsoft/Windows/Fonts"


def test_outlook_signatures_and_pst_are_captured_but_ost_is_not(tmp_path: Path):
    sig = tmp_path / "AppData" / "Roaming" / "Microsoft" / "Signatures"
    sig.mkdir(parents=True)
    (sig / "mine.htm").write_text("<p>Regards</p>")
    outlook = tmp_path / "AppData" / "Local" / "Microsoft" / "Outlook"
    outlook.mkdir(parents=True)
    (outlook / "archive.pst").write_bytes(b"MAIL")
    (outlook / "account.ost").write_bytes(b"CACHE")  # must be skipped

    items, _ = syssettings.scan_system_settings(Environment.fixture(tmp_path, {}))
    ids = {i.id for i in items}
    assert "settings:outlook_signatures" in ids
    assert "settings:outlook_pst:archive" in ids
    assert not any("ost" in i.id or "account" in i.id for i in items)
    pst = item(items, "settings:outlook_pst:archive")
    assert pst.archive_path == "data/AppData/Local/Microsoft/Outlook/archive.pst"


def test_guided_settings_get_a_followup():
    env = env_with({r"HKCU\Network\Z": {"RemotePath": r"\\server\share"}})
    _items, followups = syssettings.scan_system_settings(env)
    assert any(f.id == "settings:mapped_drives:guided" for f in followups)


def test_settings_land_where_their_restore_spec_says_they_will(tmp_path: Path):
    """The archive path is placement, not a label.

    Restore joins everything under ``data/`` onto the destination profile, so an
    archive name has to be the real profile-relative location. Tidy invented
    names (``data/fonts``, ``data/outlook/...``) read fine in the manifest and
    then put the fonts in a folder Windows never looks at and the .pst in one
    Outlook cannot see -- a restore that reports success and silently delivers
    nothing usable. This pins each item's placement to the target it promises.
    """
    from winmigrate.restore import _target_for

    fonts = tmp_path / "AppData" / "Local" / "Microsoft" / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "MyFont.ttf").write_bytes(b"font")
    sig = tmp_path / "AppData" / "Roaming" / "Microsoft" / "Signatures"
    sig.mkdir(parents=True)
    (sig / "mine.htm").write_text("<p>Regards</p>")
    outlook = tmp_path / "AppData" / "Local" / "Microsoft" / "Outlook"
    outlook.mkdir(parents=True)
    (outlook / "archive.pst").write_bytes(b"MAIL")

    destination = Path("/dest")
    expansions = {
        "%LOCALAPPDATA%": "/dest/AppData/Local",
        "%APPDATA%": "/dest/AppData/Roaming",
        "%USERPROFILE%": "/dest",
    }
    items, _ = syssettings.scan_system_settings(Environment.fixture(tmp_path, {}))
    checked = 0
    for entry in items:
        if not entry.archive_path or not entry.restore:
            continue
        promised = entry.restore.target.replace("\\", "/")
        for name, value in expansions.items():
            promised = promised.replace(name, value)
        assert str(_target_for(entry.archive_path, destination)) == promised, entry.id
        checked += 1
    assert checked == 3  # fonts, signatures, the .pst


def test_a_redirected_appdata_outside_the_profile_is_not_captured_at_a_made_up_path(
    tmp_path: Path, monkeypatch
):
    """``data/`` names are profile-relative; a path outside it cannot be one.

    Rather than run such a path through the drive-stripping fallback and restore
    it somewhere arbitrary, the item is dropped and logged.
    """
    elsewhere = tmp_path / "redirected"
    fonts = elsewhere / "Local" / "Microsoft" / "Windows" / "Fonts"
    fonts.mkdir(parents=True)
    (fonts / "MyFont.ttf").write_bytes(b"font")
    profile = tmp_path / "profile"
    profile.mkdir()

    monkeypatch.setattr(Environment, "appdata_local", lambda self: elsewhere / "Local")
    monkeypatch.setattr(Environment, "appdata_roaming", lambda self: elsewhere / "Roaming")
    items, _ = syssettings.scan_system_settings(Environment.fixture(profile, {}))
    assert not any(i.id == "settings:fonts" for i in items)
