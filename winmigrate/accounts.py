"""Whose profile is this, and is it the account this program is running as?

Usually the same person, and nothing here matters. It stops being the same
person the moment somebody without administrator rights starts WinMigrate "as
administrator". Windows does not raise *their* rights -- it asks for somebody
else's password and runs the program as that other account. From then on
``HKEY_CURRENT_USER`` is the administrator's registry, ``Path.home()`` is the
administrator's folder, and the desktop the program changes is the
administrator's desktop, while the person watching the screen is still signed
in as themselves.

That is how a restore can report "desktop background: applied" and change
nothing anyone can see: it set the background of an account that was not
looking. The same goes for the taskbar, the login programs and the rest of the
personal settings, in both directions -- a backup started that way reads the
administrator's settings and files them under somebody else's name.

So the question is asked of the profile rather than of the process. Every
profile on the machine is listed, with its owner's security identifier, under
``ProfileList``; a signed-in account's registry is open under
``HKEY_USERS\\<SID>`` for as long as they are signed in. Reading and writing
there reaches the right person whichever account the program runs as.

Nothing here is secret: account names, SIDs and folder paths.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from .platform_win import HKLM, HKU, Environment
from .util import paths as pathutil

log = logging.getLogger(__name__)

PROFILE_LIST_KEY = r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\ProfileList"
#: Present in a signed-in account's hive, and only while they are signed in.
VOLATILE_KEY = "Volatile Environment"


def profile_owner(root: Path, env: Environment) -> str | None:
    """The SID whose profile folder is ``root``, or None if it is nobody's."""
    wanted = pathutil.normalize_key(root)
    for sid in env.registry_subkeys(HKLM, PROFILE_LIST_KEY):
        path = env.read_registry_value(HKLM, f"{PROFILE_LIST_KEY}\\{sid}", "ProfileImagePath")
        if not isinstance(path, str) or not path.strip():
            continue
        expanded = pathutil.expand(path.strip(), env.environ)
        if pathutil.normalize_key(expanded) == wanted:
            return sid
    return None


def signed_in(env: Environment) -> dict[str, dict[str, str]]:
    """Every account signed in right now: SID -> its user name and profile."""
    found: dict[str, dict[str, str]] = {}
    for sid in env.registry_subkeys(HKU, ""):
        if not sid.startswith("S-1-5-21-") or sid.endswith("_Classes"):
            continue
        values = env.read_registry_key(HKU, f"{sid}\\{VOLATILE_KEY}") or {}
        name = values.get("USERNAME")
        profile = values.get("USERPROFILE")
        if isinstance(name, str) and isinstance(profile, str):
            found[sid] = {
                "name": name,
                "domain": str(values.get("USERDOMAIN") or ""),
                "profile": profile,
            }
    return found


def current_sid() -> str | None:  # pragma: no cover -- Windows only
    """The SID this process runs as. None when it cannot be told."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        advapi32 = ctypes.windll.advapi32
        kernel32 = ctypes.windll.kernel32
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)
        ]
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)
        ]
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        token = wintypes.HANDLE()
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)  # TOKEN_QUERY
        ):
            return None
        try:
            size = wintypes.DWORD()
            advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))  # TokenUser
            buffer = ctypes.create_string_buffer(size.value)
            if not advapi32.GetTokenInformation(
                token, 1, buffer, size, ctypes.byref(size)
            ):
                return None
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            text = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
                return None
            try:
                return text.value
            finally:
                kernel32.LocalFree(ctypes.cast(text, ctypes.c_void_p))
        finally:
            kernel32.CloseHandle(token)
    except Exception as exc:  # noqa: BLE001 -- not knowing is the old behaviour
        log.debug("could not read this process's account: %s", exc)
        return None


def desktop_user() -> tuple[str, str] | None:  # pragma: no cover -- Windows only
    """(domain, name) of whoever is signed in to the desktop this runs on."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        kernel32 = ctypes.windll.kernel32
        wtsapi32 = ctypes.windll.wtsapi32
        session = wintypes.DWORD()
        if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
            return None
        wtsapi32.WTSQuerySessionInformationW.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
            ctypes.POINTER(wintypes.LPWSTR), ctypes.POINTER(wintypes.DWORD),
        ]
        wtsapi32.WTSFreeMemory.argtypes = [ctypes.c_void_p]

        def ask(what: int) -> str:
            text = wintypes.LPWSTR()
            size = wintypes.DWORD()
            if not wtsapi32.WTSQuerySessionInformationW(
                None, session.value, what, ctypes.byref(text), ctypes.byref(size)
            ):
                return ""
            try:
                return text.value or ""
            finally:
                wtsapi32.WTSFreeMemory(ctypes.cast(text, ctypes.c_void_p))

        name = ask(5)  # WTSUserName
        return (ask(7), name) if name else None  # WTSDomainName
    except Exception as exc:  # noqa: BLE001
        log.debug("could not ask who is signed in here: %s", exc)
        return None


def desktop_profile(env: Environment, me=current_sid, desktop=desktop_user) -> Path | None:
    """The signed-in person's profile, when this program is running as somebody else.

    None in the ordinary case -- the program runs as the person at the screen
    -- and whenever it cannot be told, which leaves everything as it was.
    """
    if not env.is_windows:
        return None
    who = desktop()
    mine = me()
    if not who or not mine:
        return None
    domain, name = who
    for sid, account in signed_in(env).items():
        if account["name"].lower() != name.lower():
            continue
        if domain and account["domain"] and account["domain"].lower() != domain.lower():
            continue
        if sid == mine:
            return None
        return Path(account["profile"])
    return None


def settle(env: Environment, owner: str | None, mine: str | None, loaded: bool) -> Environment:
    """Point ``env``'s personal registry at the owner of its profile.

    ``owner`` is the SID whose profile ``env`` is rooted at, ``mine`` the one
    this process runs as, and ``loaded`` whether the owner's registry is open.
    Nothing changes when they are the same person, or when either is unknown.
    """
    if not owner or not mine or owner == mine:
        return env
    if loaded:
        env.user_sid = owner
        log.info(
            "%s belongs to another account (%s); its settings are read and written "
            "there, not in the registry of the account running this program",
            env.profile_root, owner,
        )
    else:
        env.personal_registry_unreachable = True
        log.warning(
            "%s belongs to an account that is not signed in (%s), so its personal "
            "settings cannot be reached; they are left alone rather than taken from "
            "or written to the account running this program",
            env.profile_root, owner,
        )
    return env


def for_profile(root: os.PathLike[str] | str, me=current_sid) -> Environment:
    """The live machine, with the profile -- and the registry -- of ``root``'s owner."""
    root = Path(os.fspath(root))
    live = Environment.live()
    if pathutil.normalize_key(root) == pathutil.normalize_key(live.profile_root):
        env = live
    else:
        env = Environment.rooted(root)
    if not env.is_windows:
        return env
    try:
        owner = profile_owner(root, env)
        loaded = bool(owner) and env.registry_key_exists(HKU, owner or "")
        mine = me()
    except Exception:  # noqa: BLE001 -- not knowing is how it always was
        log.debug("could not tell whose profile %s is", root, exc_info=True)
        return env
    return settle(env, owner, mine, loaded)
