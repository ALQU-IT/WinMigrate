"""What the two shipped executables do with the arguments they are given.

The package ships a window and a console build. They are different binaries
with different subsystems, and only one of them can run a command -- which is
easy to forget when both say "WinMigrate" on the front.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

import gui_entry
from winmigrate import reinstall
from winmigrate.gui import elevate


def test_the_window_opens_with_no_arguments_at_all():
    options, complaint = gui_entry._window_arguments([])
    assert options == {} and complaint == ""


def test_the_elevated_relaunch_comes_back_on_the_branch_it_left():
    """Elevation restarts this executable with the first page's choices on the
    command line so the elevated window opens looking like the one that
    disappeared. The entry point used to ignore them, so it came back on page
    one offering to back the machine up -- to somebody halfway through a
    restore. The one thing worse than that is their agreeing to it.

    The two halves are written in different files; this is the test that says
    they still meet.
    """
    argv = elevate.forward_arguments(
        mode="restore", restore_as_admin=True, profile_root=r"C:\Users\Bob Smith",
        include_wifi=True, compression="none",
    )
    options, complaint = gui_entry._window_arguments(argv)

    assert complaint == ""
    assert options["mode"] == "restore"
    assert options["restore_as_admin"] is True
    assert options["profile_root"] == r"C:\Users\Bob Smith"
    assert options["include_wifi"] is True
    assert options["compression"] == "none"
    assert options["elevation_attempted"] is True


def test_a_command_the_window_cannot_run_says_so_instead_of_opening_a_wizard():
    """"WinMigrate.exe reinstall <folder> --apps" -- the command the restore
    report printed -- opened the backup wizard. Somebody who came to install
    their programs was answered with the wrong question, and it looked like a
    working program the whole time."""
    options, complaint = gui_entry._window_arguments(
        ["reinstall", r"C:\x\WinMigrate-Reinstall", "--apps"]
    )

    assert options is None, "the window must not open for a command it cannot run"
    assert reinstall.CONSOLE_BUILD in complaint
    # And it hands back the command that will work, rather than a lecture.
    assert "reinstall" in complaint and "--apps" in complaint


@pytest.mark.parametrize(
    "argv", [["capture"], ["restore", "b.dat"], ["scan"], ["verify", "b.dat"]]
)
def test_every_console_command_is_turned_away_the_same_way(argv):
    options, complaint = gui_entry._window_arguments(argv)
    assert options is None
    assert reinstall.CONSOLE_BUILD in complaint


def test_options_the_window_does_not_understand_do_not_print_to_a_missing_console():
    """argparse answers a bad option by printing usage and exiting. A windowed
    build has no console, so that is a program that vanishes without a word."""
    options, complaint = gui_entry._window_arguments(["gui", "--nonsense"])

    assert options is None
    assert "--help" in complaint


# --- what to tell somebody to type -----------------------------------------
def test_from_a_checkout_the_command_is_the_installed_script(monkeypatch):
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    assert reinstall.console_command() == "winmigrate"


def test_frozen_it_names_the_console_build_and_not_the_window(monkeypatch, tmp_path):
    """The window cannot run commands. Naming it is how the restore report came
    to print an instruction that opened a backup wizard."""
    window = tmp_path / "WinMigrate.exe"
    window.write_bytes(b"MZ")
    (tmp_path / reinstall.CONSOLE_BUILD).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(window))

    command = reinstall.console_command()

    assert reinstall.CONSOLE_BUILD in command
    assert "WinMigrate.exe" not in command
    # A full path, because whoever reads this is not necessarily standing in
    # that folder.
    assert str(tmp_path) in command


def test_a_path_with_a_space_in_it_is_quoted(monkeypatch, tmp_path):
    """Program Files, Bob Smith, OneDrive - Company. Unquoted, the shell reads
    the first word as the program and the rest as arguments."""
    folder = tmp_path / "My Tools"
    folder.mkdir()
    window = folder / "WinMigrate.exe"
    window.write_bytes(b"MZ")
    (folder / reinstall.CONSOLE_BUILD).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(window))

    assert reinstall.console_command().startswith('"')
    assert reinstall.console_command().endswith('"')


def test_without_a_console_build_beside_it_the_window_is_named_after_all(
    monkeypatch, tmp_path
):
    """Naming a file that is not there is worse than naming the window, which
    now explains itself when asked to run a command it cannot."""
    window = tmp_path / "WinMigrate.exe"
    window.write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(window))

    assert reinstall.console_command() == "WinMigrate.exe"


def test_the_name_the_entry_point_hands_out_is_the_one_that_is_shipped():
    """Two files agree on it by writing it once. A rename in the build workflow
    that missed one of them would print a command naming a file nobody has."""
    workflow = Path("/home/user/WinMigrate/.github/workflows/build-exe.yml").read_text(
        encoding="utf-8"
    )
    assert f"--name {reinstall.CONSOLE_BUILD.removesuffix('.exe')}" in workflow
    assert gui_entry.CONSOLE_BUILD == reinstall.CONSOLE_BUILD


def test_the_window_is_actually_opened_with_what_was_parsed(monkeypatch):
    """The parsing is only half of it. What this file was wired to was run()
    with no arguments at all, so every option it read went nowhere -- which is
    the whole bug, and was invisible to a test that only checked the parser."""
    opened: list[dict] = []
    argv = elevate.forward_arguments(mode="restore", profile_root=r"C:\Users\Bob")

    code = gui_entry.main(argv, opener=lambda options: opened.append(options) or 0)

    assert code == 0
    assert opened and opened[0]["mode"] == "restore"
    assert opened[0]["profile_root"] == r"C:\Users\Bob"


def test_a_console_command_never_opens_the_window(monkeypatch):
    """This is the failure the user hit: a window opened for "reinstall", on
    its first page, offering to back the machine up."""
    opened: list[dict] = []
    said: list[str] = []
    monkeypatch.setattr(gui_entry, "_report", lambda message: said.append(message))

    code = gui_entry.main(
        ["reinstall", "C:\\x", "--apps"], opener=lambda options: opened.append(options) or 0
    )

    assert opened == [], "the window opened for a command it cannot run"
    assert code == 2
    assert said and reinstall.CONSOLE_BUILD in said[0]
