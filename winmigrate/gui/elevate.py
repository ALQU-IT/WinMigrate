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


def should_offer(wanted: bool, already_tried: bool) -> bool:
    """Is there any point showing a UAC prompt?

    Not when the user has not asked for anything that needs it, not when they
    already have the rights, not when this is the copy that was just
    relaunched, and not on a platform with no such thing.

    Two things ask. Backing up wants a shadow copy, so files a program is
    holding open are copied rather than skipped. Restoring wants to install
    the software: winget can install for the machine rather than for one
    account, and without the rights it either refuses or asks once per
    program, which for ninety-seven of them is not a migration, it is an
    afternoon of clicking Yes.
    """
    if not wanted or already_tried:
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


#: ShellExecuteEx, so the started process can be waited on rather than fired
#: into the dark. SEE_MASK_NOCLOSEPROCESS is what makes it hand back a handle.
_SEE_MASK_NOCLOSEPROCESS = 0x00000040
_SW_SHOWNORMAL = 1
_WAIT_OBJECT_0 = 0x00000000
_STILL_ACTIVE = 259


def start_elevated(executable: str, arguments: list[str]) -> int | None:
    """Run one program elevated, and hand back a handle to it. None if refused.

    This is how a step in the middle of the wizard can ask for administrator
    rights without the whole program restarting. Relaunching is right at the
    start, before anything has been done; it is useless after a restore has
    finished, because the restore is the thing that would be thrown away.

    The program runs in its own window, which is not a compromise: winget
    showing its own progress is better than a bar somebody has to trust, and
    there is no pipe to inherit, which is the mistake this codebase has already
    made once.
    """
    if not is_windows():
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        class SHELLEXECUTEINFOW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("fMask", ctypes.c_ulong),
                ("hwnd", wintypes.HWND),
                ("lpVerb", wintypes.LPCWSTR),
                ("lpFile", wintypes.LPCWSTR),
                ("lpParameters", wintypes.LPCWSTR),
                ("lpDirectory", wintypes.LPCWSTR),
                ("nShow", ctypes.c_int),
                ("hInstApp", wintypes.HINSTANCE),
                ("lpIDList", ctypes.c_void_p),
                ("lpClass", wintypes.LPCWSTR),
                ("hkeyClass", wintypes.HKEY),
                ("dwHotKey", wintypes.DWORD),
                ("hIcon", wintypes.HANDLE),
                ("hProcess", wintypes.HANDLE),
            ]

        shell32 = ctypes.windll.shell32
        shell32.ShellExecuteExW.argtypes = [ctypes.POINTER(SHELLEXECUTEINFOW)]
        shell32.ShellExecuteExW.restype = wintypes.BOOL

        information = SHELLEXECUTEINFOW()
        information.cbSize = ctypes.sizeof(SHELLEXECUTEINFOW)
        information.fMask = _SEE_MASK_NOCLOSEPROCESS
        information.lpVerb = "runas"
        information.lpFile = executable
        information.lpParameters = " ".join(quote(part) for part in arguments)
        information.lpDirectory = str(Path.cwd())
        information.nShow = _SW_SHOWNORMAL
        if not shell32.ShellExecuteExW(ctypes.byref(information)):
            # A declined prompt lands here, and is not an error to report as one.
            log.info("elevation declined or unavailable (ShellExecuteExW failed)")
            return None
        return int(information.hProcess or 0) or None
    except Exception as exc:  # noqa: BLE001 -- never let this stop the window
        log.warning("could not start %s elevated: %s", executable, exc)
        return None


def wait_for(handle: int, waiting=None, poll_ms: int = 500) -> int | None:
    """Wait for a process started by :func:`start_elevated`. Returns its code.

    ``waiting`` is asked between polls and returning False gives up watching --
    which is all it can do. A program running with administrator rights cannot
    be stopped by one running without them, so "stop" here means stop waiting,
    and the window says so rather than pretending otherwise.
    """
    if not is_windows() or not handle:
        return None
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.windll.kernel32
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)
        ]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        try:
            while True:
                if kernel32.WaitForSingleObject(handle, poll_ms) == _WAIT_OBJECT_0:
                    break
                if waiting is not None and not waiting():
                    return None
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            value = int(code.value)
            return None if value == _STILL_ACTIVE else value
        finally:
            kernel32.CloseHandle(handle)
    except Exception as exc:  # noqa: BLE001
        log.warning("could not wait for the elevated process: %s", exc)
        return None


def forward_arguments(
    *,
    mode: str = "",
    restore_as_admin: bool = False,
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
    # The relaunched copy has to come back on the same branch. Without this it
    # reopens on the first page and offers to back this machine up, which to
    # somebody halfway through a restore reads as the program having forgotten
    # what they asked for -- and the one thing worse than that is their
    # agreeing to it.
    if mode:
        arguments += ["--mode", mode]
    if restore_as_admin:
        arguments.append("--restore-as-admin")
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
