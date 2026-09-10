"""Browser extensions: what is installed, and whether its data came too.

The extensions were always in the bundle -- they live inside the profile
directory, which is copied whole. What was missing was any way to know it. A
profile copy reported "15 files" and the user found out on the new machine
whether their uBlock rules had survived.
"""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate.platform_win import Environment
from winmigrate.scan import browsers, extensions


def chromium_extension(
    profile: Path, ext_id: str, name: str, version: str, *, localized: bool = False
) -> Path:
    version_dir = profile / "Extensions" / ext_id / f"{version}_0"
    version_dir.mkdir(parents=True)
    if localized:
        manifest = {"name": "__MSG_extName__", "version": version, "default_locale": "en"}
        locale = version_dir / "_locales" / "en"
        locale.mkdir(parents=True)
        (locale / "messages.json").write_text(json.dumps({"extName": {"message": name}}))
    else:
        manifest = {"name": name, "version": version}
    (version_dir / "manifest.json").write_text(json.dumps(manifest))
    return version_dir


def test_a_localized_name_is_resolved_from_the_locale_file(tmp_path: Path):
    """Most Store extensions localise their name, so manifest.json holds
    __MSG_extName__ and the real string is in _locales/<locale>/messages.json.
    Without resolving it the inventory is a list of __MSG_appName__ and tells
    the user nothing about what they had."""
    profile = tmp_path / "Default"
    profile.mkdir()
    chromium_extension(profile, "a" * 32, "uBlock Origin", "1.60.0", localized=True)

    found = extensions.inventory(profile, "chromium")
    assert [(e.name, e.version) for e in found] == [("uBlock Origin", "1.60.0")]


def test_an_update_leaves_an_old_version_behind_and_the_newest_one_wins(tmp_path: Path):
    """Chromium keeps <id>/<version>_<n>/ and does not always clean up the
    previous one. Sorting has to be numeric: 1.9.0 must not beat 1.60.0."""
    profile = tmp_path / "Default"
    profile.mkdir()
    ext_id = "a" * 32
    chromium_extension(profile, ext_id, "uBlock Origin", "1.9.0")
    chromium_extension(profile, ext_id, "uBlock Origin", "1.60.0")

    found = extensions.inventory(profile, "chromium")
    assert [e.version for e in found] == ["1.60.0"]


def test_saved_extension_data_is_found_in_both_places_chromium_keeps_it(tmp_path: Path):
    profile = tmp_path / "Default"
    profile.mkdir()
    ext_id = "b" * 32
    chromium_extension(profile, ext_id, "Tab Manager", "3.0")
    settings = profile / "Local Extension Settings" / ext_id
    settings.mkdir(parents=True)
    (settings / "000003.log").write_bytes(b"x" * 500)
    idb = profile / "IndexedDB" / f"chrome-extension_{ext_id}_0.indexeddb.leveldb"
    idb.mkdir(parents=True)
    (idb / "000005.ldb").write_bytes(b"y" * 300)

    found = extensions.inventory(profile, "chromium")
    assert found[0].has_data is True
    assert found[0].data_bytes == 800


def test_an_extension_with_no_saved_data_is_not_claimed_to_have_any(tmp_path: Path):
    profile = tmp_path / "Default"
    profile.mkdir()
    chromium_extension(profile, "c" * 32, "My Internal Tool", "0.1")
    found = extensions.inventory(profile, "chromium")
    assert found[0].has_data is False and found[0].data_bytes == 0


def test_firefox_lists_the_users_addons_and_not_mozillas(tmp_path: Path):
    """extensions.json holds the built-in theme and the system add-ons that ship
    with Firefox alongside the ones the user chose. Listing those as things to
    reinstall would bury the handful that are actually theirs."""
    profile = tmp_path / "abc.default"
    profile.mkdir()
    (profile / "extensions.json").write_text(
        json.dumps(
            {
                "addons": [
                    {
                        "id": "uBlock0@raymondhill.net",
                        "type": "extension",
                        "version": "1.60.0",
                        "location": "app-profile",
                        "defaultLocale": {"name": "uBlock Origin"},
                    },
                    {
                        "id": "default-theme@mozilla.org",
                        "type": "theme",
                        "location": "app-builtin",
                        "defaultLocale": {"name": "System theme"},
                    },
                    {
                        "id": "formautofill@mozilla.org",
                        "type": "extension",
                        "location": "app-system-defaults",
                        "defaultLocale": {"name": "Form Autofill"},
                    },
                ]
            }
        )
    )
    data = profile / "browser-extension-data" / "uBlock0@raymondhill.net"
    data.mkdir(parents=True)
    (data / "storage.js").write_text('{"myFilters": "custom rules"}')

    found = extensions.inventory(profile, "firefox")
    assert [e.name for e in found] == ["uBlock Origin"]
    assert found[0].has_data is True


def test_unreadable_or_malformed_manifests_do_not_break_the_scan(tmp_path: Path):
    """A half-written manifest, a stray file where a version directory should
    be, an extensions.json that is not JSON -- none of it is worth failing a
    scan over."""
    profile = tmp_path / "Default"
    profile.mkdir()
    (profile / "Extensions").mkdir()
    (profile / "Extensions" / "loose-file.txt").write_text("not a directory")
    broken = profile / "Extensions" / ("d" * 32) / "1.0_0"
    broken.mkdir(parents=True)
    (broken / "manifest.json").write_text("{not json")
    chromium_extension(profile, "e" * 32, "Good One", "2.0")

    assert [e.name for e in extensions.inventory(profile, "chromium")] == ["Good One"]

    firefox = tmp_path / "ff"
    firefox.mkdir()
    (firefox / "extensions.json").write_text("<html>not json</html>")
    assert extensions.inventory(firefox, "firefox") == []


def test_no_extensions_means_no_followup_and_no_note(tmp_path: Path):
    root = tmp_path / "alice"
    profile = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(json.dumps({"profile": {"name": "P"}}))

    _items, followups, _notes = browsers.scan_browsers(Environment.fixture(root, {}))
    assert not any(f.id.startswith("browser:extensions:") for f in followups)


def test_the_followup_names_each_extension_and_says_reinstalling_is_expected(tmp_path: Path):
    """The step people are most surprised by. Chromium ties its extension
    registry to the machine, so a migrated profile usually opens with the
    extensions disabled or absent -- which looks like the migration lost them.
    It did not: the data is restored and comes back when each one is installed
    again. The list is the only thing on the new machine that says what to
    install."""
    root = tmp_path / "alice"
    profile = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(json.dumps({"profile": {"name": "P"}}))
    chromium_extension(profile, "f" * 32, "uBlock Origin", "1.60.0")
    settings = profile / "Local Extension Settings" / ("f" * 32)
    settings.mkdir(parents=True)
    (settings / "000003.log").write_bytes(b"rules")

    items, followups, _notes = browsers.scan_browsers(Environment.fixture(root, {}))
    followup = next(f for f in followups if f.id == "browser:extensions:chrome")
    steps = "\n".join(followup.steps)
    assert "uBlock Origin (1.60.0)" in steps and "has saved data" in steps
    assert "reinstall" in followup.title
    # The inventory rides on the item's record, which is redacted from the
    # plaintext sidecar with the rest of a SECRET item -- an extension list
    # says a lot about someone.
    assert items[0].record["extensions"][0]["name"] == "uBlock Origin"
