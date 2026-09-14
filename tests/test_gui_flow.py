"""Driving the real window, page by page, on stub widgets.

Everything else about the GUI is checked by reading it. This runs it. The two
bugs it found on the first attempt had both survived a careful read: a rail that
described the previous mode rather than the current one, and a passphrase field
emptied while the button that needed it stayed live.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from guistub import open_window, pump, rail
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
    assert rail(wizard) == ["●  Scan", "○  Choose", "○  Destination", "○  Confirm", "○  Finish"]

    # Choosing on the first page repaints immediately, without waiting for the
    # page to change.
    wizard.mode_var.set(Mode.RESTORE.value)
    wizard._refresh_buttons()
    assert rail(wizard) == ["●  Backup", "○  Choose", "○  Confirm", "○  Finish"]

    wizard.mode_var.set(Mode.BACKUP.value)
    wizard._show(Step.CHOOSE)
    assert rail(wizard) == ["●  Scan", "○  Choose", "○  Destination", "○  Confirm", "○  Finish"]


def test_the_rail_marks_progress_as_the_pages_advance(monkeypatch):
    wizard, _ = open_window(monkeypatch)
    wizard._show(Step.DESTINATION)
    assert rail(wizard)[:3] == ["✓  Scan", "✓  Choose", "●  Destination"]


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
