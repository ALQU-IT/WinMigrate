"""The browser password handoff: the browser exports, WinMigrate only encrypts.

The property that matters: WinMigrate never reads a password store, only ingests
a CSV the user's own browser produced, and that CSV lives encrypted-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate import manifest as manifest_mod
from winmigrate import passwords
from winmigrate.platform_win import Environment
from winmigrate.models import Category, ScanResult, Sensitivity


def chrome_profile(root: Path, *, sync: bool, email: str | None = "me@example.com") -> Environment:
    prof = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    prof.mkdir(parents=True)
    prefs: dict = {"profile": {"name": "Person 1"}}
    if email:
        prefs["account_info"] = [{"email": email}]
    if sync:
        prefs["sync"] = {"requested": True}
    (prof / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    return Environment.fixture(root, {})


def test_only_browsers_without_sync_are_offered_for_export(tmp_path: Path):
    """A synced browser needs no export -- its passwords come back on sign-in."""
    synced = chrome_profile(tmp_path / "a", sync=True)
    assert passwords.export_targets(synced) == []

    local = chrome_profile(tmp_path / "b", sync=False)
    targets = passwords.export_targets(local)
    assert [t.browser_key for t in targets] == ["chrome"]
    assert targets[0].export_page == "chrome://password-manager/settings"
    assert targets[0].account_email == "me@example.com"


def test_no_browsers_no_targets(tmp_path: Path):
    (tmp_path / "Documents").mkdir()
    assert passwords.export_targets(Environment.fixture(tmp_path, {})) == []


def test_a_real_password_export_is_recognised():
    assert passwords.looks_like_password_csv("name,url,username,password,note\nS,https://x,me,pw,\n")
    assert passwords.looks_like_password_csv("url,username,password\nhttps://x,me,pw\n")
    # BOM-prefixed header (browsers write these).
    assert passwords.looks_like_password_csv("﻿url,username,password\n")


def test_a_file_that_is_not_a_password_csv_is_rejected():
    assert not passwords.looks_like_password_csv("just some notes\n")
    assert not passwords.looks_like_password_csv("name,url,note\n")  # no username/password
    assert not passwords.looks_like_password_csv("")


def test_a_password_item_is_secret_and_never_parsed(tmp_path: Path):
    csv = tmp_path / "pw.csv"
    csv.write_text("url,username,password\nhttps://x,me,SUPERSECRET\n", encoding="utf-8")
    target = passwords.ExportTarget("chrome", "Google Chrome", "chromium", "chrome://x")
    item = passwords.build_password_item(target, csv)
    assert item.category is Category.BROWSER_PASSWORDS
    assert item.sensitivity is Sensitivity.SECRET
    assert item.archive_path == "secrets/WinMigrate-Passwords/chrome-passwords.csv"
    assert item.kind.value == "file"


def test_the_password_csv_is_redacted_in_the_public_sidecar(tmp_path: Path):
    csv = tmp_path / "pw.csv"
    csv.write_text("url,username,password\nhttps://bank,me,SUPERSECRET\n", encoding="utf-8")
    target = passwords.ExportTarget("chrome", "Google Chrome", "chromium", "chrome://x")
    result = ScanResult(source=manifest_mod.detect_source_machine(str(tmp_path)))
    result.items.append(passwords.build_password_item(target, csv))
    public = manifest_mod.public_view(manifest_mod.build(result))
    blob = json.dumps(public)
    assert "SUPERSECRET" not in blob
    assert str(csv) not in blob
    stub = public["items"][0]
    assert stub["redacted"] is True
    assert "source_path" not in stub


def test_the_import_followup_names_the_file_and_says_to_delete_it():
    target = passwords.ExportTarget("chrome", "Google Chrome", "chromium",
                                    "chrome://password-manager/passwords")
    followup = passwords.import_followup(target)
    assert followup.id == "browser:passwords:chrome"
    assert "chrome-passwords.csv" in " ".join(followup.steps)
    assert any("delete" in step.lower() for step in followup.steps)


def test_shred_overwrites_and_removes(tmp_path: Path):
    csv = tmp_path / "pw.csv"
    csv.write_text("url,username,password\nhttps://x,me,SECRET\n", encoding="utf-8")
    assert passwords.shred(csv) is True
    assert not csv.exists()


def test_shred_of_a_missing_file_is_false(tmp_path: Path):
    assert passwords.shred(tmp_path / "nope.csv") is False


def test_every_known_browser_has_an_export_page():
    from winmigrate.scan import browsers

    for browser in browsers.CHROMIUM_BROWSERS:
        assert browser.key in passwords.EXPORT_PAGES
    assert "firefox" in passwords.EXPORT_PAGES


def test_the_exported_csv_does_not_also_travel_as_an_ordinary_file(tmp_path: Path):
    """Chrome's export dialog defaults to Downloads, which is captured.

    The CSV was then in the bundle twice: once as the encrypted-only item, and
    once as a plain user file under data/user_files/Downloads. The source copy
    gets shredded and the follow-up says to delete the CSV -- meaning the one in
    WinMigrate-Passwords -- so the second copy restored to Downloads on the new
    machine and stayed there, plaintext, with nothing pointing at it.
    """
    from winmigrate import capture as capture_mod
    from winmigrate import passwords as passwords_mod
    from winmigrate import restore as restore_mod
    from winmigrate.capture import CaptureOptions
    from winmigrate.config import ScanConfig
    from winmigrate.platform_win import Environment
    from winmigrate.restore import RestoreOptions
    from winmigrate.scan import run_scan

    profile = tmp_path / "alice"
    (profile / "Downloads").mkdir(parents=True)
    (profile / "Downloads" / "installer.exe").write_bytes(b"x" * 100)
    chrome = profile / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    chrome.mkdir(parents=True)
    (chrome / "Preferences").write_text('{"profile": {"name": "P"}, "account_info": []}', encoding="utf-8")

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)

    # The user exports now, after the scan, into the folder the dialog offers.
    csv = profile / "Downloads" / "Chrome Passwords.csv"
    csv.write_text("name,url,username,password\nBank,https://bank.example,alice,hunter2\n", encoding="utf-8")
    target = next(iter(passwords_mod.export_targets(env)))
    scan.items.append(passwords_mod.build_password_item(target, csv))

    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase="pw", use_vss=False), config, env
    )
    csv.unlink()  # shredded on the source machine, as the flow does

    destination = tmp_path / "dest"
    destination.mkdir()
    result = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase="pw", destination=destination)
    )
    assert result.ok
    holding = {
        str(f.relative_to(destination))
        for f in destination.rglob("*")
        if f.is_file() and "hunter2" in f.read_text(errors="replace", encoding="utf-8")
    }
    # Exactly one copy, in the one place the follow-up tells the user to clear.
    assert holding == {str(Path("WinMigrate-Passwords") / "chrome-passwords.csv")}
    assert (destination / "Downloads" / "installer.exe").is_file()  # the rest still travelled


def test_the_export_page_is_opened_with_the_browser_not_the_windows_shell(tmp_path: Path):
    r"""brave://, chrome://, edge:// and about: are internal browser schemes.

    Windows has no handler registered for any of them -- they mean something
    only inside the browser that defines them. Handing one to the shell
    ("start \"\" brave://...") therefore does not open Brave: Windows hunts for
    an app claiming a "brave" protocol, finds none, and offers the Microsoft
    Store. That was true of every browser here, not just the first one tried.

    The browser's own executable is resolved from App Paths -- HKCU as well as
    HKLM, because Chrome installs per-user by default -- and launched with the
    URL as an argument, which is the only thing that can resolve it.
    """
    from winmigrate import passwords as passwords_mod

    exe = tmp_path / "Brave-Browser" / "Application" / "brave.exe"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"MZ")
    key = r"Software\Microsoft\Windows\CurrentVersion\App Paths\brave.exe"
    target = passwords_mod.ExportTarget(
        browser_key="brave",
        title="Brave",
        engine="chromium",
        export_page="brave://password-manager/settings",
    )

    # Machine-wide install.
    env = Environment.fixture(tmp_path, {f"HKLM\\{key}": {"": str(exe)}})
    assert passwords_mod.browser_executable(target, env) == exe

    # Per-user install, quoted as the registry often holds it.
    env = Environment.fixture(tmp_path, {f"HKCU\\{key}": {"": f'"{exe}"'}})
    assert passwords_mod.browser_executable(target, env) == exe

    # Registered but no longer installed: no path, so nothing is launched.
    env = Environment.fixture(tmp_path, {f"HKLM\\{key}": {"": str(tmp_path / "gone.exe")}})
    assert passwords_mod.browser_executable(target, env) is None

    # Not registered at all.
    assert passwords_mod.browser_executable(target, Environment.fixture(tmp_path, {})) is None


def test_every_browser_offered_for_export_has_an_executable_to_launch(tmp_path: Path):
    """A browser whose password page we advertise but cannot open would send the
    user back to the Microsoft Store dialog this replaced."""
    from winmigrate import passwords as passwords_mod

    assert set(passwords_mod.EXPORT_PAGES) <= set(passwords_mod.BROWSER_EXECUTABLES)


def test_each_browser_lands_on_the_page_that_has_the_export_control():
    """Chromium's password manager splits the list from the controls.

    ``.../password-manager/passwords`` shows saved entries and no export button;
    "Export passwords" and "Import passwords" both live on
    ``.../password-manager/settings``. Aiming at the list means the user still
    has to find Settings in the sidebar -- the exact step opening the page for
    them was meant to remove. Firefox has no deeper URL: both sit behind the
    "..." menu on about:logins.
    """
    from winmigrate import passwords as passwords_mod

    for key, page in passwords_mod.EXPORT_PAGES.items():
        if key == "firefox":
            assert page == "about:logins"
            continue
        assert page.endswith("/settings") or page.endswith("/settings/passwords"), (
            f"{key} points at {page}, which is not where the export control is"
        )
        # And it must use a scheme that browser actually resolves.
        assert "://" in page and not page.startswith("http")


def test_the_page_is_shown_to_the_user_whether_or_not_the_browser_opened(tmp_path: Path):
    """A browser that opens somewhere unexpected is the failure this cannot
    detect, so the address is printed either way rather than only on launch
    failure -- finding out after the browser opened is too late."""
    import inspect

    from winmigrate import cli

    source = inspect.getsource(cli._collect_browser_passwords)
    # One print, outside the if/else, carrying the address.
    assert source.count("target.export_page") == 1
    assert "lead = (" in source


# --- one staging path, shared by the window and the command line ------------
def test_the_cloud_half_is_reported_as_well_as_the_local_half(tmp_path: Path):
    """export_targets and synced_browsers between them account for every browser
    found. A page that could only list the first would look broken on the
    machines -- most of them -- where everything is already signed in."""
    synced = chrome_profile(tmp_path / "a", sync=True)
    accounts = passwords.synced_browsers(synced)
    assert [(a.browser_key, a.account_email) for a in accounts] == [
        ("chrome", "me@example.com")
    ]
    assert passwords.export_targets(synced) == []

    local = chrome_profile(tmp_path / "b", sync=False)
    assert passwords.synced_browsers(local) == []
    assert [t.browser_key for t in passwords.export_targets(local)] == ["chrome"]


def test_ingesting_a_csv_stages_it_encrypted_only_and_answers_the_followup(tmp_path: Path):
    from winmigrate.models import Followup

    env = chrome_profile(tmp_path / "p", sync=False)
    target = passwords.export_targets(env)[0]
    csv = tmp_path / "chrome-passwords.csv"
    csv.write_text("name,url,username,password,note\nS,https://x,me,pw,\n", encoding="utf-8")

    scan = ScanResult(source=None)
    scan.followups = [
        Followup(
            id="browser:passwords:chrome",
            title="Export your Chrome passwords",
            why="Sync is off, so they live only on this machine.",
        )
    ]

    outcome = passwords.ingest_csv(target, csv, scan)

    assert outcome.ok and outcome.item is not None
    assert outcome.item.sensitivity is Sensitivity.SECRET
    assert outcome.item.archive_path.startswith("secrets/")
    # The scan-time "export these yourself" note is answered by the import one.
    assert [f.title for f in scan.followups] == [
        "Import your Google Chrome passwords, then delete the file"
    ]


def test_pointing_at_a_second_file_replaces_the_first_rather_than_both(tmp_path: Path):
    """The usual reason to choose again is having picked the wrong file. Two
    plaintext exports of one browser is the last thing this should quietly
    produce."""
    env = chrome_profile(tmp_path / "p", sync=False)
    target = passwords.export_targets(env)[0]
    scan = ScanResult(source=None)
    header = "url,username,password\nhttps://x,me,pw\n"
    first = tmp_path / "wrong.csv"
    first.write_text(header, encoding="utf-8")
    second = tmp_path / "right.csv"
    second.write_text(header, encoding="utf-8")

    passwords.ingest_csv(target, first, scan)
    passwords.ingest_csv(target, second, scan)

    staged = [item for item in scan.items if item.category is Category.BROWSER_PASSWORDS]
    assert len(staged) == 1
    assert staged[0].source_path == str(second)
    assert len([f for f in scan.followups if f.id == "browser:passwords:chrome"]) == 1


def test_a_file_that_is_not_an_export_is_refused_with_a_reason(tmp_path: Path):
    env = chrome_profile(tmp_path / "p", sync=False)
    target = passwords.export_targets(env)[0]
    scan = ScanResult(source=None)
    notes = tmp_path / "notes.txt"
    notes.write_text("shopping list\n", encoding="utf-8")

    outcome = passwords.ingest_csv(target, notes, scan)

    assert not outcome.ok and outcome.item is None
    assert "url, username and password" in outcome.message
    assert scan.items == []

    missing = passwords.ingest_csv(target, tmp_path / "nothing.csv", scan)
    assert not missing.ok and scan.items == []


# --- finding the browser to open -------------------------------------------
def test_the_browser_is_found_where_it_installs_itself_when_app_paths_is_silent(
    tmp_path: Path,
):
    """App Paths usually answers, but not always -- a stale key, an install
    done for another user. Missing means the button does nothing and the user
    is told to navigate there themselves, which is the one job it had."""
    program_files = tmp_path / "Program Files"
    brave = program_files / "BraveSoftware" / "Brave-Browser" / "Application" / "brave.exe"
    brave.parent.mkdir(parents=True)
    brave.write_bytes(b"MZ")

    env = Environment.fixture(
        tmp_path / "profile", {}, {"ProgramFiles": str(program_files)}
    )
    target = passwords.ExportTarget(
        browser_key="brave",
        title="Brave",
        engine="chromium",
        export_page="brave://password-manager/settings",
    )

    assert passwords.browser_executable(target, env) == brave


def test_the_registry_still_wins_when_it_has_an_answer(tmp_path: Path):
    """App Paths is what Windows itself uses; a portable or relocated install is
    only in there."""
    installed = tmp_path / "Elsewhere" / "brave.exe"
    installed.parent.mkdir(parents=True)
    installed.write_bytes(b"MZ")
    standard = tmp_path / "Program Files" / "BraveSoftware" / "Brave-Browser" / "Application"
    standard.mkdir(parents=True)
    (standard / "brave.exe").write_bytes(b"MZ")

    env = Environment.fixture(
        tmp_path / "profile",
        {
            f"HKLM\\{passwords.APP_PATHS_KEY}\\brave.exe": {"": f'"{installed}"'},
        },
        {"ProgramFiles": str(tmp_path / "Program Files")},
    )
    target = passwords.ExportTarget(
        browser_key="brave", title="Brave", engine="chromium", export_page="x"
    )

    assert passwords.browser_executable(target, env) == installed


def test_a_browser_that_is_not_installed_is_not_invented(tmp_path: Path):
    env = Environment.fixture(tmp_path / "profile", {}, {"ProgramFiles": str(tmp_path / "pf")})
    target = passwords.ExportTarget(
        browser_key="brave", title="Brave", engine="chromium", export_page="x"
    )
    assert passwords.browser_executable(target, env) is None


# --- one browser, several profiles ------------------------------------------
def chrome_profiles(root: Path, profiles: dict) -> Environment:
    """A Chrome with several profiles: {folder: (display name, sync, email)}."""
    user_data = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    for folder, (name, sync, email) in profiles.items():
        directory = user_data / folder
        directory.mkdir(parents=True)
        prefs: dict = {"profile": {"name": name}}
        if email:
            prefs["account_info"] = [{"email": email}]
        if sync:
            prefs["sync"] = {"requested": True}
        (directory / "Preferences").write_text(json.dumps(prefs), encoding="utf-8")
    return Environment.fixture(root, {})


def test_every_local_profile_is_offered_not_just_the_first(tmp_path: Path):
    """People keep work and personal profiles in one browser, and everything
    about passwords is per profile: the store, the export dialog, whether sync
    is on. One row per browser silently left every profile but one behind."""
    env = chrome_profiles(
        tmp_path,
        {
            "Default": ("Personal", False, "me@home.test"),
            "Profile 2": ("Work", False, "me@work.test"),
        },
    )

    targets = passwords.export_targets(env)

    assert [t.key for t in targets] == ["chrome", "chrome:profile-2"]
    assert [t.label for t in targets] == ["Google Chrome — Personal", "Google Chrome — Work"]
    # Each carries its own account, not the first one found for the browser.
    assert [t.account_email for t in targets] == ["me@home.test", "me@work.test"]


def test_a_synced_profile_no_longer_hides_a_local_one(tmp_path: Path):
    """The worse half of the same bug: any synced profile skipped the whole
    browser, so the local profile's passwords were never offered at all."""
    env = chrome_profiles(
        tmp_path,
        {
            "Default": ("Personal", True, "me@home.test"),
            "Profile 2": ("Work", False, "me@work.test"),
        },
    )

    assert [t.key for t in passwords.export_targets(env)] == ["chrome:profile-2"]
    assert [(a.label, a.account_email) for a in passwords.synced_browsers(env)] == [
        ("Google Chrome — Personal", "me@home.test")
    ]


def test_two_profiles_produce_two_items_rather_than_one_on_top_of_the_other(tmp_path: Path):
    """Same browser, same file name, one overwriting the other in the bundle --
    and the second export would have looked like it worked."""
    env = chrome_profiles(
        tmp_path,
        {"Default": ("Personal", False, None), "Profile 2": ("Work", False, None)},
    )
    personal, work = passwords.export_targets(env)
    scan = ScanResult(source=None)
    body = "url,username,password\nhttps://x,me,pw\n"
    for name, target in (("personal.csv", personal), ("work.csv", work)):
        csv = tmp_path / name
        csv.write_text(body, encoding="utf-8")
        assert passwords.ingest_csv(target, csv, scan).ok

    staged = [item for item in scan.items if item.category is Category.BROWSER_PASSWORDS]
    assert [item.id for item in staged] == [
        "browser:passwords-csv:chrome",
        "browser:passwords-csv:chrome:profile-2",
    ]
    assert [item.archive_path for item in staged] == [
        "secrets/WinMigrate-Passwords/chrome-passwords.csv",
        "secrets/WinMigrate-Passwords/chrome-profile-2-passwords.csv",
    ]
    # And each answers its own follow-up, not the other's.
    assert sorted(f.id for f in scan.followups) == [
        "browser:passwords:chrome",
        "browser:passwords:chrome:profile-2",
    ]


def test_the_usual_one_profile_machine_is_named_exactly_as_before(tmp_path: Path):
    """Nearly every machine. The profile suffix must not appear where it
    distinguishes nothing, or every existing bundle's file names change for no
    reason."""
    env = chrome_profile(tmp_path / "p", sync=False)
    target = passwords.export_targets(env)[0]
    csv = tmp_path / "pw.csv"
    csv.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")
    scan = ScanResult(source=None)

    item = passwords.ingest_csv(target, csv, scan).item

    assert target.key == "chrome" and target.label == "Google Chrome"
    assert item.id == "browser:passwords-csv:chrome"
    assert item.archive_path == "secrets/WinMigrate-Passwords/chrome-passwords.csv"


def test_the_browser_is_opened_in_the_profile_the_passwords_are_in(tmp_path: Path):
    """Chromium opens whichever profile it used last, and its export dialog only
    ever exports the profile whose window it is in. The folder name is the one
    identifier that survives someone renaming a profile."""
    env = chrome_profiles(
        tmp_path,
        {"Default": ("Personal", False, None), "Profile 2": ("Work", False, None)},
    )
    personal, work = passwords.export_targets(env)

    assert passwords.launch_arguments(work) == [
        "--profile-directory=Profile 2",
        "chrome://settings/",
    ]
    assert passwords.launch_arguments(personal) == [
        "--profile-directory=Default",
        "chrome://settings/",
    ]

    # Firefox picks its profile at startup and will not switch while running,
    # so there is nothing honest to pass.
    firefox = passwords.ExportTarget(
        browser_key="firefox",
        title="Mozilla Firefox",
        engine="firefox",
        export_page="about:logins",
        profile_id="abc.default-release",
    )
    assert passwords.launch_arguments(firefox) == ["about:logins"]


def test_a_chromium_is_sent_to_the_one_page_of_its_own_it_will_accept(tmp_path: Path):
    """Chromium drops an internal address that came from another program --
    every one of them except the settings root. Handing it
    "chrome://password-manager/settings" therefore starts the browser, ignores
    the address and shows the new tab page, which is what "it opens the browser
    but it doesn't go to the link" looked like. So it is sent one click short,
    to settings, and the real address goes on the clipboard."""
    env = chrome_profiles(tmp_path, {"Default": ("Personal", False, None)})
    (target,) = passwords.export_targets(env)

    assert passwords.landing_page(target) == "chrome://settings/"
    assert passwords.launch_arguments(target)[-1] == "chrome://settings/"
    assert passwords.opens_directly(target) is False
    # The address we show, copy and write into the restore instructions is
    # still the page the user actually needs.
    assert target.export_page == "chrome://password-manager/settings"


def test_firefox_goes_to_its_password_page_because_firefox_allows_it():
    """The restriction is Chromium's, not every browser's, and pretending
    otherwise would add a step Firefox does not need."""
    firefox = passwords.ExportTarget(
        browser_key="firefox",
        title="Mozilla Firefox",
        engine="firefox",
        export_page="about:logins",
    )

    assert passwords.landing_page(firefox) == "about:logins"
    assert passwords.opens_directly(firefox) is True


def test_a_rebranded_chromium_is_given_both_spellings_of_the_one_page():
    """Chromium matches the address against a single constant its fork rebrands
    -- "brave://settings/" in Brave, "chrome://settings/" upstream -- and which
    one a build kept cannot be known from out here. The one that does not match
    is discarded before it becomes a tab, so offering both costs a user
    nothing and getting it wrong costs them the whole feature."""
    brave = passwords.ExportTarget(
        browser_key="brave",
        title="Brave",
        engine="chromium",
        export_page="brave://password-manager/settings",
        profile_id="Default",
    )

    assert passwords.launch_arguments(brave) == [
        "--profile-directory=Default",
        "brave://settings/",
        "chrome://settings/",
    ]
    # Chrome's two spellings are the same one, so it is passed once.
    chrome = passwords.ExportTarget(
        browser_key="chrome",
        title="Google Chrome",
        engine="chromium",
        export_page="chrome://password-manager/settings",
    )
    assert passwords.launch_arguments(chrome) == ["chrome://settings/"]

    # Firefox has no such filter and no such fork; it gets its own page, once.
    firefox = passwords.ExportTarget(
        browser_key="firefox", title="Mozilla Firefox", engine="firefox",
        export_page="about:logins",
    )
    assert passwords.launch_arguments(firefox) == ["about:logins"]


def test_a_chromium_fork_we_have_never_heard_of_still_lands_in_its_settings():
    """Every Chromium rebrands the scheme and keeps the page. Taking the scheme
    from the address we wanted beats sending a fork to a blank tab."""
    fork = passwords.ExportTarget(
        browser_key="arc",
        title="Arc",
        engine="chromium",
        export_page="arc://password-manager/settings",
        profile_id="Default",
    )

    assert passwords.landing_page(fork) == "arc://settings/"
    assert passwords.launch_arguments(fork) == [
        "--profile-directory=Default",
        "arc://settings/",
        "chrome://settings/",
    ]
