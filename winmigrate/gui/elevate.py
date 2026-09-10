"""Asking Windows for administrator rights, once, at the right moment.

A shadow copy is the difference between a backup that includes the files your
browser and Outlook are holding open and one that skips them, and creating one
needs administrator rights. The console tool can only tell the user to start
again from an elevated prompt. A window can do better: ask Windows to relaunch
it, which is the UAC dialog everyone already knows.

**When** matters more than how. Elevation restarts the process, so anything
gathered beforehand is lost -- a scan of a real profile takes minutes, and
throwing that away because the user was asked too late would be worse than not
asking. So the prompt comes at the very start, before the scan, and the
relaunched copy carries the choices made on the first page forward on its
command line.

Nothing secret goes on that command line. The passphrase is collected pages
later, in the elevated process, and never leaves the widget it was typed into.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: Set on the relaunched copy so it does not ask again. A refused prompt must
#: not turn into a loop of refused prompts.
ALREADY_TRIED_FLAG = "--elevation-attempted"


def is_windows() -> bool:
    return sys.platform == "win32"


def is_elevated() -> bool:
    """True when this process can already create a shadow copy."""
    from .. import vss  # noqa: PLC0415 -- the same check the capture uses

    return vss.is_elevated()


def should_offer(want_shadow_copy: bool, already_tried: bool) -> bool:
    """Is there any point showing a UAC prompt?

    Not when the user does not want a shadow copy, not when they already have
    the rights, not when this is the copy that was just relaunched, and not on
    a platform with no such thing.
    """
    if not want_shadow_copy or already_tried:
        return False
    if not is_windows():
        return False
    return not is_elevated()


def relaunch_command() -> tuple[str, list[str]]:
    """``(executable, leading arguments)`` for restarting this program.

    Frozen by PyInstaller the executable is the program itself and there is no
    script to name. From a source checkout it is the interpreter, and the
    package has to be named with ``-m`` so the relaunched copy is the same code.
    """
    if getattr(sys, "frozen", False):
        return sys.executable, []
    return sys.executable, ["-m", "winmigrate"]


def quote(argument: str) -> str:
    """Quote one argument for the Windows command line.

    ShellExecuteW takes the arguments as a single string, so a profile path with
    a space in it -- which is most of them -- has to be quoted or it arrives as
    two arguments.
    """
    if not argument:
        return '""'
    if not any(ch in argument for ch in ' \t"'):
        return argument
    return '"' + argument.replace('"', r"\"") + '"'


def relaunch_as_admin(arguments: list[str]) -> bool:
    """Ask Windows to start this program again, elevated.

    Returns True when a new process was started and this one should exit, and
    False when it could not be -- the user clicked No, or there is no UAC to ask.
    A False is not an error: the caller carries on without a shadow copy and
    says so, which is exactly what the command line does.
    """
    if not is_windows():
        return False
    executable, leading = relaunch_command()
    parameters = " ".join(quote(part) for part in [*leading, *arguments])
    try:
        import ctypes  # noqa: PLC0415

        # SW_SHOWNORMAL = 1. A return value above 32 means it started.
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", executable, parameters, str(Path.cwd()), 1
        )
        started = int(result) > 32
        if not started:
            # 5 is ERROR_ACCESS_DENIED, which is what a declined UAC prompt
            # gives back. Nothing to report to the user beyond carrying on.
            log.info("elevation declined or unavailable (ShellExecuteW returned %s)", result)
        return started
    except Exception as exc:  # noqa: BLE001 -- never let this stop the window opening
        log.warning("could not request elevation: %s", exc)
        return False


def forward_arguments(
    *,
    profile_root: str = "",
    files_only: bool = False,
    include_wifi: bool = False,
    include_software: bool = True,
    include_notepad: bool = True,
    compression: str = "auto",
) -> list[str]:
    """The first page's choices, as arguments for the relaunched copy.

    Only what the user set before elevating, so the elevated window opens
    looking exactly like the one that disappeared. No passphrase, no output
    path: those are collected afterwards, in the process that will use them.
    """
    arguments = ["gui", ALREADY_TRIED_FLAG]
    if profile_root:
        arguments += ["--profile-root", profile_root]
    if files_only:
        arguments.append("--files-only")
    if include_wifi:
        arguments.append("--include-wifi")
    if not include_software:
        arguments.append("--no-software")
    if not include_notepad:
        arguments.append("--no-notepad")
    if compression and compression != "auto":
        arguments += ["--compression", compression]
    return arguments
