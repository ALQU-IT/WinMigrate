"""Browser profiles and sign-in detection -- folder copy and config reads only.

The parsers are pure and exercised directly; the whole scan is exercised over a
fixture profile tree. The property that matters most -- the password and cookie
stores are never carried, and nothing sensitive reaches the plaintext sidecar --
is asserted end to end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from winmigrate import capture as capture_mod
from winmigrate import manifest as manifest_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.models import Action, Category, Sensitivity, SkipReason
from winmigrate.platform_win import Environment
from winmigrate.restore import RestoreOptions
from winmigrate.scan import browsers, run_scan

PASSPHRASE = "correct horse battery staple"


# --- pure parsers ----------------------------------------------------------
def test_chromium_signin_reads_account_and_sync_without_touching_the_store():
    prefs = json.dumps(
        {
            "account_info": [{"email": "a@example.com", "full_name": "A"}],
            "sync": {"has_setup_completed": True},
            "profile": {"name": "Work"},
        }
    )
    state = browsers.parse_chromium_signin(prefs)
    assert state.signed_in and state.sync_on
    assert state.account_email == "a@example.com"


def test_chromium_signed_in_but_sync_off_is_distinguished():
    prefs = json.dumps({"account_info": [{"email": "a@example.com"}], "sync": {"requested": False}})
    state = browsers.parse_chromium_signin(prefs)
    assert state.signed_in and not state.sync_on


def test_chromium_not_signed_in():
    state = browsers.parse_chromium_signin(json.dumps({"profile": {"name": "Person 1"}}))
    assert not state.signed_in and not state.sync_on and state.account_email is None


def test_malformed_preferences_yield_a_blank_state_not_an_error():
    assert browsers.parse_chromium_signin("{not json").signed_in is False
    assert browsers.parse_chromium_signin("").signed_in is False


def test_firefox_prefs_sync_username_is_the_signal():
    prefs = 'user_pref("services.sync.username", "a@mozilla.example");\n'
    state = browsers.parse_firefox_prefs(prefs)
    assert state.signed_in and state.sync_on
    assert state.account_email == "a@mozilla.example"


def test_firefox_without_sync_username_is_not_signed_in():
    assert browsers.parse_firefox_prefs('user_pref("browser.startup.page", 1);').signed_in is False


def test_firefox_profiles_ini_is_parsed():
    ini = "[Profile0]\nName=default-release\nIsRelative=1\nPath=Profiles/x.default\n[General]\nVersion=2\n"
    parsed = browsers._firefox_profiles(ini)
    assert parsed == [("default-release", "Profiles/x.default", True)]


# --- fixture-based scan ----------------------------------------------------
def build_browsers(root: Path, *, chrome_sync: bool) -> None:
    def w(rel: str, data: bytes) -> None:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    chrome = "AppData/Local/Google/Chrome/User Data/Default"
    w(
        f"{chrome}/Preferences",
        json.dumps(
            {
                "account_info": [{"email": "me@example.com"}],
                "sync": {"has_setup_completed": chrome_sync},
                "profile": {"name": "Me"},
            }
        ).encode(),
    )
    w(f"{chrome}/Bookmarks", b'{"roots":{}}')
    w(f"{chrome}/History", b"history db")
    w(f"{chrome}/Login Data", b"ENCRYPTED-PASSWORD-STORE")
    w(f"{chrome}/Network/Cookies", b"SESSION-COOKIES")
    w(f"{chrome}/Cache/data_0", b"x" * 5000)
    w(f"{chrome}/Code Cache/js/b", b"x" * 3000)


@pytest.fixture
def browser_profile(tmp_path: Path) -> Path:
    root = tmp_path / "alice"
    (root / "Documents").mkdir(parents=True)
    build_browsers(root, chrome_sync=False)
    return root


def scan(root: Path):
    return run_scan(ScanConfig(profile_root=root, include_software=False), Environment.fixture(root, {}))


def chrome_item(result):
    return next(i for i in result.items if i.category is Category.BROWSER_PROFILE)


def test_a_chromium_profile_is_captured_as_a_secret_item(browser_profile: Path):
    item = chrome_item(scan(browser_profile))
    assert item.sensitivity is Sensitivity.SECRET
    assert item.title.startswith("Google Chrome")
    assert item.record["sign_in"]["account_email"] == "me@example.com"


def test_the_password_and_cookie_stores_are_never_captured(browser_profile: Path):
    item = chrome_item(scan(browser_profile))
    # Bookmarks + History + Preferences are captured; Login Data, Cookies and the
    # caches are excluded.
    reasons = {group.reason for group in item.skipped}
    assert SkipReason.EXCLUDED in reasons
    assert SkipReason.REGENERABLE in reasons
    assert item.file_count == 3  # Preferences, Bookmarks, History


def test_sync_off_recommends_the_browser_export_sync_on_recommends_sign_in(tmp_path: Path):
    off = tmp_path / "off"
    (off / "Documents").mkdir(parents=True)
    build_browsers(off, chrome_sync=False)
    followup = next(f for f in scan(off).followups if f.id == "browser:passwords:chrome")
    assert "export" in followup.title.lower()
    assert any("Windows" in step for step in followup.steps)

    on = tmp_path / "on"
    (on / "Documents").mkdir(parents=True)
    build_browsers(on, chrome_sync=True)
    followup = next(f for f in scan(on).followups if f.id == "browser:passwords:chrome")
    assert "sync down" in followup.title.lower()


def test_no_browsers_means_no_browser_items(tmp_path: Path):
    root = tmp_path / "bare"
    (root / "Documents").mkdir(parents=True)
    result = scan(root)
    assert not [i for i in result.items if i.category is Category.BROWSER_PROFILE]


def test_files_only_mode_drops_browser_profiles(browser_profile: Path):
    result = run_scan(
        ScanConfig(profile_root=browser_profile, include_software=False, files_only=True),
        Environment.fixture(browser_profile, {}),
    )
    item = chrome_item(result)
    assert item.action is Action.SKIP
    assert item.skip_reason is SkipReason.FILES_ONLY_MODE


def test_round_trip_keeps_content_and_drops_credential_stores(browser_profile: Path, tmp_path: Path):
    env = Environment.fixture(browser_profile, {})
    config = ScanConfig(profile_root=browser_profile, include_software=False)
    result = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(result, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env)

    # The sidecar names nothing sensitive.
    sidecar = bundle.with_suffix(".manifest.json").read_text()
    for needle in ("ENCRYPTED-PASSWORD", "SESSION-COOKIES", "me@example.com", "Bookmarks", "History"):
        assert needle not in sidecar

    destination = tmp_path / "restored"
    report = restore_mod.restore(RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination))
    assert report.ok and not report.digest_mismatches
    base = destination / "AppData/Local/Google/Chrome/User Data/Default"
    assert (base / "Bookmarks").is_file()
    assert (base / "History").is_file()
    assert not (base / "Login Data").exists()
    assert not (base / "Network" / "Cookies").exists()
    assert not (base / "Cache").exists()


def test_the_full_manifest_holds_the_account_but_the_sidecar_does_not(browser_profile: Path):
    result = scan(browser_profile)
    full = manifest_mod.build(result)
    assert "me@example.com" in json.dumps(full)  # authoritative copy keeps it
    public = manifest_mod.public_view(full)
    assert "me@example.com" not in json.dumps(public)  # redacted stub in the sidecar


def test_two_profiles_with_the_same_display_name_get_distinct_ids(tmp_path):
    """Chromium names every new profile "Person 1" by default, so keying an item
    on the friendly name collides -- and a duplicate id fails manifest
    validation, making the bundle un-restorable. The directory name is unique."""
    from winmigrate import manifest as manifest_mod
    from winmigrate.models import ScanResult

    root = tmp_path / "Users" / "a"
    user_data = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    for sub in ("Default", "Profile 1"):
        d = user_data / sub
        d.mkdir(parents=True)
        (d / "Preferences").write_text(json.dumps({"profile": {"name": "Person 1"}}))
    env = Environment.fixture(root, {})

    items, _followups, _notes = browsers.scan_browsers(env)
    ids = [item.id for item in items]
    assert ids == ["browser:chrome:default", "browser:chrome:profile-1"]
    assert len(ids) == len(set(ids))

    result = ScanResult(source=manifest_mod.detect_source_machine(str(root)))
    result.items = items
    manifest_mod.validate(manifest_mod.build(result))  # must not raise


def test_a_firefox_profile_outside_the_user_folder_is_not_silently_relocated(tmp_path):
    """profiles.ini can point anywhere -- D:\\FFProfiles\\work is an ordinary setup.

    Archive names under ``secrets/`` are profile-relative by convention, so such
    a path cannot be expressed in one. It used to go through the drive-stripping
    fallback and restore to ``%USERPROFILE%\\FFProfiles\\work``: a nonsense path,
    with nothing in the manifest, the report or the follow-ups saying the profile
    had moved. Now the relocation is named, warned about, and has a follow-up
    telling the user how to put it back.
    """
    root = tmp_path / "Users" / "a"
    firefox = root / "AppData" / "Roaming" / "Mozilla" / "Firefox"
    firefox.mkdir(parents=True)
    outside = tmp_path / "D_drive" / "FFProfiles" / "work"
    outside.mkdir(parents=True)
    (outside / "prefs.js").write_text('user_pref("browser.startup.page", 3);')
    (firefox / "profiles.ini").write_text(
        f"[Profile0]\nName=work\nIsRelative=0\nPath={outside}\n"
    )

    env = Environment.fixture(root, {})
    items, followups, _notes = browsers.scan_browsers(env)
    item = next(i for i in items if i.category is Category.BROWSER_PROFILE)

    assert item.archive_path == "secrets/WinMigrate-Relocated/firefox/work"
    assert item.restore.target == "%USERPROFILE%\\WinMigrate-Relocated\\firefox\\work"
    # Where it lands must be what the item promises, not an invented path.
    assert restore_mod._target_for(item.archive_path, Path("/dest")) == Path(
        "/dest/WinMigrate-Relocated/firefox/work"
    )
    # The move is stated, not buried: a warning naming the original location...
    assert any(str(outside) in (note.message or "") for note in item.notes)
    # ...and a follow-up, because the browser still looks at the old path.
    relocation = next(f for f in followups if f.id.startswith("browser:relocated:"))
    assert str(outside) in relocation.why


def test_a_firefox_profile_inside_the_user_folder_is_untouched_by_the_relocation_path(
    tmp_path,
):
    """The ordinary case must keep its real location, and raise no follow-up."""
    root = tmp_path / "Users" / "a"
    profiles = root / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "abc.default"
    profiles.mkdir(parents=True)
    (profiles / "prefs.js").write_text('user_pref("x", 1);')
    (profiles.parent.parent / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\n"
    )

    items, followups, _notes = browsers.scan_browsers(Environment.fixture(root, {}))
    item = next(i for i in items if i.category is Category.BROWSER_PROFILE)
    assert item.archive_path == "secrets/AppData/Roaming/Mozilla/Firefox/Profiles/abc.default"
    assert not any(f.id.startswith("browser:relocated:") for f in followups)
