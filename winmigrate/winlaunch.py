"""Starting a program as the person sitting at the machine.

WinMigrate asks for administrator rights once, at the start, because a shadow
copy needs them -- so by the time the wizard reaches the browser password page
it is usually running elevated. That turns out to matter for one thing: opening
the user's browser.

An elevated process launching ``brave.exe`` does not reach the Brave the user
already has open. Chromium keeps one instance per profile, and the running one
is theirs at medium integrity; the elevated launch cannot hand its command line
across that boundary, cannot take the profile lock either, and the visible
result is a browser window that comes to the front **without going anywhere**.

That was half of "it opens the browser, but it doesn't go to the link". The
other half is not a privilege problem at all and is not solved here: Chromium
refuses to navigate to its own internal pages on another program's say-so, so
the address has to be one it accepts. See
:data:`winmigrate.passwords.LANDING_PAGES`.

The fix is to launch it as the user rather than as the administrator, which is
a step *down* in privilege and the right way round in every sense: the browser
should not be running elevated, and a de-elevated launch talks to the instance
the user already has. Windows has no single call for it; the standard technique
is to borrow the token of the process that owns the desktop -- the shell,
usually explorer.exe -- and start the program with that.

Everything here fails soft. A failure means the caller falls back to launching
normally, and past that to telling the user the address, which is a page they
can reach themselves.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

#: DuplicateTokenEx / CreateProcessWithTokenW constants, named rather than
#: spelled as magic numbers at the call site.
_TOKEN_DUPLICATE = 0x0002
_TOKEN_QUERY = 0x0008
_TOKEN_ASSIGN_PRIMARY = 0x0001
_MAXIMUM_ALLOWED = 0x02000000
_SECURITY_IMPERSONATION = 2
_TOKEN_PRIMARY = 1
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_CREATE_NEW_CONSOLE = 0x00000010


def is_windows() -> bool:
    return sys.platform == "win32"


def is_elevated() -> bool:
    from . import vss  # noqa: PLC0415 -- the same check the capture makes

    return vss.is_elevated()


def quote(argument: str) -> str:
    """Quote one argument for a Windows command line."""
    if not argument:
        return '""'
    if not any(character in argument for character in ' \t"'):
        return argument
    return '"' + argument.replace('"', r"\"") + '"'


def launch(executable: Path, arguments: list[str]) -> bool:
    """Start ``executable``, as the signed-in user when this process is elevated.

    Returns True when a process was started. The de-elevated path is tried first
    and only when it is needed; if it cannot be taken, this falls back to an
    ordinary launch rather than refusing, because an elevated browser that at
    least opens is better than no browser at all.
    """
    if not is_windows():
        return False
    if is_elevated() and launch_as_shell_user(executable, arguments):
        log.info("launched %s as the signed-in user", executable.name)
        return True
    try:
        # No shell and no "start": the program resolves its own arguments, and
        # nothing passes through a command interpreter that could reinterpret
        # them.
        subprocess.Popen(  # noqa: S603 -- a fixed executable, resolved by the caller
            [str(executable), *arguments],
            close_fds=True,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not launch %s: %s", executable, exc)
        return False


def launch_as_shell_user(executable: Path, arguments: list[str]) -> bool:
    """Start a program with the desktop owner's token. True when it started.

    The desktop owner is whoever the shell is running as -- the person at the
    keyboard -- found through the shell window rather than by guessing at a
    session id. Borrowing a token needs SeImpersonatePrivilege, which an
    elevated process has and an ordinary one does not, so this is only ever
    useful in the case it exists for.
    """
    if not is_windows():
        return False
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415
    except ImportError:  # pragma: no cover -- ctypes is in the standard library
        return False

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    user32 = ctypes.WinDLL("user32", use_last_error=True)

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    advapi32.CreateProcessWithTokenW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPWSTR,
        wintypes.DWORD, wintypes.LPVOID, wintypes.LPCWSTR,
        ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION),
    ]
    advapi32.CreateProcessWithTokenW.restype = wintypes.BOOL
    advapi32.DuplicateTokenEx.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID, ctypes.c_int,
        ctypes.c_int, ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi32.DuplicateTokenEx.restype = wintypes.BOOL
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
    ]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    # Declared rather than left to ctypes' default int conversion: a handle is
    # pointer-sized, and a 64-bit one passed as a C int is a different handle.
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD

    shell_pid = _shell_process_id(user32, wintypes)
    if not shell_pid:
        log.info("no shell window, so no user token to borrow")
        return False

    process = token = duplicate = None
    try:
        process = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, shell_pid)
        if not process:
            log.info("could not open the shell process: %s", ctypes.get_last_error())
            return False
        token = wintypes.HANDLE()
        access = _TOKEN_DUPLICATE | _TOKEN_QUERY | _TOKEN_ASSIGN_PRIMARY
        if not advapi32.OpenProcessToken(process, access, ctypes.byref(token)):
            log.info("could not open the shell's token: %s", ctypes.get_last_error())
            return False
        duplicate = wintypes.HANDLE()
        if not advapi32.DuplicateTokenEx(
            token, _MAXIMUM_ALLOWED, None, _SECURITY_IMPERSONATION,
            _TOKEN_PRIMARY, ctypes.byref(duplicate),
        ):
            log.info("could not duplicate the shell's token: %s", ctypes.get_last_error())
            return False

        startup = STARTUPINFOW()
        startup.cb = ctypes.sizeof(STARTUPINFOW)
        information = PROCESS_INFORMATION()
        command = ctypes.create_unicode_buffer(
            " ".join(quote(part) for part in [str(executable), *arguments])
        )
        started = advapi32.CreateProcessWithTokenW(
            duplicate, 0, str(executable), command, _CREATE_NEW_CONSOLE,
            None, str(executable.parent), ctypes.byref(startup), ctypes.byref(information),
        )
        if not started:
            log.info("could not start %s as the user: %s", executable.name,
                     ctypes.get_last_error())
            return False
        kernel32.CloseHandle(information.hProcess)
        kernel32.CloseHandle(information.hThread)
        return True
    except OSError as exc:  # pragma: no cover -- Windows-only failure path
        log.warning("could not start %s as the user: %s", executable.name, exc)
        return False
    finally:
        for handle in (duplicate, token):
            if handle is not None and getattr(handle, "value", None):
                kernel32.CloseHandle(handle)
        if process:
            kernel32.CloseHandle(process)


def _shell_process_id(user32, wintypes) -> int:
    """The process that owns the desktop -- explorer.exe, normally.

    Asked of the window rather than assumed, because a machine can be running
    something other than Explorer as its shell, and because this is the process
    whose token is the one sitting in front of the screen.
    """
    import ctypes  # noqa: PLC0415

    user32.GetShellWindow.restype = wintypes.HWND
    window = user32.GetShellWindow()
    if not window:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
    return int(pid.value)
