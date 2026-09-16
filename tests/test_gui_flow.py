"""Driving the real window, page by page, on stub widgets.

Everything else about the GUI is checked by reading it. This runs it. The two
bugs it found on the first attempt had both survived a careful read: a rail that
described the previous mode rather than the current one, and a passphrase field
emptied while the button that needed it stayed live.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from guistub import open_window, pump, rail, written
from winmigrate import capture as capture_mod
from winmigrate import restore as restore_mod
from winmigrate.capture import CaptureOptions
from winmigrate.config import ScanConfig
from winmigrate.gui.wizard import Mode, Step
from winmigrate.platform_win import Environment
from winmigrate.scan import run_scan


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    root = tmp_path / "Users" / "alice"
    for folder in ("Documents", "Pictures", "Music"):
        (root / folder).mkdir(parents=True)
        (root / folder / "f.bin").write_bytes(folder.encode() * 200)
    return root


@pytest.fixture
def bundle(profile: Path, tmp_path: Path) -> Path:
    env = Environment.fixture(profile, {})
    config = ScanConfig(profile_root=profile, include_software=False)
    path = tmp_path / "backup.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=path, passphrase="pw", use_vss=False),
        config,
        env,
    )
    return path


# --- the rail ---------------------------------------------------------------
def test_the_rail_follows_the_mode_rather_than_trailing_it(monkeypatch):
    """It was painted from a mode read before the widgets were collected, so it
    described the *previous* choice. Picking Restore and coming back to Backup
    left a rail carrying the restore labels, one entry still hidden, and
    "Finish" appearing twice.
    """
    wizard, _ = open_window(monkeypatch)
    assert rail(wizard) == ["●  Start", "○  Choose", "○  Passwords", "○  Sign-ins", "○  Where to", "○  Check", "○  Done"]

    # Choosing on the first page repaints immediately, without waiting for the
    # page to change.
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._refresh_buttons()
    assert rail(wizard) == ["●  Backup", "○  Choose", "○  Check", "○  Programs", "○  Done"]

    wizard.mode_var.set(Mode.BACKUP.value)
    wizard._show(Step.CHOOSE)
    assert rail(wizard) == ["●  Start", "○  Choose", "○  Passwords", "○  Sign-ins", "○  Where to", "○  Check", "○  Done"]


def test_the_rail_marks_progress_as_the_pages_advance(monkeypatch):
    wizard, _ = open_window(monkeypatch)
    wizard._show(Step.DESTINATION)
    assert rail(wizard)[:5] == [
        "✓  Start", "✓  Choose", "✓  Passwords", "✓  Sign-ins", "●  Where to",
    ]


# --- the backup branch ------------------------------------------------------
def test_a_backup_runs_from_the_first_page_to_the_last(monkeypatch, profile: Path, tmp_path: Path):
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)

    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    assert wizard.step is Step.SELECT
    assert wizard.data.rows and wizard.data.selected

    output = tmp_path / "out.dat"
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")

    wizard._show(Step.DESTINATION)
    assert wizard.next_button.state == "normal"
    wizard._show(Step.CONFIRM)
    assert str(output) in wizard.confirm_text.cget("text")

    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"
    assert wizard.step is Step.DONE
    assert output.is_file()
    assert output.name in wizard.done_text.cget("text")


def test_the_forward_button_refuses_an_empty_selection(monkeypatch, profile: Path):
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    pump(wizard)

    wizard._set_all(False)
    wizard._refresh_buttons()
    assert wizard.next_button.state == "disabled"
    assert "nothing" in wizard.hint.cget("text").lower()

    wizard._set_all(True)
    wizard._refresh_buttons()
    assert wizard.next_button.state == "normal"


# --- the restore branch -----------------------------------------------------
def test_a_restore_runs_from_the_first_page_to_the_last(
    monkeypatch, bundle: Path, tmp_path: Path
):
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")

    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    assert wizard.step is Step.RESTORE_SELECT
    assert {row.item_id for row in wizard.data.restore_rows} >= {
        "files:documents", "files:pictures", "files:music"
    }

    destination = tmp_path / "restored"
    destination.mkdir()
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORE_CONFIRM)
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"
    assert wizard.step is Step.RESTORE_DONE
    assert {p.parent.name for p in destination.rglob("*.bin")} == {
        "Documents", "Pictures", "Music"
    }


def test_the_wrong_passphrase_returns_to_the_field_rather_than_a_dead_end(
    monkeypatch, bundle: Path
):
    """It is nearly always what was wrong, so the page it goes back to is the
    one with the field on it."""
    wizard, shown = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "not it")

    wizard._show(Step.OPENING)
    assert pump(wizard) == "open-failed"
    assert wizard.step is Step.SOURCE
    assert shown and "passphrase" in shown[-1][1][1].lower()


def test_unticking_an_item_keeps_it_off_the_machine(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """The whole point of the choosing page. If the ids did not line up with
    what restore expects, everything would come back and it would look like it
    had worked."""
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    pump(wizard)

    wizard.data.restore_selected = {"files:documents"}
    destination = tmp_path / "some"
    destination.mkdir()
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORE_CONFIRM)
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"
    assert {p.parent.name for p in destination.rglob("*.bin")} == {"Documents"}


def test_a_practice_run_reaches_the_options_and_writes_nothing(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """A practice run that created the destination would not be one."""
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    pump(wizard)

    seen: dict = {}
    real = restore_mod.restore

    def spy(options, progress=None):
        seen["dry_run"] = options.dry_run
        seen["overwrite"] = options.overwrite
        return real(options, progress)

    monkeypatch.setattr(restore_mod, "restore", spy)

    target = tmp_path / "never-created"
    wizard.destination_var.set(str(target))
    wizard.dry_run_var.set(True)
    wizard._show(Step.RESTORE_CONFIRM)
    assert "Practice run" in wizard.restore_summary.cget("text")
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"

    assert seen["dry_run"] is True
    assert not target.exists()
    assert "practice run" in wizard.restore_done_text.cget("text").lower()


# --- the passphrase ---------------------------------------------------------
def test_the_passphrase_survives_long_enough_to_retry_and_no_longer(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """It was cleared the moment the worker had it, which looked careful and set
    a trap. A restore that fails sends the user back to try again -- and the
    field was empty, the button still live, and the retry failed with "the
    passphrase is wrong, or the file has been altered or corrupted". Neither was
    true, and it sends someone to check their backup for a fault that is not
    there.
    """
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    pump(wizard)

    destination = tmp_path / "out"
    destination.mkdir()
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORE_CONFIRM)
    # Still held while the job is in front of the user.
    assert wizard.bundle_passphrase.get() == "pw"
    assert wizard.next_button.state == "normal"

    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"

    # Dropped once the job is genuinely over.
    assert wizard.bundle_passphrase.get() == ""
    assert wizard.data.bundle_passphrase == ""

    # And the button will not send an empty one back through.
    second = tmp_path / "again"
    second.mkdir()
    wizard.destination_var.set(str(second))
    wizard._show(Step.RESTORE_CONFIRM)
    assert wizard.next_button.state == "disabled"
    assert "passphrase" in wizard.hint.cget("text").lower()


def test_the_backup_passphrase_is_dropped_the_same_way(
    monkeypatch, profile: Path, tmp_path: Path
):
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    pump(wizard)

    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.CONFIRM)
    assert wizard.passphrase.get() == "hunter2"

    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"
    assert wizard.passphrase.get() == "" and wizard.passphrase2.get() == ""


# --- what it says about a file before anyone types anything -----------------
def test_a_file_that_is_not_a_backup_is_described_as_such(monkeypatch, tmp_path: Path):
    wizard, _ = open_window(monkeypatch)
    junk = tmp_path / "holiday.dat"
    junk.write_bytes(b"not a bundle at all")
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(junk))
    wizard._describe_bundle()
    assert "does not look like a backup" in wizard.bundle_summary.cget("text")


def test_a_real_backup_is_described_before_the_passphrase_is_asked_for(
    monkeypatch, bundle: Path
):
    """The header and sidecar are plaintext by design, so when it was made and
    which machine it came from can be shown before anyone commits to typing."""
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(bundle))
    wizard._describe_bundle()
    summary = wizard.bundle_summary.cget("text")
    assert "Made" in summary and "item(s)" in summary and "intact" in summary


def test_a_path_that_is_not_there_blocks_the_button_with_a_reason(
    monkeypatch, tmp_path: Path
):
    wizard, _ = open_window(monkeypatch)
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set(str(tmp_path / "gone.dat"))
    wizard._refresh_buttons()
    assert wizard.next_button.state == "disabled"
    assert "not there" in wizard.hint.cget("text")


# --- the log ----------------------------------------------------------------
def test_the_window_says_where_its_log_is_while_it_is_still_running(
    monkeypatch, profile: Path, tmp_path: Path
):
    """A log nobody can find is worth as much as no log. The moment someone
    wants it is the moment something looks wrong -- which is in the middle of a
    capture, not after it -- so the rail carries it on every page, and the last
    page carries the whole path.
    """
    log_path = tmp_path / "E_drive" / "winmigrate-20260914-120000.log"
    log_path.parent.mkdir(parents=True)
    wizard, _ = open_window(
        monkeypatch, {"profile_root": str(profile), "log_path": str(log_path)}
    )
    wizard.use_vss.set(False)

    assert log_path.name in wizard.log_label.cget("text")

    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    output = tmp_path / "out.dat"
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert str(log_path) in wizard.done_text.cget("text")


def test_a_window_without_a_log_says_nothing_rather_than_something_empty(
    monkeypatch, profile: Path
):
    """No candidate folder would take a file. The window still opens and works;
    it just has no log to point at, and must not point at one anyway."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})

    assert wizard.log_path is None
    assert wizard.log_label.cget("text") == ""


def test_the_run_is_written_down_as_it_happens(monkeypatch, profile: Path, tmp_path: Path):
    """The log is the answer to "it looked stuck": which phase was running, on
    what, to where. Checked by capturing what the window actually logs during a
    real backup rather than by reading the call sites."""
    import logging

    records: list[str] = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    handler = Collect()
    logger = logging.getLogger("winmigrate.gui.app")
    logger.addHandler(handler)
    # What a real run gets from logging_setup.configure(), which the window
    # calls before it opens: everything reaches the file, warnings reach the eye.
    # setLevel, not logger.level = ...: the attribute alone leaves logging's
    # own is-this-enabled cache holding the old answer, and the records vanish.
    previous = logger.level
    logger.setLevel(logging.DEBUG)
    try:
        wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
        wizard.use_vss.set(False)
        wizard._show(Step.SCANNING)
        assert pump(wizard) == "scanned"
        output = tmp_path / "out.dat"
        wizard.output_var.set(str(output))
        wizard.passphrase.insert(0, "hunter2")
        wizard.passphrase2.insert(0, "hunter2")
        wizard._show(Step.WORKING)
        assert pump(wizard) == "captured"
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)

    written = "\n".join(records)
    assert "scan started" in written and "scan finished" in written
    assert "capture started" in written and "capture finished" in written
    assert str(output) in written
    # The passphrase was typed into this run. It is not in what was written down.
    assert "hunter2" not in written


# --- browser passwords ------------------------------------------------------
def chrome_with_local_passwords(root: Path) -> None:
    """A Chrome profile that is signed in but not syncing: passwords are local."""
    import json

    folder = root / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    folder.mkdir(parents=True)
    (folder / "Preferences").write_text(
        json.dumps({"profile": {"name": "Person 1"}, "account_info": [{"email": "me@x.test"}]}),
        encoding="utf-8",
    )


def test_the_window_offers_the_password_handoff_the_command_line_had(
    monkeypatch, profile: Path
):
    """The page existed only on the command line, so someone using the window
    was never asked about browser passwords at all -- the migration silently
    left them behind."""
    chrome_with_local_passwords(profile)
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})

    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    assert [t.browser_key for t in wizard.password_targets] == ["chrome"]
    assert "chrome" in wizard.password_rows
    assert "export" in wizard.passwords_intro.cget("text").lower()


def test_an_export_chosen_after_the_choosing_page_still_reaches_the_bundle(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The item is created two pages after the list was built. Without being
    added to the selection it would be marked "deselected" at capture time and
    silently left out -- the one item the user went furthest out of their way to
    include."""
    from winmigrate import restore as restore_mod
    from winmigrate.restore import RestoreOptions

    chrome_with_local_passwords(profile)
    export = tmp_path / "Chrome Passwords.csv"
    export.write_text(
        "name,url,username,password,note\nS,https://secretsite.test,me,swordfish,\n",
        encoding="utf-8",
    )

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"

    wizard._show(Step.PASSWORDS)
    wizard._ingest_password_csv("chrome", export)
    assert wizard.password_status["chrome"].cget("text").startswith("Google Chrome passwords added")
    # Not deleted yet: the plaintext file is the user's until the bundle holds it.
    assert export.is_file()

    output = tmp_path / "out.dat"
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.CONFIRM)
    assert "Google Chrome" in wizard.confirm_text.cget("text")

    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    # In the bundle, and only in the bundle. The plaintext sidecar -- which
    # travels beside the .dat and needs no passphrase to read -- carries a
    # redacted stub and none of what the export contained.
    sidecar = json.dumps(restore_mod.load_sidecar(output))
    assert "swordfish" not in sidecar and "secretsite" not in sidecar
    entry = next(
        item
        for item in restore_mod.load_sidecar(output)["items"]
        if item["id"] == "browser:passwords-csv:chrome"
    )
    assert entry["redacted"] is True and entry["sensitivity"] == "secret"

    destination = tmp_path / "new"
    destination.mkdir()
    report = restore_mod.restore(
        RestoreOptions(bundle=output, passphrase="hunter2", destination=destination)
    )
    assert report.ok
    restored = destination / "WinMigrate-Passwords" / "chrome-passwords.csv"
    assert restored.is_file()
    assert "swordfish" in restored.read_text(encoding="utf-8")
    # And the follow-up now tells the new machine to import it and delete it.
    assert any(f.id == "browser:passwords:chrome" for f in report.followups)


def test_the_plaintext_export_is_deleted_once_the_bundle_holds_it(
    monkeypatch, profile: Path, tmp_path: Path
):
    """What the browser wrote is plaintext sitting in Downloads. The copy in the
    bundle is encrypted; the original has served its purpose."""
    chrome_with_local_passwords(profile)
    export = tmp_path / "Chrome Passwords.csv"
    export.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)
    wizard._ingest_password_csv("chrome", export)

    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert not export.exists()
    assert any("deleted" in line for line in wizard.shred_results)


def test_unticking_the_box_leaves_the_users_own_file_alone(
    monkeypatch, profile: Path, tmp_path: Path
):
    """Deleting something a person made is theirs to decide. Unticked means
    untouched, and nothing else about the backup changes."""
    chrome_with_local_passwords(profile)
    export = tmp_path / "Chrome Passwords.csv"
    export.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)
    wizard._ingest_password_csv("chrome", export)
    wizard.shred_after.set(False)

    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert export.is_file()
    assert wizard.shred_results == []


def test_a_machine_with_everything_synced_says_so_rather_than_nothing(
    monkeypatch, profile: Path
):
    """The common case. "Nothing to do" has to be said out loud, with the
    account to sign into, or the page reads as broken."""
    import json

    folder = profile / "AppData" / "Local" / "Google" / "Chrome" / "User Data" / "Default"
    folder.mkdir(parents=True)
    (folder / "Preferences").write_text(
        json.dumps(
            {
                "profile": {"name": "Person 1"},
                "account_info": [{"email": "me@x.test"}],
                "sync": {"requested": True},
            }
        ),
        encoding="utf-8",
    )

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    assert wizard.password_targets == []
    assert "sign in" in wizard.passwords_intro.cget("text").lower()
    assert "me@x.test" in wizard.passwords_cloud.cget("text")


def test_files_only_mode_has_nothing_to_offer_and_says_which(monkeypatch, profile: Path):
    """files-only is a promise that no credential material travels. The page
    must not offer to add some."""
    chrome_with_local_passwords(profile)
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.files_only.set(True)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    assert wizard.password_rows == {}
    assert "files-only" in wizard.passwords_intro.cget("text").lower()


def test_the_passwords_come_back_when_a_backup_is_restored(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The round trip, driven through the window both ways. Restoring listed the
    export, ticked it, reported it restored -- and wrote nothing, because the
    window always names what it is putting back and naming a single file matched
    no member at all. What comes back is the plaintext CSV in a folder of its
    own, plus the instruction to import it and delete it: WinMigrate does not
    put passwords into a browser, which is a thing only the person can do.
    """
    chrome_with_local_passwords(profile)
    export = tmp_path / "Chrome Passwords.csv"
    export.write_text(
        "url,username,password\nhttps://secretsite.test,me,swordfish\n", encoding="utf-8"
    )
    bundle_path = tmp_path / "b.dat"

    backup, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    backup.use_vss.set(False)
    backup._show(Step.SCANNING)
    assert pump(backup) == "scanned"
    backup._show(Step.PASSWORDS)
    backup._ingest_password_csv("chrome", export)
    backup.output_var.set(str(bundle_path))
    backup.passphrase.insert(0, "hunter2")
    backup.passphrase2.insert(0, "hunter2")
    backup._show(Step.WORKING)
    assert pump(backup) == "captured"

    destination = tmp_path / "new"
    destination.mkdir()
    window, _ = open_window(monkeypatch, {})
    window.mode_var.set(Mode.RESTORE.value)
    window.bundle_var.set(str(bundle_path))
    window.bundle_passphrase.insert(0, "hunter2")
    window._show(Step.OPENING)
    assert pump(window) == "opened"

    # Listed, and marked as the secret it is, so it can be left off a shared
    # machine deliberately rather than by accident.
    row = next(
        r for r in window.data.restore_rows if r.item_id == "browser:passwords-csv:chrome"
    )
    assert row.secret is True and row.item_id in window.data.restore_selected

    window.destination_var.set(str(destination))
    window._show(Step.RESTORING)
    assert pump(window) == "restored"

    restored = destination / "WinMigrate-Passwords" / "chrome-passwords.csv"
    assert restored.is_file()
    assert "swordfish" in restored.read_text(encoding="utf-8")
    # And the last page says what is left for the person to do.
    instructions = written(window.followup_box)
    assert "Import your Google Chrome passwords" in instructions
    assert "delete" in instructions.lower()


# --- which machine the window is looking at ---------------------------------
def windows_live(monkeypatch, profile: str):
    """Pretend this is Windows, signed in as ``profile``."""
    from winmigrate.platform_win import Environment

    monkeypatch.delenv("WINMIGRATE_ALLOW_NON_WINDOWS", raising=False)
    live = Environment(
        profile_root=Path(profile),
        environ={"USERPROFILE": profile},
        registry=None,        # live reads
        is_windows=True,
    )
    monkeypatch.setattr(Environment, "live", classmethod(lambda cls: live), raising=True)
    return live


def test_the_window_reads_the_real_machine_rather_than_a_fixture(monkeypatch):
    """It handed back Environment.fixture() whenever the profile field held a
    path -- which it always does, because the window fills it in with the
    signed-in profile. A fixture has an empty registry and says it is not
    Windows, so on a real machine every registry-backed part of a backup
    quietly did nothing: the software inventory, Office, OneDrive's account,
    the display layouts, and finding the browser to open on the passwords page.
    """
    # The window is built first: opening it reads the theme from the registry,
    # and the machine these tests run on has none.
    wizard, _ = open_window(monkeypatch, {"profile_root": r"C:\Users\alessio"})
    live = windows_live(monkeypatch, r"C:\Users\alessio")

    env = wizard._environment(wizard._config())

    assert env is live
    assert env.registry is None and env.is_windows is True


def test_another_profile_on_a_real_machine_still_reads_that_machine(monkeypatch):
    """Backing up a different account moves the profile paths. It does not turn
    the registry off -- what is installed, and where, is machine-wide."""
    wizard, _ = open_window(monkeypatch, {"profile_root": r"D:\OldDisk\Users\maria"})
    windows_live(monkeypatch, r"C:\Users\alessio")

    env = wizard._environment(wizard._config())

    # registry=None means live reads rather than a fixture's empty dict; the
    # platform stays whatever it really is, which is the point of not faking it.
    assert env.registry is None
    assert env.profile_root == Path(r"D:\OldDisk\Users\maria")
    assert env.environ["USERPROFILE"] == r"D:\OldDisk\Users\maria"


def test_a_fake_profile_tree_off_windows_is_still_a_fixture(monkeypatch, profile: Path):
    """Developing this on Linux, and the Windows test runs, depend on it."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})

    env = wizard._environment(wizard._config())

    assert env.registry == {} and env.is_windows is False


def test_the_export_address_goes_on_the_clipboard_either_way(monkeypatch, profile: Path):
    """Launching the browser is not the same as it navigating. An instance that
    is already open can come to the front on whatever page it was showing, and
    being told to type "brave://password-manager/settings" by hand is the
    fiddling this button exists to remove."""
    from winmigrate import passwords as passwords_mod

    chrome_with_local_passwords(profile)
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    monkeypatch.setattr(passwords_mod, "open_export_page", lambda target, env=None: True)
    wizard._open_export_page("chrome")
    assert wizard.root.clipboard == "chrome://password-manager/settings"
    assert "clipboard" in wizard.password_status["chrome"].cget("text")

    # And when it could not be started at all, the address is still there.
    wizard.root.clipboard = ""
    monkeypatch.setattr(passwords_mod, "open_export_page", lambda target, env=None: False)
    wizard._open_export_page("chrome")
    assert wizard.root.clipboard == "chrome://password-manager/settings"
    assert "Open it yourself" in wizard.password_status["chrome"].cget("text")


def test_the_window_does_not_claim_a_page_the_browser_never_opened(
    monkeypatch, profile: Path
):
    """Chromium ignores an internal address handed to it by another program, so
    the browser comes up on its settings and not on the password page. Saying
    "Chrome should now be showing chrome://password-manager/settings" sends the
    user hunting for a tab that was never opened; the window says where it
    really is and what the remaining hop is."""
    from winmigrate import passwords as passwords_mod

    chrome_with_local_passwords(profile)
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    monkeypatch.setattr(passwords_mod, "open_export_page", lambda target, env=None: True)
    wizard._open_export_page("chrome")

    said = wizard.password_status["chrome"].cget("text")
    assert "should now be showing" not in said
    assert "settings" in said and "chrome://password-manager/settings" in said
    assert "Export passwords" in said


def test_the_page_offers_every_profile_that_has_local_passwords(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The window asked once per browser, so a machine with a work profile and a
    personal one could only ever export one of them -- and the row did not say
    which."""
    import json as _json

    user_data = profile / "AppData" / "Local" / "Google" / "Chrome" / "User Data"
    for folder, name in (("Default", "Personal"), ("Profile 2", "Work")):
        (user_data / folder).mkdir(parents=True)
        (user_data / folder / "Preferences").write_text(
            _json.dumps({"profile": {"name": name}}), encoding="utf-8"
        )

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    assert sorted(wizard.password_rows) == ["chrome", "chrome:profile-2"]

    # Both exports land in the same bundle, under their own names.
    for key, name in (("chrome", "personal.csv"), ("chrome:profile-2", "work.csv")):
        export = tmp_path / name
        export.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")
        wizard._ingest_password_csv(key, export)

    assert sorted(wizard.data.passwords_added) == ["chrome", "chrome:profile-2"]
    staged = [
        row.item_id for row in wizard.data.rows if "passwords-csv" in row.item_id
    ]
    assert sorted(staged) == [
        "browser:passwords-csv:chrome",
        "browser:passwords-csv:chrome:profile-2",
    ]
    assert all(item in wizard.data.selected for item in staged)


# --- never delete what is not in the backup ---------------------------------
def staged_export(monkeypatch, profile: Path, tmp_path: Path, name: str = "pw.csv"):
    """A window with a password export staged, one step from the capture."""
    chrome_with_local_passwords(profile)
    export = tmp_path / name
    export.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)
    wizard._ingest_password_csv("chrome", export)
    return wizard, export


def capture_to(wizard, output: Path) -> None:
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"


def test_scanning_again_drops_the_staged_export_rather_than_deleting_it(
    monkeypatch, profile: Path, tmp_path: Path
):
    """A second scan builds a new plan that the export was never added to. The
    window kept the record anyway: the last page claimed the passwords had
    travelled, and then deleted the only plaintext copy of passwords that were
    not in the bundle at all."""
    wizard, export = staged_export(monkeypatch, profile, tmp_path)

    wizard._show(Step.SCANNING)  # back to the options, and scan again
    assert pump(wizard) == "scanned"
    capture_to(wizard, tmp_path / "out.dat")

    assert export.is_file()
    assert "Exported passwords" not in wizard.done_text.cget("text")
    assert wizard.data.passwords_added == {}


def test_unticking_the_export_leaves_the_file_where_it_is(
    monkeypatch, profile: Path, tmp_path: Path
):
    """Still in the plan, but marked skipped, so it is not in the bundle. The
    box asks about deleting a file that has been backed up; this one has not."""
    wizard, export = staged_export(monkeypatch, profile, tmp_path)

    wizard.data.selected.discard("browser:passwords-csv:chrome")
    capture_to(wizard, tmp_path / "out.dat")

    assert export.is_file()
    assert any("not in the backup" in line for line in wizard.shred_results)


def test_choosing_a_second_file_forgets_the_first_completely(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The usual reason to choose again is having picked the wrong file. The
    wrong one is not in the bundle, so it is not this tool's to delete."""
    wizard, first = staged_export(monkeypatch, profile, tmp_path, "wrong.csv")
    second = tmp_path / "right.csv"
    second.write_text("url,username,password\nhttps://x,me,pw\n", encoding="utf-8")
    wizard._ingest_password_csv("chrome", second)

    capture_to(wizard, tmp_path / "out.dat")

    assert first.is_file()
    assert not second.exists()


# --- saying what it changed -------------------------------------------------
def test_a_restore_says_which_settings_it_put_back(monkeypatch, bundle: Path, tmp_path: Path):
    """A restore re-applies Wi-Fi profiles, printers, mapped drives and
    environment variables, and the window said nothing about any of it -- not
    on the page that asks to start, and not on the one that reports what
    happened."""
    from winmigrate.apply import Outcome, Result

    destination = tmp_path / "new"
    destination.mkdir()
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORE_CONFIRM)

    assert "Wi-Fi networks, printers" in wizard.restore_summary.cget("text")

    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"
    wizard.restore_report.applied = [
        Result(kind="wifi", name="Office", outcome=Outcome.APPLIED),
        Result(kind="printer", name="HP-4th-floor", outcome=Outcome.FAILED, detail="no driver"),
    ]
    wizard._render_restore_done()

    text = wizard.restore_done_text.cget("text")
    assert "wifi Office" in text
    assert "printer HP-4th-floor: no driver" in text


def test_the_window_names_the_software_it_prepared_and_did_not_install(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """The restore writes a winget import, an Office configuration and a
    by-hand list into a folder, and installs none of it -- deliberately. The
    command line says so at the end of every restore. The window said nothing,
    so the only software the user ever saw named was the part it could *not*
    install, and "no apps were installed" was the only reading available."""
    from winmigrate.reinstall import Artifacts

    destination = tmp_path / "new"
    destination.mkdir()
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"

    folder = destination / "WinMigrate-Reinstall"
    wizard.restore_report.artifacts = Artifacts(
        directory=folder,
        winget_import=folder / "winget-import.json",
        manual_list=folder / "reinstall-by-hand.md",
        reinstallable_count=97,
        manual_count=167,
        component_count=8,
    )
    wizard._render_restore_done()

    text = wizard.restore_done_text.cget("text")
    assert "nothing has been installed yet" in text
    assert "97 can be reinstalled for you" in text
    assert "167 need installing by hand" in text
    assert str(folder) in text
    # Both commands: the bare one prints the plan and installs nothing, which
    # is deliberate, so offering only it would repeat the same silence.
    assert f'winmigrate reinstall "{folder}"' in text
    assert f'winmigrate reinstall "{folder}" --apps' in text
    # And a way there that does not involve typing the path out.
    assert wizard.reinstall_button._packed


def test_nothing_about_software_is_shown_when_there_was_none(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """A bundle captured with the software list off has no reinstall folder, and
    a heading about software that says nothing under it is noise."""
    destination = tmp_path / "new"
    destination.mkdir()
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"

    wizard.restore_report.artifacts = None
    wizard._render_restore_done()

    assert "Software" not in wizard.restore_done_text.cget("text")
    assert not wizard.reinstall_button._packed


def test_the_practice_run_says_it_will_not_touch_settings_either(
    monkeypatch, bundle: Path, tmp_path: Path
):
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(tmp_path / "new"))
    wizard.dry_run_var.set(True)
    wizard._show(Step.RESTORE_CONFIRM)

    summary = wizard.restore_summary.cget("text")
    assert "Wi-Fi networks, printers" not in summary
    assert "Practice run" in summary


def test_unticking_it_means_the_restore_is_told_not_to(monkeypatch, bundle: Path, tmp_path: Path):
    from winmigrate import restore as restore_mod

    seen: list = []
    monkeypatch.setattr(
        restore_mod, "restore",
        lambda options, progress=None: seen.append(options) or _stub_restore_report(options),
    )
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard.destination_var.set(str(tmp_path / "new"))
    wizard.apply_settings_var.set(False)
    wizard.data.restore_selected = {"files:documents"}
    wizard._start_restore()
    assert pump(wizard) == "restored"

    assert seen and seen[0].apply_settings is False


def _stub_restore_report(options):
    from winmigrate.restore import RestoreReport

    return RestoreReport(bundle=options.bundle, destination=options.destination)


def test_a_capture_warning_reaches_the_last_page(monkeypatch, profile: Path, tmp_path: Path):
    """"The shadow copy could not be read, so files were read directly" only
    ever appeared in the log and on the console. It means files a program was
    holding open were copied live, which is worth knowing before the old
    machine is wiped."""
    from winmigrate.models import Note, Severity

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    wizard.capture_report.notes.append(
        Note(Severity.WARNING, "the shadow copy could not be read, so files were read directly")
    )
    wizard.done_text.configure(text=wizard._done_summary())

    assert "shadow copy could not be read" in wizard.done_text.cget("text")


# --- not writing over a backup ----------------------------------------------
def test_a_name_that_is_already_a_backup_blocks_the_button(
    monkeypatch, profile: Path, tmp_path: Path
):
    """A bundle written to a name that is taken destroys the backup that was
    there, and nothing afterwards says so: the new file has the same name, the
    same shape, and none of the old data."""
    taken = tmp_path / "backup.dat"
    taken.write_bytes(b"LAST WEEK'S BUNDLE")

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(taken))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.DESTINATION)

    assert wizard.next_button.state == "disabled"
    assert "already there" in wizard.hint.cget("text")
    assert "already a backup" in wizard.output_hint.cget("text")

    # Ticking the box is the decision, and the confirmation page repeats it.
    wizard.replace_var.set(True)
    wizard._refresh_buttons()
    assert wizard.next_button.state == "normal"
    wizard._show(Step.CONFIRM)
    assert "Replaces:" in wizard.confirm_text.cget("text")


def test_the_sidecar_counts_as_a_backup_being_there(monkeypatch, profile: Path, tmp_path: Path):
    """Half a pair left behind is still someone's backup, and replacing one of
    the two leaves a bundle and a manifest describing different things."""
    (tmp_path / "backup.manifest.json").write_text("{}", encoding="utf-8")

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(tmp_path / "backup.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.DESTINATION)

    assert wizard.next_button.state == "disabled"


def test_the_restore_page_says_when_there_is_no_room(monkeypatch, bundle: Path, tmp_path: Path):
    """Being told before pressing Start is the difference between unticking a
    few items and finding out at the end of a page you have already left."""
    import shutil
    from collections import namedtuple

    Usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(shutil, "disk_usage", lambda path: Usage(1_000, 999, 1))

    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(tmp_path))
    wizard._show(Step.RESTORE_CONFIRM)

    assert "Not enough room" in wizard.restore_summary.cget("text")

    # A practice run writes nothing, so a full disk is no reason to warn.
    wizard.dry_run_var.set(True)
    wizard._refresh_restore_summary()
    assert "Not enough room" not in wizard.restore_summary.cget("text")


def test_the_summary_keeps_its_order_when_both_extra_lines_appear(
    monkeypatch, profile: Path, tmp_path: Path
):
    """Two optional lines addressed by counted index is a summary that reorders
    itself the first time both of them show up."""
    taken = tmp_path / "backup.dat"
    taken.write_bytes(b"LAST WEEK")
    wizard, export = staged_export(monkeypatch, profile, tmp_path)
    wizard.output_var.set(str(taken))
    wizard.replace_var.set(True)

    lines = [line for line in wizard._confirm_summary().splitlines() if line]

    assert lines[1].startswith("To:")
    assert lines[2].startswith("Replaces:")
    assert lines[3].startswith("Items:")
    assert [line.split(":")[0] for line in lines[3:8]] == [
        "Items", "Size", "Encrypted-only items", "Passwords", "Shadow copy"
    ]


def test_scanning_a_different_profile_looks_at_that_profile_s_browsers(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The browsers found belong to the profile that was scanned. Held from the
    first scan, the page went on describing the profile the user had just
    changed away from."""
    chrome_with_local_passwords(profile)
    other = tmp_path / "Users" / "bob"
    (other / "Documents").mkdir(parents=True)
    (other / "Documents" / "f.bin").write_bytes(b"x" * 100)

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)
    assert list(wizard.password_rows) == ["chrome"]

    wizard.profile_var.set(str(other))
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard._show(Step.PASSWORDS)

    assert wizard.password_rows == {}
    assert "No browser" in wizard.passwords_intro.cget("text")


def test_files_caught_mid_write_are_mentioned_on_the_last_page(
    monkeypatch, profile: Path, tmp_path: Path
):
    """They are in the bundle and their digest matches what was written, so
    nothing else about the run looks wrong. The console version has always said
    so; the window did not."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    wizard.capture_report.changed_while_reading = [r"C:\Users\a\AppData\app.log"]
    wizard.done_text.configure(text=wizard._done_summary())

    assert "being written while they were copied" in wizard.done_text.cget("text")


# --- checking the backup ----------------------------------------------------
def test_the_window_can_read_the_backup_back_and_say_it_matched(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The command line has --verify; the window had nothing, so after an
    hour-long backup there was no way to find out whether it opens without
    going to the console tool. It is the question asked just before a machine
    is wiped."""
    output = tmp_path / "out.dat"
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    assert wizard.verify_after.get() is True  # on by default

    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert wizard.verify_report is not None and wizard.verify_report.ok
    assert wizard.verify_report.files_checked > 0
    assert "every file read back and matched" in wizard.done_text.cget("text")


def test_a_backup_that_does_not_check_out_says_so_in_as_many_words(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The one result that must not be quiet: the file exists, it is the right
    size, and it cannot be trusted."""
    from winmigrate.verify import VerifyReport

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard.verify_after.set(False)
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    wizard.verify_report = VerifyReport(
        bundle=tmp_path / "out.dat", mismatches=["files:documents"], missing=["files:desktop"]
    )
    wizard.done_text.configure(text=wizard._done_summary())

    text = wizard.done_text.cget("text")
    assert "Do not rely on this backup" in text


def test_unticking_the_check_skips_it(monkeypatch, profile: Path, tmp_path: Path):
    """It takes about as long again as writing the backup."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(tmp_path / "out.dat"))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard.verify_after.set(False)

    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert wizard.verify_report is None
    assert "read back and matched" not in wizard.done_text.cget("text")


def test_a_check_that_cannot_run_does_not_hide_the_backup_that_was_written(
    monkeypatch, profile: Path, tmp_path: Path
):
    """The bundle is on disk by then. Sharing one try block with the capture
    meant a failed check threw the window back to the choosing page with an
    integrity error, as though nothing had been written -- and the file was
    sitting there all along."""
    from winmigrate import verify as verify_mod
    from winmigrate.errors import IntegrityError

    output = tmp_path / "out.dat"
    monkeypatch.setattr(
        verify_mod,
        "verify",
        lambda *a, **k: (_ for _ in ()).throw(IntegrityError("the drive went away")),
    )

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.use_vss.set(False)
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.output_var.set(str(output))
    wizard.passphrase.insert(0, "hunter2")
    wizard.passphrase2.insert(0, "hunter2")
    wizard._show(Step.WORKING)
    assert pump(wizard) == "captured"

    assert wizard.step is Step.DONE
    assert output.is_file()
    text = wizard.done_text.cget("text")
    assert "the check could not be completed" in text
    assert "the drive went away" in text


def test_the_window_names_the_records_so_they_are_not_left_out(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """The records are not tick boxes -- the printers, the software list and
    the follow-ups are how a restore explains itself -- but a restore that
    names items leaves out everything it did not name, and the window always
    names items."""
    from winmigrate import restore as restore_mod

    seen: list = []
    monkeypatch.setattr(
        restore_mod, "restore",
        lambda options, progress=None: seen.append(options) or _stub_restore_report(options),
    )
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(tmp_path / "new"))
    # Only one file item ticked, as if the user unticked the rest.
    wizard.data.restore_selected = {"files:documents"}
    wizard._start_restore()
    assert pump(wizard) == "restored"

    named = set(seen[0].items)
    records = {
        item["id"]
        for item in wizard.manifest["items"]
        if item.get("kind") not in ("tree", "file")
    }
    assert "files:documents" in named
    assert records <= named


def test_the_restore_page_says_which_programs_to_close(monkeypatch, profile: Path, tmp_path: Path):
    """The capture side has always said to close things for a cleaner copy.
    The restore side said nothing, and it is the side where a program writing
    to those files at the same time damages its own data."""
    chrome_with_local_passwords(profile)
    bundle_path = tmp_path / "b.dat"

    backup, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    backup.use_vss.set(False)
    backup.verify_after.set(False)
    backup._show(Step.SCANNING)
    assert pump(backup) == "scanned"
    backup.output_var.set(str(bundle_path))
    backup.passphrase.insert(0, "hunter2")
    backup.passphrase2.insert(0, "hunter2")
    backup._show(Step.WORKING)
    assert pump(backup) == "captured"

    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle_path))
    wizard.bundle_passphrase.insert(0, "hunter2")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(tmp_path / "new"))
    wizard._show(Step.RESTORE_CONFIRM)

    assert "Close Google Chrome" in wizard.restore_summary.cget("text")

    # A practice run writes nothing, so nobody has to close anything.
    wizard.dry_run_var.set(True)
    wizard._refresh_restore_summary()
    assert "Close Google Chrome" not in wizard.restore_summary.cget("text")


# --- installing the software ------------------------------------------------
def _with_software(wizard, tmp_path: Path, packages: list[str]):
    """Give the finished restore a winget import, as a real one would have."""
    from winmigrate.reinstall import Artifacts

    folder = tmp_path / "WinMigrate-Reinstall"
    folder.mkdir(exist_ok=True)
    import_file = folder / "winget-import.json"
    import_file.write_text(
        json.dumps(
            {"Sources": [{"Packages": [{"PackageIdentifier": name} for name in packages]}]}
        ),
        encoding="utf-8",
    )
    wizard.restore_report.artifacts = Artifacts(
        directory=folder,
        winget_import=import_file,
        reinstallable_count=len(packages),
        manual_count=3,
    )
    return import_file


def _finished_restore(monkeypatch, bundle: Path, tmp_path: Path):
    destination = tmp_path / "new"
    destination.mkdir(exist_ok=True)
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.bundle_var.set(str(bundle))
    wizard.bundle_passphrase.insert(0, "pw")
    wizard._show(Step.OPENING)
    assert pump(wizard) == "opened"
    wizard.destination_var.set(str(destination))
    wizard._show(Step.RESTORING)
    assert pump(wizard) == "restored"
    return wizard


def test_a_restore_with_software_stops_to_offer_it(monkeypatch, bundle: Path, tmp_path: Path):
    """Backing up a profile and getting it back should not mean going to a
    command line afterwards. The restore ends on the software page, which lists
    what it would fetch and installs nothing until asked."""
    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox", "7zip.7zip"])

    wizard._show(Step.SOFTWARE)

    assert "2 application(s) can be installed for you" in wizard.software_text.cget("text")
    listed = written(wizard.software_box)
    assert "Mozilla.Firefox" in listed and "7zip.7zip" in listed
    assert "3 more have no package" in wizard.software_text.cget("text")


def test_a_restore_with_no_software_does_not_show_an_empty_page(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """A page between the work and the result, with nothing on it, is a step
    backwards."""
    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    wizard.restore_report.artifacts = None

    wizard.events.put(("restored", wizard.restore_report))
    wizard._drain_events()

    assert wizard.step is Step.RESTORE_DONE


def test_the_install_streams_what_winget_says_and_reports_how_it_went(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """A bar that moves once at the end, after ninety-seven downloads, is a bar
    with nothing behind it."""
    from winmigrate.util import process

    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])

    def fake_stream(command, on_line, timeout=0, cancelled=None):
        assert command[:2] == ["winget", "import"]
        on_line("Found Mozilla Firefox [Mozilla.Firefox]")
        on_line("Successfully installed")
        return process.CommandResult(command, 0, "ok")

    monkeypatch.setattr(process, "stream", fake_stream)

    wizard._show(Step.INSTALLING)
    assert pump(wizard, until=("installed", "install-failed")) == "installed"

    assert wizard.step is Step.RESTORE_DONE
    assert "Found Mozilla Firefox" in written(wizard.install_output)
    assert "winget installed everything" in wizard.restore_done_text.cget("text")


def test_stopping_an_install_says_what_is_already_installed_stays(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """Stop cannot uninstall what has already been put on, and saying nothing
    leaves the user wondering which half they have."""
    from winmigrate.util import process

    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])

    def fake_stream(command, on_line, timeout=0, cancelled=None):
        return process.CommandResult(command, None, "part way", error="stopped")

    monkeypatch.setattr(process, "stream", fake_stream)
    wizard._show(Step.INSTALLING)
    assert pump(wizard, until=("installed", "install-failed")) == "installed"

    assert "you stopped the install" in wizard.restore_done_text.cget("text")
    assert "what had finished is installed" in wizard.restore_done_text.cget("text").lower()


def test_a_machine_without_winget_is_told_so_rather_than_left_guessing(
    monkeypatch, bundle: Path, tmp_path: Path
):
    from winmigrate.util import process

    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])

    def fake_stream(command, on_line, timeout=0, cancelled=None):
        return process.CommandResult(command, None, error="winget not found on this machine")

    monkeypatch.setattr(process, "stream", fake_stream)
    wizard._show(Step.INSTALLING)
    assert pump(wizard, until=("installed", "install-failed")) == "installed"

    text = wizard.restore_done_text.cget("text")
    assert "winget is not on this machine" in text
    assert "App Installer" in text


def test_a_restore_without_the_rights_says_so_rather_than_offering_a_dead_button(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """winget installs programs for the whole machine, which needs permission.
    Saying "Windows will ask" invites somebody to press a button that cannot
    work; saying what to do instead costs the same line."""
    from winmigrate.gui import elevate

    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])

    wizard._show(Step.SOFTWARE)

    note = wizard.software_note.cget("text")
    assert "cannot" in note and "administrator" in note
    # And it says the part that stops this reading as a failed migration.
    assert "already back" in note


# --- saved Windows sign-ins -------------------------------------------------
def test_the_window_offers_the_credential_handoff_the_command_line_had(
    monkeypatch, profile: Path
):
    """A command-line flag is not a feature for the person this tool is for.
    The handoff needs Windows' own wizard either way, but finding it should not
    require knowing that --credentials exists."""
    from winmigrate import credentials as credentials_mod

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    monkeypatch.setattr(
        credentials_mod, "list_credentials",
        lambda: ([{"target": "Domain:target=fileserver", "type": "Domain Password",
                   "user": "maria"}], None),
    )
    wizard._environment(wizard._config()).is_windows = True

    wizard.credential_entries = [
        {"target": "Domain:target=fileserver", "type": "Domain Password", "user": "maria"}
    ]
    wizard._show(Step.CREDENTIALS)

    intro = wizard.credentials_intro.cget("text")
    assert "1 saved sign-in" in intro
    assert "Ctrl+Alt+Del" in intro
    # Named the way somebody recognises them, not as Windows spells them.
    assert "fileserver" in wizard.credentials_list.cget("text")


def test_a_machine_with_no_saved_sign_ins_says_so_rather_than_nothing(
    monkeypatch, profile: Path
):
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []

    wizard._show(Step.CREDENTIALS)

    assert "no saved sign-ins" in wizard.credentials_intro.cget("text")


def test_the_backup_file_is_staged_and_its_path_never_logged(
    monkeypatch, profile: Path, tmp_path: Path, caplog
):
    """It names a file holding every saved sign-in they have."""
    import logging

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []
    wizard._show(Step.CREDENTIALS)

    backup = tmp_path / "my-sign-ins.crd"
    backup.write_bytes(b"\x01\x02encrypted")
    with caplog.at_level(logging.INFO, logger="winmigrate.gui.app"):
        wizard._ingest_credential_backup(backup)

    staged = [item for item in wizard.scan_result.items if item.id == "credentials:backup"]
    assert len(staged) == 1
    # And it is selected, or the one thing they went out of their way to include
    # would be counted as deselected at capture time.
    assert "credentials:backup" in wizard.data.selected
    assert str(backup) not in "\n".join(caplog.messages)


def test_a_file_that_is_not_a_credential_backup_is_refused_on_the_page(
    monkeypatch, profile: Path, tmp_path: Path
):
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []
    wizard._show(Step.CREDENTIALS)

    wrong = tmp_path / "holiday.jpg"
    wrong.write_bytes(b"\xff\xd8\xff")
    wizard._ingest_credential_backup(wrong)

    assert "cannot be used" in wizard.credentials_status.cget("text")
    assert not [i for i in wizard.scan_result.items if i.id == "credentials:backup"]


def test_the_taskbar_is_refreshed_once_its_programs_exist(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """The restore puts the taskbar back before installing anything, because
    that is the order a restore runs in. A pin is a shortcut, and a shortcut to
    a program that is not there yet resolves to a blank icon Explorer then
    remembers."""
    from winmigrate.apply import Outcome, Result
    from winmigrate import apply as apply_mod
    from winmigrate.util import process

    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])
    wizard.restore_report.applied = [
        Result("layout", "your taskbar", Outcome.APPLIED, "4 value(s)")
    ]

    refreshed: list[str] = []
    monkeypatch.setattr(
        apply_mod, "refresh_shell",
        lambda: refreshed.append("yes") or Result("layout", "Explorer", Outcome.APPLIED),
    )
    monkeypatch.setattr(
        process, "stream",
        lambda command, on_line, timeout=0, cancelled=None: process.CommandResult(
            command, 0, "ok"
        ),
    )

    wizard._show(Step.INSTALLING)
    assert pump(wizard, until=("installed", "install-failed")) == "installed"

    assert refreshed == ["yes"]


def test_nothing_is_refreshed_when_there_was_no_taskbar_to_put_back(
    monkeypatch, bundle: Path, tmp_path: Path
):
    """A flicker for nothing is a flicker for nothing."""
    from winmigrate import apply as apply_mod
    from winmigrate.util import process

    wizard = _finished_restore(monkeypatch, bundle, tmp_path)
    _with_software(wizard, tmp_path, ["Mozilla.Firefox"])
    wizard.restore_report.applied = []

    refreshed: list[str] = []
    monkeypatch.setattr(apply_mod, "refresh_shell", lambda: refreshed.append("yes"))
    monkeypatch.setattr(
        process, "stream",
        lambda command, on_line, timeout=0, cancelled=None: process.CommandResult(
            command, 0, "ok"
        ),
    )

    wizard._show(Step.INSTALLING)
    assert pump(wizard, until=("installed", "install-failed")) == "installed"

    assert refreshed == []


# --- the keyboard -----------------------------------------------------------
def test_enter_moves_on_and_escape_backs_out(monkeypatch, profile: Path):
    """Somebody who fills a field and presses Enter expects to move on. A window
    where that does nothing feels broken before anything has gone wrong."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.mode_var.set(Mode.BACKUP.value)
    wizard._show(Step.CHOOSE)

    wizard._on_return()

    assert wizard.step is Step.WELCOME


def test_enter_cannot_skip_a_page_whose_question_is_unanswered(monkeypatch):
    """Inert when the button is disabled, or Enter becomes a way past the one
    check that was in the way."""
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._show(Step.SOURCE)
    wizard.bundle_var.set("")
    wizard._refresh_buttons()
    assert wizard.next_button.cget("state") == "disabled"

    wizard._on_return()

    assert wizard.step is Step.SOURCE


def test_enter_inside_a_text_box_stays_in_the_text_box(monkeypatch, profile: Path):
    """A Text widget wants a newline of its own; stealing it would make the
    box unusable."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard.mode_var.set(Mode.BACKUP.value)
    wizard._show(Step.CHOOSE)

    class InAText:
        widget = type("W", (), {"winfo_class": staticmethod(lambda: "Text")})()

    wizard._on_return(InAText())

    assert wizard.step is Step.CHOOSE


# --- pages with nothing on them ---------------------------------------------
def test_a_page_with_nothing_on_it_is_not_shown(monkeypatch, profile: Path):
    """Somebody who has never had a password saved in a browser should not have
    to read a screen about browser passwords, and then another about Windows
    sign-ins, to reach the one asking where the backup goes."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []

    wizard._show(Step.SELECT)
    wizard._go_next()

    assert wizard.step is Step.DESTINATION


def test_back_does_not_walk_into_the_page_next_stepped_over(
    monkeypatch, profile: Path
):
    """Skipped in both directions, or Back becomes a way into the empty page."""
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []

    wizard._show(Step.DESTINATION)
    wizard._go_back()

    assert wizard.step is Step.SELECT


def test_a_page_with_something_on_it_is_still_shown(monkeypatch, profile: Path):
    """The skip is for empty pages, not for pages somebody would rather not
    read: a browser with local passwords is the whole reason that page exists."""
    chrome_with_local_passwords(profile)
    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"
    wizard.credential_entries = []

    wizard._show(Step.SELECT)
    wizard._go_next()

    assert wizard.step is Step.PASSWORDS


def test_a_page_is_never_skipped_because_its_check_had_not_finished(
    monkeypatch, profile: Path
):
    """"Not detected yet" is not "nothing", and a page skipped because a check
    had not run is a feature the user never learns exists."""
    from winmigrate import credentials as credentials_mod

    wizard, _ = open_window(monkeypatch, {"profile_root": str(profile)})
    wizard._show(Step.SCANNING)
    assert pump(wizard) == "scanned"

    # Nothing cached: the page has to ask before it may be skipped.
    assert wizard.credential_entries is None
    asked: list[str] = []
    monkeypatch.setattr(
        credentials_mod, "list_credentials",
        lambda: (asked.append("yes") or [{"target": "Domain:target=nas"}], None),
    )
    windows = Environment.fixture(profile, {})
    windows.is_windows = True
    monkeypatch.setattr(wizard, "_environment", lambda _config: windows)

    assert wizard._page_is_empty(Step.CREDENTIALS) is False
    assert asked == ["yes"]


# --- opting in to administrator rights, for the restore ---------------------
def _relaunches(monkeypatch) -> list:
    """Record what the window asks Windows to relaunch, instead of relaunching."""
    from winmigrate.gui import elevate

    asked: list = []
    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(
        elevate, "relaunch_as_admin", lambda args: asked.append(args) or True
    )
    return asked


def test_the_restore_asks_for_rights_on_the_first_page_or_not_at_all(monkeypatch):
    """Agreeing restarts the program. Asked on the page that collects the
    backup's password, that would throw the password away along with
    everything else typed there -- so the question comes first, before anything
    has been entered."""
    asked = _relaunches(monkeypatch)
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.restore_as_admin.set(True)

    wizard._go_next()

    assert len(asked) == 1
    # The relaunched copy must come back on the restore branch, still ticked.
    assert "--mode" in asked[0] and "restore" in asked[0]
    assert "--restore-as-admin" in asked[0]
    assert elevate_flag_present(asked[0])


def elevate_flag_present(arguments: list) -> bool:
    from winmigrate.gui.elevate import ALREADY_TRIED_FLAG

    return ALREADY_TRIED_FLAG in arguments


def test_leaving_it_unticked_asks_for_nothing(monkeypatch):
    """It is an opt-in. Somebody who only wants their files back should never
    see a permission prompt."""
    asked = _relaunches(monkeypatch)
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.restore_as_admin.set(False)

    wizard._go_next()

    assert asked == []
    assert wizard.step is Step.SOURCE


def test_declining_the_prompt_carries_on_rather_than_stopping(monkeypatch):
    """The files, the settings and the taskbar all come back without the
    rights. Only the installing needs them."""
    from winmigrate.gui import elevate

    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)
    monkeypatch.setattr(elevate, "relaunch_as_admin", lambda args: False)
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard.restore_as_admin.set(True)

    wizard._go_next()

    assert wizard.step is Step.SOURCE
    assert wizard.elevation_attempted is True


def test_the_relaunched_copy_comes_back_on_the_branch_it_left(monkeypatch):
    """Reopening on the other one, to somebody halfway through a restore, reads
    as the program having forgotten -- and the only thing worse than that is
    their agreeing to it."""
    wizard, _ = open_window(
        monkeypatch, {"mode": "restore", "restore_as_admin": True,
                      "elevation_attempted": True}
    )

    assert wizard.mode_var.get() == Mode.RESTORE.value
    assert wizard.restore_as_admin.get() is True
    # And it does not ask a second time.
    asked = _relaunches(monkeypatch)
    wizard._go_next()
    assert asked == []


def test_a_backup_is_not_asked_the_restore_question(monkeypatch):
    asked = _relaunches(monkeypatch)
    wizard, _ = open_window(monkeypatch, {})
    wizard.mode_var.set(Mode.BACKUP.value)
    wizard.restore_as_admin.set(True)

    wizard._go_next()

    assert asked == []
    assert wizard.step is Step.WELCOME
