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

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
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
    sidecar = bundle.with_suffix(".manifest.json").read_text(encoding="utf-8")
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
        (d / "Preferences").write_text(json.dumps({"profile": {"name": "Person 1"}}), encoding="utf-8")
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
    (outside / "prefs.js").write_text('user_pref("browser.startup.page", 3);', encoding="utf-8")
    (firefox / "profiles.ini").write_text(
        f"[Profile0]\nName=work\nIsRelative=0\nPath={outside}\n"
, encoding="utf-8")

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
    (profiles / "prefs.js").write_text('user_pref("x", 1);', encoding="utf-8")
    (profiles.parent.parent / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\n"
, encoding="utf-8")

    items, followups, _notes = browsers.scan_browsers(Environment.fixture(root, {}))
    item = next(i for i in items if i.category is Category.BROWSER_PROFILE)
    assert item.archive_path == "secrets/AppData/Roaming/Mozilla/Firefox/Profiles/abc.default"
    assert not any(f.id.startswith("browser:relocated:") for f in followups)


def firefox_profile_with_stores(root: Path) -> Path:
    """A Firefox profile holding both real data and the credential stores."""
    profile = root / "AppData" / "Roaming" / "Mozilla" / "Firefox" / "Profiles" / "abc.default"
    profile.mkdir(parents=True)
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    for name, blob in [
        ("prefs.js", b'user_pref("browser.startup.page", 3);'),
        ("places.sqlite", b"BOOKMARKS-AND-HISTORY"),
        ("logins.json", b"SAVED-PASSWORDS"),
        ("logins-backup.json", b"SAVED-PASSWORDS-BACKUP"),
        ("key4.db", b"THE-KEY-THAT-OPENS-THEM"),
        ("cookies.sqlite", b"SESSION-COOKIES"),
    ]:
        (profile / name).write_bytes(blob)
    (profile.parent.parent / "profiles.ini").write_text(
        "[Profile0]\nName=default\nIsRelative=1\nPath=Profiles/abc.default\n"
, encoding="utf-8")
    return profile


def restored_names(root: Path, tmp_path: Path, extra_includes=()) -> set[str]:
    config = ScanConfig(
        profile_root=root, include_software=False, extra_includes=tuple(extra_includes)
    )
    scan_result = run_scan(config, Environment.fixture(root, {}))
    bundle = tmp_path / "bundle" / "b.dat"
    capture_mod.capture(
        scan_result,
        CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False),
        config,
        Environment.fixture(root, {}),
    )
    destination = tmp_path / "restored"
    destination.mkdir()
    restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    return {f.name for f in destination.rglob("*") if f.is_file()}


def test_firefoxs_password_store_is_never_carried_in_a_profile_copy(tmp_path: Path):
    """Chromium's store is DPAPI-bound and useless off the source machine.
    Firefox's is not.

    ``key4.db`` holds the key that unwraps ``logins.json``, wrapped in turn by
    the primary password -- which almost nobody sets. Carried together to
    another machine they hand over every saved password in plaintext, asking
    nothing of whoever holds the bundle. WinMigrate refuses to write the routine
    that decrypts a password store; a profile copy that ships the store and its
    key is the same outcome by a longer road, and it used to do exactly that,
    while the item's own note told the user the password stores were left out.
    """
    root = tmp_path / "alice"
    firefox_profile_with_stores(root)
    names = restored_names(root, tmp_path)

    assert "places.sqlite" in names and "prefs.js" in names   # the profile did travel
    assert not names & {"logins.json", "logins-backup.json", "key4.db", "cookies.sqlite"}


def test_a_wide_include_pattern_cannot_pull_the_credential_stores_back_in(tmp_path: Path):
    """Every other exclusion is a default the user may overrule. This one is a
    guarantee about what a bundle can contain, so ``--include *.json`` -- aimed
    at some config file and catching logins.json by accident -- must not cancel
    it."""
    root = tmp_path / "alice"
    firefox_profile_with_stores(root)
    names = restored_names(root, tmp_path, extra_includes=["*.json", "key4.db", "*.sqlite"])

    assert "places.sqlite" in names  # the include did work for everything else
    assert not names & {"logins.json", "logins-backup.json", "key4.db", "cookies.sqlite"}


def test_extension_code_and_data_both_survive_a_round_trip(tmp_path: Path):
    """The point of the inventory is that this already worked -- the extensions
    live inside the profile, which is copied whole. This pins it, so a future
    exclusion aimed at browser caches cannot quietly take the extensions or
    their saved settings with it."""
    root = tmp_path / "alice"
    profile = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    profile.mkdir(parents=True)
    (root / "Documents").mkdir(parents=True, exist_ok=True)
    (profile / "Preferences").write_text(json.dumps({"profile": {"name": "P"}}), encoding="utf-8")
    ext_id = "a" * 32
    code = profile / "Extensions" / ext_id / "1.60.0_0"
    code.mkdir(parents=True)
    (code / "manifest.json").write_text(json.dumps({"name": "uBlock Origin", "version": "1.60.0"}), encoding="utf-8")
    (code / "background.js").write_bytes(b"the extension itself")
    settings = profile / "Local Extension Settings" / ext_id
    settings.mkdir(parents=True)
    (settings / "000003.log").write_bytes(b"MY CUSTOM FILTER RULES")
    idb = profile / "IndexedDB" / f"chrome-extension_{ext_id}_0.indexeddb.leveldb"
    idb.mkdir(parents=True)
    (idb / "000005.ldb").write_bytes(b"EXTENSION INDEXEDDB DATA")

    names = restored_names(root, tmp_path)
    assert {"background.js", "manifest.json", "000003.log", "000005.ldb"} <= names


# --- what the browser actually calls a profile ------------------------------
def test_a_renamed_profile_is_called_what_the_browser_calls_it(tmp_path: Path):
    """Chromium keeps two names and they disagree. The one inside the profile's
    own Preferences is what it was created with; renaming it in the browser
    does not change it. The name on screen lives in Local State beside the
    profiles, and that is the one kept up to date.

    Not cosmetic: these names are how the window asks which profile's passwords
    to export, and how the restore says which profile to put them back into."""
    from winmigrate.scan.browsers import _chromium_profile_name

    user_data = tmp_path / "User Data"
    profile = user_data / "Profile 1"
    profile.mkdir(parents=True)
    # Created as "Personal", renamed by its owner to "Demo Work".
    (profile / "Preferences").write_text(
        json.dumps({"profile": {"name": "Personal"}}), encoding="utf-8"
    )
    (user_data / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {"Profile 1": {"name": "Demo Work"}}}}),
        encoding="utf-8",
    )

    assert _chromium_profile_name(profile) == "Demo Work"


def test_a_browser_too_old_for_local_state_still_gets_a_name(tmp_path: Path):
    from winmigrate.scan.browsers import _chromium_profile_name

    profile = tmp_path / "User Data" / "Default"
    profile.mkdir(parents=True)
    (profile / "Preferences").write_text(
        json.dumps({"profile": {"name": "Private"}}), encoding="utf-8"
    )

    assert _chromium_profile_name(profile) == "Private"


def test_a_profile_that_names_itself_nowhere_is_called_by_its_folder(tmp_path: Path):
    from winmigrate.scan.browsers import _chromium_profile_name

    profile = tmp_path / "User Data" / "Profile 3"
    profile.mkdir(parents=True)

    assert _chromium_profile_name(profile) == "Profile 3"


def test_local_state_that_is_not_json_does_not_cost_the_name(tmp_path: Path):
    """It is read off a live machine, where a browser may be mid-write."""
    from winmigrate.scan.browsers import _chromium_profile_name

    user_data = tmp_path / "User Data"
    profile = user_data / "Profile 1"
    profile.mkdir(parents=True)
    (user_data / "Local State").write_text("{ truncated", encoding="utf-8")
    (profile / "Preferences").write_text(
        json.dumps({"profile": {"name": "Personal"}}), encoding="utf-8"
    )

    assert _chromium_profile_name(profile) == "Personal"


# --- the browser's own list of which profiles exist -------------------------
def _brave(profile: Path, folders: dict[str, str], extra: dict | None = None) -> Path:
    """A Brave user-data directory with the given folders and display names."""
    user_data = profile / "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    for folder in folders:
        (user_data / folder).mkdir(parents=True, exist_ok=True)
        (user_data / folder / "Preferences").write_text(
            json.dumps({"profile": {"name": "Person 1"}}), encoding="utf-8"
        )
    state = {
        "profile": {
            "info_cache": {f: {"name": n} for f, n in folders.items()},
            "profiles_order": list(folders),
        }
    }
    state.update(extra or {})
    (user_data / "Local State").write_text(json.dumps(state), encoding="utf-8")
    return user_data


def test_a_second_profile_is_on_disk_and_invisible_without_the_browsers_own_list(tmp_path):
    """The bug, stated as a test. Chromium reads which profiles exist from
    Local State, which sits *beside* the profile folders rather than inside
    one. Restoring only the folders leaves the second profile complete on disk
    and absent from the browser -- "it only copied one of my two profiles"."""
    profile = tmp_path / "alice"
    _brave(profile, {"Default": "private", "Profile 1": "Demo Work"})
    env = Environment.fixture(profile, {})

    items, _followups, _notes = browsers.scan_browsers(env)
    listing = [i for i in items if i.id == "browser:brave:profile_list"]

    assert len(listing) == 1
    record = listing[0].record
    assert sorted(record["profiles"]) == ["Default", "Profile 1"]
    assert record["profiles"]["Profile 1"]["name"] == "Demo Work"
    assert record["user_data"].endswith("Brave-Browser/User Data")


def test_the_password_store_key_is_never_read_let_alone_carried(tmp_path):
    """Local State also holds os_crypt.encrypted_key -- the DPAPI-wrapped key
    for the password and cookie stores. Neither store is captured and the key
    has no business in a bundle either.

    The record is *built* from the profile list rather than filtered out of the
    file, so a future Chromium putting something new in there cannot be carried
    by an oversight."""
    profile = tmp_path / "alice"
    _brave(profile, {"Default": "private"},
           extra={"os_crypt": {"encrypted_key": "SECRET-KEY"}, "browser": {"x": 1}})
    env = Environment.fixture(profile, {})

    items, _f, _n = browsers.scan_browsers(env)
    listing = [i for i in items if i.id == "browser:brave:profile_list"][0]

    assert "SECRET-KEY" not in json.dumps(listing.record)
    assert set(listing.record) == {"user_data", "profiles"}
    # And it is encrypted-only, like the profile folders it describes.
    assert listing.sensitivity is Sensitivity.SECRET


def test_the_list_is_merged_into_the_new_machines_own(tmp_path):
    """The file on the new machine belongs to the browser already installed on
    it: its profiles, its settings, and the wrapped key for its password store.
    Overwriting it would take all three away and hand Chromium a key this
    machine's DPAPI cannot unwrap."""
    destination = tmp_path / "new"
    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    user_data = destination / relative
    for folder in ("Default", "Profile 1"):
        (user_data / folder).mkdir(parents=True)
    (user_data / "Local State").write_text(
        json.dumps({
            "profile": {"info_cache": {"Default": {"name": "Person 1"}},
                        "profiles_order": ["Default"]},
            "os_crypt": {"encrypted_key": "THIS-MACHINES-KEY"},
            "browser": {"its_own_setting": 42},
        }),
        encoding="utf-8",
    )
    record = {
        "user_data": relative,
        "profiles": {"Default": {"name": "private"},
                     "Profile 1": {"name": "Demo Work"}},
    }

    results = apply_mod.apply_browser_profiles(record, destination)

    assert [r.outcome for r in results] == [Outcome.APPLIED]
    state = json.loads((user_data / "Local State").read_text(encoding="utf-8"))
    assert state["os_crypt"]["encrypted_key"] == "THIS-MACHINES-KEY"
    assert state["browser"] == {"its_own_setting": 42}
    assert sorted(state["profile"]["info_cache"]) == ["Default", "Profile 1"]
    # Newer Chromium hides a profile missing from this list even with a good
    # info_cache entry.
    assert state["profile"]["profiles_order"] == ["Default", "Profile 1"]


def test_a_restored_profile_keeps_the_name_the_user_knows_it_by(tmp_path):
    """The new machine made its own "Default" on first run and called it
    "Person 1". Its contents are the old machine's by the time this runs, so
    leaving the label alone restores the profile and not the profile's
    identity."""
    destination = tmp_path / "new"
    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    user_data = destination / relative
    (user_data / "Default").mkdir(parents=True)
    (user_data / "Local State").write_text(
        json.dumps({"profile": {"info_cache": {
            "Default": {"name": "Person 1", "avatar_icon": "one-this-machine-chose"}
        }}}),
        encoding="utf-8",
    )
    record = {"user_data": relative, "profiles": {"Default": {"name": "private"}}}

    apply_mod.apply_browser_profiles(record, destination)

    entry = json.loads((user_data / "Local State").read_text(encoding="utf-8"))["profile"]["info_cache"]["Default"]
    assert entry["name"] == "private"
    # Anything the bundle has no opinion about stays as the new machine set it.
    assert entry["avatar_icon"] == "one-this-machine-chose"


def test_a_profile_that_was_not_restored_is_not_announced_to_the_browser(tmp_path):
    """Listing a folder that is not there gives the user a profile in the
    switcher that opens empty, which is worse than not offering it."""
    destination = tmp_path / "new"
    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    (destination / relative / "Default").mkdir(parents=True)
    record = {
        "user_data": relative,
        "profiles": {"Default": {"name": "private"}, "Profile 9": {"name": "never restored"}},
    }

    apply_mod.apply_browser_profiles(record, destination)

    state = json.loads((destination / relative / "Local State").read_text(encoding="utf-8"))
    assert sorted(state["profile"]["info_cache"]) == ["Default"]


def test_a_machine_with_no_browser_yet_gets_a_list_of_its_own(tmp_path):
    """Restoring before installing the browser is an ordinary order to do it
    in. Chromium fills in the rest of the file itself, and there is nothing
    here to lose."""
    destination = tmp_path / "new"
    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    (destination / relative / "Profile 1").mkdir(parents=True)
    record = {"user_data": relative, "profiles": {"Profile 1": {"name": "Demo Work"}}}

    results = apply_mod.apply_browser_profiles(record, destination)

    assert [r.outcome for r in results] == [Outcome.APPLIED]
    state = json.loads((destination / relative / "Local State").read_text(encoding="utf-8"))
    assert state["profile"]["info_cache"]["Profile 1"]["name"] == "Demo Work"


@pytest.mark.parametrize(
    "record",
    [{}, {"user_data": ""}, {"user_data": "x", "profiles": {}},
     {"user_data": "x", "profiles": "nonsense"}, {"profiles": {"a": {}}}],
)
def test_a_record_shaped_wrongly_costs_the_restore_nothing(record, tmp_path):
    """The record comes out of a bundle, which is a file from another machine."""
    assert apply_mod.apply_browser_profiles(record, tmp_path) == []


def test_an_unreadable_list_on_the_new_machine_is_replaced_not_raised(tmp_path):
    """A half-written Local State -- the browser was killed mid-save -- must
    not take the restore down with it."""
    destination = tmp_path / "new"
    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    (destination / relative / "Default").mkdir(parents=True)
    (destination / relative / "Local State").write_text("{not json", encoding="utf-8")
    record = {"user_data": relative, "profiles": {"Default": {"name": "private"}}}

    results = apply_mod.apply_browser_profiles(record, destination)

    assert [r.outcome for r in results] == [Outcome.APPLIED]
    state = json.loads((destination / relative / "Local State").read_text(encoding="utf-8"))
    assert state["profile"]["info_cache"]["Default"]["name"] == "private"


def test_both_profiles_survive_a_whole_backup_and_restore(tmp_path):
    """The bug end to end, through the real capture and the real restore.

    The unit tests above check the pieces; this checks that the restore
    actually calls the piece that puts the list back. It did not, at first --
    the item was captured, the applier worked, and nothing joined them up, so
    the second profile still arrived invisible.
    """
    source = tmp_path / "old"
    user_data = _brave(source, {"Default": "private", "Profile 1": "Demo Work"},
                       extra={"os_crypt": {"encrypted_key": "OLD-MACHINE-KEY"}})
    for folder in ("Default", "Profile 1"):
        (user_data / folder / "Bookmarks").write_text("{}", encoding="utf-8")

    env = Environment.fixture(source, {})
    config = ScanConfig(profile_root=source, include_software=False)
    scan = run_scan(config, env)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase=PASSPHRASE, use_vss=False), config, env
    )

    destination = tmp_path / "new"
    destination.mkdir()
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase=PASSPHRASE, destination=destination)
    )
    assert report.ok

    relative = "AppData/Local/BraveSoftware/Brave-Browser/User Data"
    state = json.loads(
        (destination / relative / "Local State").read_text(encoding="utf-8")
    )
    assert sorted(state["profile"]["info_cache"]) == ["Default", "Profile 1"]
    assert {f: e["name"] for f, e in state["profile"]["info_cache"].items()} == {
        "Default": "private", "Profile 1": "Demo Work"
    }
    # The report says it happened, because a restore that shows its work is
    # the premise of the tool.
    assert any(r.kind == "browser" and r.ok for r in report.applied)
    # And the old machine's password-store key went nowhere near the bundle.
    assert b"OLD-MACHINE-KEY" not in bundle.read_bytes()


def test_the_password_follow_up_names_the_machine_it_must_be_done_on(tmp_path):
    """A follow-up list is read on the NEW machine. "Settings -> Passwords ->
    Export" with no machine named is an instruction somebody follows on the
    computer in front of them -- exporting the empty store of a browser they
    have just installed, and concluding the migration lost their passwords.

    It cannot lose them: it never had them. But the old machine still does,
    until it is wiped, and that is the part with a deadline on it.
    """
    profile = tmp_path / "alice"
    user_data = _brave(profile, {"Default": "private"})
    (user_data / "Default" / "Preferences").write_text(
        json.dumps({"account_info": [{"email": "a@b.c"}], "sync": {}}), encoding="utf-8"
    )
    env = Environment.fixture(profile, {})

    _items, followups, _notes = browsers.scan_browsers(env)
    password = [f for f in followups if f.category is Category.BROWSER_PASSWORDS]

    assert password, "a profile with sync off must say what happens to its passwords"
    steps = " ".join(password[0].steps).lower()
    assert "old machine" in steps
    assert "while you still have it" in steps
    # And it says plainly that the bundle does not have them, so nobody goes
    # looking for a setting that would bring them back.
    assert "not in this bundle" in password[0].why.lower()


def test_a_synced_profile_is_told_the_opposite_and_correctly(tmp_path):
    """Sync on means the passwords are in the cloud and come down on sign-in.
    Sending that user to the old machine would be busywork."""
    profile = tmp_path / "alice"
    user_data = _brave(profile, {"Default": "private"})
    (user_data / "Default" / "Preferences").write_text(
        json.dumps({"account_info": [{"email": "a@b.c"}],
                    "sync": {"has_setup_completed": True}}),
        encoding="utf-8",
    )
    env = Environment.fixture(profile, {})

    _items, followups, _notes = browsers.scan_browsers(env)
    password = [f for f in followups if f.category is Category.BROWSER_PASSWORDS][0]

    assert "sync down on sign-in" in password.title
    assert "old machine" not in " ".join(password.steps).lower()
