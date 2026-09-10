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
    (prof / "Preferences").write_text(json.dumps(prefs))
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
    csv.write_text("url,username,password\nhttps://x,me,SUPERSECRET\n")
    target = passwords.ExportTarget("chrome", "Google Chrome", "chromium", "chrome://x")
    item = passwords.build_password_item(target, csv)
    assert item.category is Category.BROWSER_PASSWORDS
    assert item.sensitivity is Sensitivity.SECRET
    assert item.archive_path == "secrets/WinMigrate-Passwords/chrome-passwords.csv"
    assert item.kind.value == "file"


def test_the_password_csv_is_redacted_in_the_public_sidecar(tmp_path: Path):
    csv = tmp_path / "pw.csv"
    csv.write_text("url,username,password\nhttps://bank,me,SUPERSECRET\n")
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
    csv.write_text("url,username,password\nhttps://x,me,SECRET\n")
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
    (chrome / "Preferences").write_text('{"profile": {"name": "P"}, "account_info": []}')

    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    scan = run_scan(config, env)

    # The user exports now, after the scan, into the folder the dialog offers.
    csv = profile / "Downloads" / "Chrome Passwords.csv"
    csv.write_text("name,url,username,password\nBank,https://bank.example,alice,hunter2\n")
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
        if f.is_file() and "hunter2" in f.read_text(errors="replace")
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
