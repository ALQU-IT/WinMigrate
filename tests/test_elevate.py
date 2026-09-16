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


# --- opening the user's browser from an elevated window ---------------------
def test_an_elevated_window_launches_the_browser_as_the_user(monkeypatch, tmp_path):
    """Chromium keeps one instance per profile, and the one the user has open is
    theirs at medium integrity. An elevated launch cannot hand its command line
    across that boundary: the window comes to the front without going anywhere,
    which is precisely what "it opens the browser, but it doesn't go to the
    link" looks like. So when this process is elevated, the browser is started
    with the desktop owner's token instead -- a step down in privilege, and the
    only way to reach the instance that is already running."""
    from pathlib import Path

    from winmigrate import winlaunch

    calls: list[str] = []
    exe = tmp_path / "brave.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(winlaunch, "is_windows", lambda: True)
    monkeypatch.setattr(winlaunch, "is_elevated", lambda: True)
    monkeypatch.setattr(
        winlaunch,
        "launch_as_shell_user",
        lambda executable, arguments: calls.append("as-user") or True,
    )
    monkeypatch.setattr(
        winlaunch.subprocess, "Popen", lambda *a, **k: calls.append("elevated")
    )

    assert winlaunch.launch(Path(exe), ["brave://password-manager/settings"]) is True
    assert calls == ["as-user"]


def test_it_still_opens_when_the_users_token_cannot_be_borrowed(monkeypatch, tmp_path):
    """Group policy, a locked-down machine, no shell window. An elevated browser
    that at least opens beats no browser at all, and the address is on the
    clipboard regardless."""
    from pathlib import Path

    from winmigrate import winlaunch

    calls: list[str] = []
    exe = tmp_path / "brave.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(winlaunch, "is_windows", lambda: True)
    monkeypatch.setattr(winlaunch, "is_elevated", lambda: True)
    monkeypatch.setattr(winlaunch, "launch_as_shell_user", lambda executable, arguments: False)
    monkeypatch.setattr(
        winlaunch.subprocess, "Popen", lambda *a, **k: calls.append("elevated")
    )

    assert winlaunch.launch(Path(exe), ["x"]) is True
    assert calls == ["elevated"]


def test_an_ordinary_window_does_not_go_near_tokens(monkeypatch, tmp_path):
    """Borrowing a token needs a privilege an ordinary process does not have,
    and does not need: its launch already reaches the user's browser."""
    from pathlib import Path

    from winmigrate import winlaunch

    calls: list[str] = []
    exe = tmp_path / "brave.exe"
    exe.write_bytes(b"MZ")

    monkeypatch.setattr(winlaunch, "is_windows", lambda: True)
    monkeypatch.setattr(winlaunch, "is_elevated", lambda: False)
    monkeypatch.setattr(
        winlaunch, "launch_as_shell_user",
        lambda executable, arguments: calls.append("as-user") or True,
    )
    monkeypatch.setattr(
        winlaunch.subprocess, "Popen", lambda *a, **k: calls.append("elevated")
    )

    assert winlaunch.launch(Path(exe), ["x"]) is True
    assert calls == ["elevated"]


def test_nothing_is_launched_off_windows(tmp_path):
    from pathlib import Path

    from winmigrate import winlaunch

    assert winlaunch.launch(Path(tmp_path / "brave.exe"), ["x"]) is False
    assert winlaunch.launch_as_shell_user(Path(tmp_path / "brave.exe"), ["x"]) is False


# --- elevating one step, in the middle of the wizard ------------------------
def test_one_step_can_be_elevated_without_restarting_the_program(monkeypatch):
    """The tick on the first page restarts the whole program, which is right
    before anything has been done and wrong afterwards: a restore that has
    finished is exactly what the restart would throw away. So the install
    button elevates winget alone -- the same single prompt, the same elevated
    install, and the report still on screen behind it."""
    monkeypatch.setattr(elevate, "is_windows", lambda: False)
    assert elevate.start_elevated("winget", ["import", "-i", "x.json"]) is None
    assert elevate.wait_for(4242) is None


def test_waiting_on_nothing_is_not_an_error(monkeypatch):
    """A refused prompt hands back no handle. Waiting on it must return rather
    than reach into ctypes with a zero."""
    monkeypatch.setattr(elevate, "is_windows", lambda: True)
    assert elevate.wait_for(0) is None
