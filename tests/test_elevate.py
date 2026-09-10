"""Asking Windows for administrator rights -- once, and at the right moment.

A shadow copy needs elevation, elevation restarts the process, and a restart
throws away whatever the process had gathered. So the prompt has to come before
the scan, and the first page's choices have to survive the trip.
"""

from __future__ import annotations

import sys

import pytest

from winmigrate.gui import elevate


def test_no_prompt_when_there_is_nothing_to_gain(monkeypatch):
    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: False)

    # Not wanted.
    assert elevate.should_offer(False, already_tried=False) is False
    # Already asked once. A refused prompt must not become a loop of prompts.
    assert elevate.should_offer(True, already_tried=True) is False
    # Wanted, not yet asked, not yet elevated: the one case that prompts.
    assert elevate.should_offer(True, already_tried=False) is True


def test_no_prompt_when_the_rights_are_already_there(monkeypatch):
    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    monkeypatch.setattr(elevate, "is_elevated", lambda: True)
    assert elevate.should_offer(True, already_tried=False) is False


def test_no_prompt_off_windows(monkeypatch):
    monkeypatch.setattr(elevate, "is_windows", lambda: False)
    assert elevate.should_offer(True, already_tried=False) is False


@pytest.mark.parametrize(
    "argument, quoted",
    [
        ("plain", "plain"),
        (r"C:\Users\Bob Smith", r'"C:\Users\Bob Smith"'),
        ("", '""'),
        ('has"quote', '"has\\"quote"'),
        ("tab\there", '"tab\there"'),
    ],
)
def test_arguments_are_quoted_for_a_single_command_line_string(argument, quoted):
    """ShellExecuteW takes the arguments as one string, so an unquoted profile
    path with a space -- which is most of them -- arrives as two arguments and
    the relaunched window backs up the wrong folder."""
    assert elevate.quote(argument) == quoted


def test_the_first_pages_choices_survive_the_restart():
    """The elevated window should open looking exactly like the one that
    disappeared, or the user has to set everything again having just been
    interrupted by a UAC dialog."""
    arguments = elevate.forward_arguments(
        profile_root=r"C:\Users\Bob Smith",
        files_only=True,
        include_wifi=True,
        include_software=False,
        include_notepad=False,
        compression="none",
    )
    assert arguments[0] == "gui"
    assert elevate.ALREADY_TRIED_FLAG in arguments
    assert "--profile-root" in arguments and r"C:\Users\Bob Smith" in arguments
    assert "--files-only" in arguments
    assert "--include-wifi" in arguments
    assert "--no-software" in arguments
    assert "--no-notepad" in arguments
    assert arguments[arguments.index("--compression") + 1] == "none"


def test_defaults_are_not_spelled_out_on_the_command_line():
    """Only what the user changed. A command line of every default is noise in
    the UAC dialog, which shows it."""
    assert elevate.forward_arguments() == ["gui", elevate.ALREADY_TRIED_FLAG]


def test_no_secret_ever_rides_along():
    """The passphrase is collected pages after this, in the process that will
    use it. A command line is visible in the process list to every account on
    the machine."""
    arguments = elevate.forward_arguments(
        profile_root=r"C:\Users\a", files_only=True, include_wifi=True
    )
    joined = " ".join(arguments).lower()
    for forbidden in ("pass", "secret", "key", "credential"):
        assert forbidden not in joined


def test_the_relaunch_names_the_same_code(monkeypatch):
    """Frozen, the executable is the program. From a checkout it is the
    interpreter, and -m names the package -- or the elevated copy would be
    whatever winmigrate happens to be on PATH."""
    monkeypatch.setattr(sys, "frozen", False, raising=False)
    executable, leading = elevate.relaunch_command()
    assert executable == sys.executable and leading == ["-m", "winmigrate"]

    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"E:\WinMigrate.exe")
    executable, leading = elevate.relaunch_command()
    assert executable == r"E:\WinMigrate.exe" and leading == []


def test_a_refused_prompt_is_not_an_error(monkeypatch):
    """Carrying on without a shadow copy is exactly what the command line does
    without elevation. It is a reduced backup, not a failed one."""
    monkeypatch.setattr(elevate, "is_windows", lambda: False)
    assert elevate.relaunch_as_admin(["gui"]) is False


def test_the_gui_command_accepts_everything_the_relaunch_sends():
    """The two halves are written in different files; a flag renamed on one side
    would strand the elevated window with an argparse error and no window."""
    from winmigrate.cli import build_parser

    arguments = elevate.forward_arguments(
        profile_root=r"C:\Users\Bob Smith",
        files_only=True,
        include_wifi=True,
        include_software=False,
        include_notepad=False,
        compression="none",
    )
    parsed = build_parser().parse_args(arguments)
    assert str(parsed.profile_root) == r"C:\Users\Bob Smith"
    assert parsed.files_only and parsed.include_wifi
    assert not parsed.include_software and not parsed.include_notepad
    assert parsed.compression == "none"
    assert parsed.elevation_attempted is True
