"""Re-applying the settings that do not need a person.

The design of this tool stops at anything the operating system deliberately
puts a human in front of: account sign-ins, licence activation, a password store
that wants Windows Hello. That boundary is about *identity*, and it has been
quietly doing more work than it should -- four things ended up on the follow-up
list that need no identity at all and are one command each.

Re-adding eleven printers by hand is not consent, it is tedium. So Wi-Fi
profiles, network printers, mapped drives and environment variables are applied
here, and the follow-up list shrinks to the things that genuinely need the
person standing there.

Every one of these follows the same rules:

* **It reports what it did.** A setting silently applied is indistinguishable
  from one silently skipped, and the whole premise of the tool is showing its
  work.
* **A failure is a result, not an exception.** One printer whose driver is
  missing must not stop the other ten, and it must not stop the restore.
* **Nothing is overwritten that the new machine may need.** PATH is the sharp
  edge here, and it is left alone deliberately -- see :data:`MACHINE_OWNED_VARS`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from .util import paths as pathutil, process

log = logging.getLogger(__name__)


class Outcome(str, Enum):
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(slots=True)
class Result:
    """What happened to one setting."""

    kind: str          # "wifi" | "printer" | "drive" | "env"
    name: str
    outcome: Outcome
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome is not Outcome.FAILED


@dataclass(slots=True)
class ApplyReport:
    results: list[Result] = field(default_factory=list)

    def add(self, result: Result) -> None:
        self.results.append(result)
        log.info("%s %s: %s %s", result.kind, result.name, result.outcome.value, result.detail)

    def counts(self) -> dict[str, int]:
        counts = {outcome.value: 0 for outcome in Outcome}
        for result in self.results:
            counts[result.outcome.value] += 1
        return counts

    @property
    def applied(self) -> int:
        return sum(1 for r in self.results if r.outcome is Outcome.APPLIED)

    @property
    def failures(self) -> list[Result]:
        return [r for r in self.results if r.outcome is Outcome.FAILED]


# --- Wi-Fi -----------------------------------------------------------------
def apply_wifi(profile_dir: Path, runner=process.run) -> list[Result]:
    """Import the exported Wi-Fi profiles.

    ``user=current`` rather than ``all``: the profiles came out of one user's
    account and belong back in one user's account, and importing them
    machine-wide would hand every account on the new computer the network
    passwords from the old one.
    """
    results: list[Result] = []
    if not profile_dir.is_dir():
        return results
    for path in sorted(profile_dir.glob("*.xml")):
        name = path.stem
        try:
            completed = runner(
                # filename=<path>, not filename="<path>". The quotes are what
                # you type at a prompt, where the shell strips them again. Here
                # the argument list goes to CreateProcess, which escapes the
                # quotes it is given -- netsh then receives filename=\"C:\...\"
                # and looks for a file whose name starts with a quote mark. The
                # export side next door already had this right, so every
                # network went out correctly and none of them came back.
                ["netsh", "wlan", "add", "profile", f"filename={path}", "user=current"],
                timeout=30,
            )
        except Exception as exc:  # noqa: BLE001 -- one network must not stop the rest
            results.append(Result("wifi", name, Outcome.FAILED, str(exc)))
            continue
        if completed.returncode == 0:
            results.append(Result("wifi", name, Outcome.APPLIED))
        else:
            results.append(
                Result("wifi", name, Outcome.FAILED, _first_line(completed.stderr or completed.stdout))
            )
    return results


# --- printers --------------------------------------------------------------
def apply_printers(record: dict[str, Any], runner=process.run) -> list[Result]:
    """Re-add the network printers, and say so about the ones that cannot be.

    Only UNC connections are re-added. A locally attached printer is a driver
    and a USB cable, neither of which a backup can produce, so it stays on the
    follow-up list where a person can plug it in.
    """
    results: list[Result] = []
    for connection in record.get("connections", []):
        if isinstance(connection, str) and not _safe_argument(connection):
            # The record comes out of a bundle, which may have been written on
            # another machine. printui does its own parsing of what it is
            # handed, so a name carrying quotes or line breaks is refused
            # rather than passed on to be interpreted.
            results.append(
                Result("printer", connection.strip()[:60], Outcome.SKIPPED,
                       "the name in the backup is not one this can pass on safely")
            )
            continue
        if not isinstance(connection, str) or not connection.startswith("\\\\"):
            results.append(
                Result("printer", str(connection), Outcome.SKIPPED,
                       "not a network printer; it needs its driver installing")
            )
            continue
        try:
            completed = runner(
                ["rundll32", "printui.dll,PrintUIEntry", "/in", "/n", connection, "/q"],
                timeout=120,
            )
        except Exception as exc:  # noqa: BLE001
            results.append(Result("printer", connection, Outcome.FAILED, str(exc)))
            continue
        if completed.returncode == 0:
            results.append(Result("printer", connection, Outcome.APPLIED))
        else:
            results.append(
                Result("printer", connection, Outcome.FAILED,
                       _first_line(completed.stderr or completed.stdout)
                       or f"exit code {completed.returncode}")
            )

    default = record.get("default")
    if isinstance(default, str) and default.startswith("\\\\") and _safe_argument(default):
        applied = {r.name for r in results if r.outcome is Outcome.APPLIED}
        if default in applied:
            try:
                completed = runner(
                    ["rundll32", "printui.dll,PrintUIEntry", "/y", "/n", default], timeout=60
                )
                results.append(
                    Result("printer", f"{default} (default)",
                           Outcome.APPLIED if completed.returncode == 0 else Outcome.FAILED)
                )
            except Exception as exc:  # noqa: BLE001
                results.append(Result("printer", f"{default} (default)", Outcome.FAILED, str(exc)))
    return results


def _safe_argument(text: str) -> bool:
    """Can this be handed to a tool that parses its own command line?

    Quotes, control characters and line breaks are the ones that change the
    meaning of an argument rather than being part of it. A real printer share
    or UNC path contains none of them, so refusing is free; and the value comes
    from a bundle, which is a file from another machine.
    """
    return bool(text) and not any(character in text for character in '"\r\n\t\x00')


# --- mapped drives ---------------------------------------------------------
def apply_mapped_drives(record: dict[str, Any], runner=process.run) -> list[Result]:
    """Re-map the network drives, persistently, without credentials.

    No password is supplied and none was captured. A share that needs one will
    prompt the user the first time they open it, which is the operating system
    asking for an identity -- exactly the line this tool does not cross.
    """
    results: list[Result] = []
    for letter, remote in sorted((record.get("drives") or {}).items()):
        if not isinstance(remote, str) or not remote.startswith("\\\\"):
            continue
        if not _safe_argument(remote):
            continue
        # A drive is one letter. Anything else in that field did not come from
        # a mapped drive, and "net use" takes switches in the same position.
        plain = str(letter).rstrip(":")
        if len(plain) != 1 or not plain.isalpha():
            results.append(
                Result("drive", str(letter)[:20], Outcome.SKIPPED,
                       "not a drive letter")
            )
            continue
        drive = f"{plain.upper()}:"
        try:
            completed = runner(
                ["net", "use", drive, remote, "/persistent:yes"], timeout=60
            )
        except Exception as exc:  # noqa: BLE001
            results.append(Result("drive", f"{drive} {remote}", Outcome.FAILED, str(exc)))
            continue
        if completed.returncode == 0:
            results.append(Result("drive", f"{drive} {remote}", Outcome.APPLIED))
        else:
            results.append(
                Result("drive", f"{drive} {remote}", Outcome.FAILED,
                       _first_line(completed.stderr or completed.stdout))
            )
    return results


# --- environment variables -------------------------------------------------
#: Variables that describe the machine rather than the user, and must not be
#: carried across.
#:
#: PATH is the dangerous one, and it is handled above by :data:`MERGED_VARS`
#: before this set is ever consulted. It stays listed here as well: if that
#: merge is ever bypassed, the fallback must be to leave PATH alone rather than
#: to overwrite the new machine's copy with the old machine's.
#: The one machine-owned variable worth merging rather than leaving behind.
#:
#: A user PATH is two things at once: entries that describe where software was
#: installed on the old disk, and entries somebody added on purpose. Copying it
#: wholesale breaks every tool that is not in the same place on the new machine;
#: leaving it out means re-adding the deliberate ones from memory, which is no
#: better if you cannot remember them.
#:
#: So it is merged. This machine's entries stay, in order and first; the old
#: ones are appended where they are not already there and the folder actually
#: exists here -- the restore has already put the files back by this point, so
#: a directory that travelled is a directory that exists. Entries pointing at
#: folders this machine does not have are dropped and named, which is the part
#: that could not be recovered from memory anyway.
MERGED_VARS = frozenset({"path"})

MACHINE_OWNED_VARS = frozenset(
    {"path", "temp", "tmp", "windir", "systemroot", "systemdrive", "username",
     "userprofile", "userdomain", "computername", "homedrive", "homepath",
     "appdata", "localappdata", "programdata", "public", "onedrive",
     "onedriveconsumer", "onedrivecommercial", "processor_architecture",
     "number_of_processors", "os", "comspec", "pathext", "psmodulepath"}
)


def apply_environment(record: dict[str, Any], env=None) -> list[Result]:
    """Write the user's own environment variables back.

    Per-user only: this writes HKCU and never HKLM, so nothing here can affect
    another account on the machine.
    """
    results: list[Result] = []
    variables = record.get("variables")
    if not isinstance(variables, dict):
        return results

    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()

    for name, value in sorted(variables.items()):
        if not isinstance(name, str) or not isinstance(value, str):
            continue
        if name.lower() in MERGED_VARS:
            results.extend(_merge_variable(env, name, value))
            continue
        if name.lower() in MACHINE_OWNED_VARS:
            results.append(
                Result("env", name, Outcome.SKIPPED,
                       "describes the machine rather than you, so the new machine's "
                       "own value was left alone")
            )
            continue
        try:
            written = env.write_registry_value("HKCU", "Environment", name, value)
        except Exception as exc:  # noqa: BLE001
            results.append(Result("env", name, Outcome.FAILED, str(exc)))
            continue
        results.append(
            Result("env", name, Outcome.APPLIED if written else Outcome.FAILED)
        )

    if any(r.outcome is Outcome.APPLIED for r in results):
        _broadcast_environment_change()
    return results


# --- the taskbar and the desktop -------------------------------------------
def apply_shell_layout(
    taskbar: dict[str, Any] | None,
    desktop: dict[str, Any] | None,
    start_menu: dict[str, Any] | None,
    env=None,
) -> list[Result]:
    """Put the taskbar and desktop layout back, and let Explorer see it.

    These are opaque blobs Windows never documented, so they are written as
    they were read. The one judgement made here is about the Start menu, whose
    format changes between Windows releases: it is put back only onto a machine
    of the release it came from. A Start menu that has to rebuild itself is a
    nuisance; one half-transplanted from another Windows version is worse, and
    the person it happens to has no way to know why their computer looks broken.
    """
    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()

    from .scan.shell import (  # noqa: PLC0415
        DESKTOP_BAG_KEY,
        TASKBAND_KEY,
        TASKBAND_VALUES,
        windows_build,
    )

    results: list[Result] = []
    if taskbar:
        results.append(
            _write_blobs(env, TASKBAND_KEY, taskbar, "your taskbar", TASKBAND_VALUES)
        )
    if desktop:
        results.append(
            _write_blobs(env, DESKTOP_BAG_KEY, desktop, "your desktop icons", None)
        )
    if start_menu is not None:
        results.append(_check_start_menu(start_menu, windows_build(env)))

    if any(result.outcome is Outcome.APPLIED for result in results):
        results.append(_restart_explorer(env))
    return results


def _write_blobs(env, key: str, record: dict[str, Any], what: str, allowed) -> Result:
    """Write a record of registry blobs back under ``key``.

    ``allowed`` names what may be written, or is None for a key whose value
    names are themselves data. Either way the record comes out of a bundle, so
    what it names is checked rather than trusted.
    """
    from .scan.shell import decode  # noqa: PLC0415

    values = record.get("values")
    if not isinstance(values, dict):
        return Result("layout", what, Outcome.SKIPPED, "nothing recorded")

    written = 0
    for name, raw in sorted(values.items()):
        if not isinstance(name, str):
            continue
        if allowed is not None and name not in allowed:
            continue
        if allowed is None and not name.startswith("ItemPos"):
            continue
        value = decode(raw)
        if value is None:
            continue
        try:
            if isinstance(value, bytes):
                ok = env.write_registry_binary("HKCU", key, name, value)
            elif isinstance(value, int):
                ok = env.write_registry_dword("HKCU", key, name, value)
            else:
                ok = env.write_registry_value("HKCU", key, name, str(value))
        except Exception as exc:  # noqa: BLE001
            return Result("layout", what, Outcome.FAILED, str(exc))
        written += int(bool(ok))

    if not written:
        return Result("layout", what, Outcome.SKIPPED, "nothing in it could be written")
    return Result("layout", what, Outcome.APPLIED, f"{written} value(s)")


def _check_start_menu(record: dict[str, Any], build: str) -> Result:
    """The Start menu layout travels as a file; this decides whether it stays.

    The file has already been written by the time this runs, because it is an
    ordinary member of the bundle. What happens here is the judgement: on a
    different Windows release it is moved aside rather than left in place.
    """
    came_from = str(record.get("windows_build") or "").strip()
    if not build or not came_from:
        return Result("layout", "your Start menu", Outcome.SKIPPED,
                      "cannot tell which Windows release this came from")
    if came_from != build:
        path = record.get("restored_path")
        moved = _move_aside(Path(str(path))) if path else False
        return Result(
            "layout", "your Start menu", Outcome.SKIPPED,
            f"it came from Windows build {came_from} and this is {build}"
            + ("; the file was set aside" if moved else ""),
        )
    return Result("layout", "your Start menu", Outcome.APPLIED,
                  f"same Windows release ({build})")


def _move_aside(path: Path) -> bool:
    """Rename a restored file out of the way. Never raises."""
    try:
        if not path.is_file():
            return False
        path.replace(path.with_suffix(path.suffix + ".from-other-windows"))
        return True
    except OSError as exc:  # noqa: BLE001
        log.info("could not set aside %s: %s", path, exc)
        return False


def refresh_shell(env=None) -> Result:
    """Restart Explorer again, once the software is actually installed.

    The taskbar is put back during the restore, which is before the programs it
    points at exist: a pin is a shortcut, and a shortcut to a program that is
    not there yet resolves to a blank icon that Explorer then remembers. Doing
    this once more after the install is what turns that row of blank squares
    back into the icons somebody recognises -- which is the entire reason the
    taskbar was carried at all.
    """
    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()
    return _restart_explorer(env)


def _restart_explorer(env) -> Result:
    """Restart Explorer so the taskbar shows what was just written.

    Without this none of it is visible until the next sign-in, and a migration
    that finishes with the old taskbar still on screen reads as one that did
    not work. Explorer restarting is something Windows does to itself routinely;
    what is on screen flickers and comes back.
    """
    if not touches_machine(env):
        return Result("layout", "Explorer", Outcome.SKIPPED, "not this machine")
    stopped = process.run(["taskkill", "/f", "/im", "explorer.exe"], timeout=30)
    # Windows restarts Explorer by itself in most configurations; starting it
    # explicitly covers the ones where it does not, and is harmless when it has
    # already come back.
    started = process.run(["cmd", "/c", "start", "", "explorer.exe"], timeout=30)
    if stopped.error and started.error:
        return Result("layout", "Explorer", Outcome.SKIPPED,
                      "could not restart it; the taskbar appears at the next sign-in")
    return Result("layout", "Explorer", Outcome.APPLIED, "restarted, so the taskbar shows")


# --- what starts when you log in -------------------------------------------
def apply_startup(record: dict[str, Any], env=None) -> list[Result]:
    """Re-add the login programs whose program is actually on this machine.

    Each entry is named in its own result rather than being written quietly and
    counted. A Run entry is a command Windows executes at every login, which
    makes it the one setting here worth seeing restored one at a time -- the
    user's own list, on screen, in a tool whose premise is showing its work.

    An entry whose program is missing is left out, not restored broken. That is
    both the safer answer and the more useful one: a dead Run entry is an error
    box at every login, for ever, naming a path the user has never seen.
    """
    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()

    from .scan.startup import RUN_KEY, executable_of  # noqa: PLC0415

    entries = record.get("entries")
    if not isinstance(entries, dict):
        return []

    results: list[Result] = []
    for name, command in sorted(entries.items()):
        if not isinstance(name, str) or not isinstance(command, str) or not command.strip():
            continue
        program = executable_of(command)
        if not program or not Path(pathutil.to_posix(pathutil.expand(program, env.environ))).exists():
            results.append(
                Result("startup", name, Outcome.SKIPPED,
                       f"{program or 'its program'} is not on this machine")
            )
            continue
        try:
            written = env.write_registry_value("HKCU", RUN_KEY, name, command)
        except Exception as exc:  # noqa: BLE001
            results.append(Result("startup", name, Outcome.FAILED, str(exc)))
            continue
        results.append(
            Result("startup", name, Outcome.APPLIED if written else Outcome.FAILED, program)
        )
    return results


# --- how Windows looks and responds ----------------------------------------
#: Settings that only take effect once Windows is told, rather than at the next
#: sign-in. SystemParametersInfoW takes each as an action code; passing the
#: value it already holds is a no-op, so telling Windows about all of them is
#: cheaper than working out which ones changed.
_SPI_SETDOUBLECLICKTIME = 0x0020
_SPI_SETMOUSEBUTTONSWAP = 0x0021
_SPI_SETKEYBOARDDELAY = 0x0017
_SPI_SETKEYBOARDSPEED = 0x000B


def apply_personalization(record: dict[str, Any], env=None) -> list[Result]:
    """Put back the settings that make a machine feel like the old one.

    Written value by value rather than key by key, and only the values that
    were actually captured. A setting the old machine never had is not written
    as a zero here: absent and "off" are different answers, and a restore that
    turns them into each other is inventing settings nobody chose.

    Type is taken from the value itself. Windows stores these as a mix of
    numbers and strings -- ``AccentColor`` is a DWORD, ``sShortDate`` is text --
    and writing one as the other leaves Windows reading a value it will not act
    on, which looks exactly like the setting not having travelled.
    """
    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()

    from .scan.personalization import SETTINGS  # noqa: PLC0415

    by_slot = {setting.slot: setting for setting in SETTINGS}
    captured = record.get("settings")
    if not isinstance(captured, dict):
        return []

    results: list[Result] = []
    for slot, values in sorted(captured.items()):
        setting = by_slot.get(slot)
        if setting is None:
            # A bundle from a newer version knows about a setting this one does
            # not. Saying so beats writing registry values by a name we cannot
            # explain to the person whose machine it is.
            results.append(
                Result("setting", slot, Outcome.SKIPPED,
                       "this version does not know what that setting is")
            )
            continue
        if not isinstance(values, dict):
            continue
        results.append(_write_setting(env, setting, values))

    if any(result.outcome is Outcome.APPLIED for result in results):
        _tell_windows_now(env, captured)
    return results


def _write_setting(env, setting, values: dict[str, Any]) -> Result:
    r"""Write one setting's values, refusing anything the table did not ask for.

    The record comes out of a bundle, and a bundle is a file from another
    machine. Checking each name against the table again here means a record
    naming ``Control Panel\Desktop\Wallpaper``, or something from a key this
    one never listed, is dropped rather than written because the slot it
    arrived under was a real one.
    """
    written = 0
    refused: list[str] = []
    for name, value in sorted(values.items()):
        if not setting.wanted(name):
            refused.append(name)
            continue
        if not _safe_setting_value(setting, name, value):
            refused.append(name)
            continue
        try:
            if isinstance(value, bool) or isinstance(value, int):
                ok = env.write_registry_dword(setting.hive, setting.key, name, int(value))
            elif isinstance(value, str):
                ok = env.write_registry_value(setting.hive, setting.key, name, value)
            else:
                refused.append(name)
                continue
        except Exception as exc:  # noqa: BLE001
            return Result("setting", setting.title, Outcome.FAILED, str(exc))
        written += int(bool(ok))

    if not written:
        return Result("setting", setting.title, Outcome.SKIPPED,
                      "nothing in it could be written")
    detail = f"{written} value(s)"
    if refused:
        detail += f"; {len(refused)} not carried ({', '.join(sorted(refused)[:3])})"
    return Result("setting", setting.title, Outcome.APPLIED, detail)


#: The screen saver is the one personalisation value that names a program, so
#: it is the one that has to be checked rather than trusted.
_SCREENSAVER_VALUE = "SCRNSAVE.EXE"


def _safe_setting_value(setting, name: str, value: Any) -> bool:
    """Is this value safe to write as it stands?

    Almost all of them are numbers, colours and format strings, where the worst
    a wrong one does is look wrong. The exception is the screen saver, which
    names an executable that Windows will later run: a bundle that could put an
    arbitrary path there would have turned a backup into a way of starting a
    program on somebody else's machine. Only a ``.scr`` in a Windows directory
    is accepted, which is what a screen saver is.
    """
    if name != _SCREENSAVER_VALUE:
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    plain = value.strip().strip('"')
    if not plain.lower().endswith(".scr"):
        return False
    # A bare name resolves against the system directory, which is where the
    # ones that ship with Windows live. A path has to be inside Windows itself.
    lowered = plain.replace("/", "\\").lower()
    if "\\" not in lowered:
        return True
    return lowered.startswith("c:\\windows\\") or lowered.startswith("%windir%\\")


def _tell_windows_now(env, captured: dict) -> None:
    """Ask Windows to act on the settings it does not re-read by itself.

    Best effort, and deliberately quiet: everything written above is in the
    registry and will be read at the next sign-in regardless. This is the
    difference between the mouse behaving correctly now and behaving correctly
    tomorrow.
    """
    if not touches_machine(env):
        return
    mouse = captured.get("mouse") or {}
    keyboard = captured.get("keyboard_speed") or {}
    actions = [
        (_SPI_SETDOUBLECLICKTIME, mouse.get("DoubleClickSpeed")),
        (_SPI_SETMOUSEBUTTONSWAP, mouse.get("SwapMouseButtons")),
        (_SPI_SETKEYBOARDDELAY, keyboard.get("KeyboardDelay")),
        (_SPI_SETKEYBOARDSPEED, keyboard.get("KeyboardSpeed")),
    ]
    try:
        import ctypes  # noqa: PLC0415

        for action, raw in actions:
            if raw is None:
                continue
            try:
                number = int(str(raw))
            except (TypeError, ValueError):
                continue
            ctypes.windll.user32.SystemParametersInfoW(action, number, None, 0)
    except Exception as exc:  # noqa: BLE001 -- a setting is never worth an exception
        log.debug("could not tell Windows about the new settings: %s", exc)


# --- the desktop background ------------------------------------------------
#: Windows keeps these in three places; see :mod:`winmigrate.scan.wallpaper`.
DESKTOP_KEY = r"Control Panel\Desktop"
COLORS_KEY = r"Control Panel\Colors"
WALLPAPERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers"

#: ``BackgroundType``, the value that decides what kind of background is on.
BACKGROUND_TYPE_NUMBERS = {"picture": 0, "solid_colour": 1, "slideshow": 2, "spotlight": 3}

#: SystemParametersInfoW, which is what actually makes the desktop change.
_SPI_SETDESKWALLPAPER = 0x0014
_SPIF_UPDATEINIFILE = 0x01
_SPIF_SENDCHANGE = 0x02


def apply_wallpaper(record: dict[str, Any], image: Path | None, env=None) -> list[Result]:
    """Put the desktop background back: the picture, or the thing instead of one.

    Two cases, and the difference between them is the whole point. A background
    someone *chose* is a file, and it is restored as that file and pointed at.
    Windows Spotlight is not a picture at all -- it is a setting that fetches a
    new photograph every day -- so it is restored as the setting. Copying the
    photograph Spotlight happened to be showing would replace a background that
    changes daily with one frozen image, which is the same mistake as restoring
    a shortcut by copying whatever it pointed at.

    The type is written first and the picture second: Windows reads them in
    that order, and a machine told "picture" with nothing to show is a black
    desktop.
    """
    if env is None:  # pragma: no cover -- the live path
        from .platform_win import Environment  # noqa: PLC0415

        env = Environment.live()

    kind = str(record.get("type") or "")
    if kind not in BACKGROUND_TYPE_NUMBERS:
        return [Result("background", kind or "unknown", Outcome.SKIPPED,
                       "the backup does not say what kind of background this was")]

    results: list[Result] = []
    _write_background_type(env, kind, results)

    if kind == "spotlight":
        results.append(
            Result("background", "Windows Spotlight", Outcome.APPLIED,
                   "a new picture every day, as before; it appears at the next sign-in "
                   "if not straight away")
        )
        return results
    if kind == "solid_colour":
        results.extend(_apply_background_colour(env, record))
        return results
    if kind == "slideshow":
        results.append(
            Result("background", "slideshow", Outcome.SKIPPED,
                   "a slideshow points at a folder of pictures; set it again in "
                   "Settings > Personalisation once they are back")
        )
        return results

    if image is None or not image.is_file():
        results.append(
            Result("background", str(record.get("file_name") or "picture"), Outcome.SKIPPED,
                   "the picture is not in this backup, so the background was left alone")
        )
        return results

    for name, value in (
        ("WallpaperStyle", str(record.get("style") or "")),
        ("TileWallpaper", str(record.get("tile") or "")),
    ):
        if value:
            env.write_registry_value("HKCU", DESKTOP_KEY, name, value)
    results.append(_set_desktop_picture(env, image))
    return results


def _write_background_type(env, kind: str, results: list[Result]) -> None:
    """Say which kind of background this is, which is a number, not a string."""
    try:
        env.write_registry_dword(
            "HKCU", WALLPAPERS_KEY, "BackgroundType", BACKGROUND_TYPE_NUMBERS[kind]
        )
    except Exception as exc:  # noqa: BLE001
        results.append(Result("background", "type", Outcome.FAILED, str(exc)))


def _apply_background_colour(env, record: dict[str, Any]) -> list[Result]:
    """Three numbers, space separated, the way Windows stores a desktop colour."""
    colour = str(record.get("colour") or "").strip()
    parts = colour.split()
    if len(parts) != 3 or not all(part.isdigit() and int(part) < 256 for part in parts):
        return [Result("background", "colour", Outcome.SKIPPED,
                       "the recorded colour is not three numbers")]
    written = env.write_registry_value("HKCU", COLORS_KEY, "Background", " ".join(parts))
    # The registry is where the colour lives; SetSysColors is what makes the
    # desktop change now rather than at the next sign-in, and is allowed to
    # fail without costing the setting.
    _set_system_background_colour(env, parts)
    return [
        Result("background", f"colour {colour}",
               Outcome.APPLIED if written else Outcome.FAILED)
    ]


def _set_desktop_picture(env, image: Path) -> Result:
    """Hand the picture to Windows itself.

    Writing ``Wallpaper`` into the registry sets what the desktop will be at the
    next sign-in. SystemParametersInfoW sets what it is *now*, and writes the
    registry on the way past, which is why it is the one that runs -- a
    background that appears after a reboot reads as a background that did not
    come back.
    """
    if not touches_machine(env):
        env.write_registry_value("HKCU", DESKTOP_KEY, "Wallpaper", str(image))
        return Result("background", image.name, Outcome.APPLIED, "recorded")
    try:
        import ctypes  # noqa: PLC0415

        ok = ctypes.windll.user32.SystemParametersInfoW(
            _SPI_SETDESKWALLPAPER, 0, str(image),
            _SPIF_UPDATEINIFILE | _SPIF_SENDCHANGE,
        )
    except Exception as exc:  # noqa: BLE001 -- a background is never worth an exception
        return Result("background", image.name, Outcome.FAILED, str(exc))
    if not ok:
        # It still belongs in the registry: the next sign-in reads it from
        # there, so a refusal now is a delay rather than a loss.
        env.write_registry_value("HKCU", DESKTOP_KEY, "Wallpaper", str(image))
        return Result("background", image.name, Outcome.APPLIED,
                      "Windows would not change it now; it appears at the next sign-in")
    return Result("background", image.name, Outcome.APPLIED)


def _set_system_background_colour(env, parts: list[str]) -> None:
    """Repaint the desktop colour now. Best effort, and never on a fixture."""
    if not touches_machine(env):
        return
    try:
        import ctypes  # noqa: PLC0415

        red, green, blue = (int(part) for part in parts)
        index = ctypes.c_int(1)  # COLOR_BACKGROUND
        colour = ctypes.c_ulong(red | (green << 8) | (blue << 16))
        ctypes.windll.user32.SetSysColors(1, ctypes.byref(index), ctypes.byref(colour))
    except Exception as exc:  # noqa: BLE001
        log.debug("could not repaint the desktop colour: %s", exc)


def touches_machine(env) -> bool:
    """May this call change the machine it is running on?

    Asked of the environment, never of the platform. ``Environment.fixture()``
    is a made-up machine -- its registry is a dict, its writes are recorded,
    and its ``is_windows`` is False however real the Windows underneath is. A
    function handed one must not reach past it.

    Gating on ``sys.platform`` instead is how a test run on a Windows build
    machine changed that machine's wallpaper: the registry writes went to the
    fixture, as intended, and the SystemParametersInfoW call beside them went
    to the actual desktop. The test failed because the fixture was missing a
    value the real machine had just been given.

    Two conditions, not one. A fixture's ``is_windows`` is False, which would
    be enough -- except that tests exercising Windows-only paths set it True on
    purpose, and one of those reaching this would be the same accident again
    wearing a different hat. A fixture also answers reads from a dict, so a
    real machine is the one whose registry is the actual registry.
    """
    return bool(getattr(env, "is_windows", False)) and getattr(env, "registry", 1) is None


def _merge_variable(env, name: str, incoming: str) -> list[Result]:
    """Add the old machine's PATH entries that make sense here, and say which did not.

    Directory names travel into the report and the log the way every other path
    in a restore does. Values of *other* variables never do: that is the line,
    and it is why this is a named set of one rather than a general merge.
    """
    spelling, existing = _current_variable(env, name)
    present = {entry.lower() for entry in _split_path(existing)}
    # The process PATH carries the machine half as well, so an entry already
    # provided system-wide is not worth adding to the user's own.
    present |= {entry.lower() for entry in _split_path(env.environ.get("PATH", ""))}

    added: list[str] = []
    dropped: list[str] = []
    for entry in _split_path(incoming):
        expanded = pathutil.expand(entry, env.environ)
        if entry.lower() in present or expanded.lower() in present:
            continue
        if not Path(pathutil.to_posix(expanded)).is_dir():
            dropped.append(entry)
            continue
        added.append(entry)
        present.add(entry.lower())

    results = [
        Result("env", f"{name} entry", Outcome.SKIPPED, f"{entry} is not on this machine")
        for entry in dropped
    ]
    if not added:
        results.append(
            Result("env", name, Outcome.SKIPPED,
                   "nothing from the old one was missing here" if not dropped
                   else f"none of the {len(dropped)} old entr(ies) exist here")
        )
        return results

    merged = ";".join([*_split_path(existing), *added])
    try:
        # Written under the spelling this machine already uses. Windows compares
        # value names without case, so "PATH" would land on "Path" there and
        # nowhere near it in a fixture -- and a test that cannot see the write
        # is a test that cannot see a mistake.
        written = env.write_registry_value("HKCU", "Environment", spelling, merged)
    except Exception as exc:  # noqa: BLE001
        results.append(Result("env", name, Outcome.FAILED, str(exc)))
        return results
    results.append(
        Result(
            "env", name,
            Outcome.APPLIED if written else Outcome.FAILED,
            f"kept this machine's, added {len(added)}: {', '.join(added)}",
        )
    )
    return results


def _current_variable(env, name: str) -> tuple[str, str]:
    """This machine's own spelling and value for ``name``, or the incoming one."""
    values = env.read_registry_key("HKCU", "Environment") or {}
    for key, value in values.items():
        if isinstance(key, str) and key.lower() == name.lower() and isinstance(value, str):
            return key, value
    return name, ""


def _split_path(value: str) -> list[str]:
    """PATH entries, in order, without the empties a trailing ';' leaves."""
    return [entry.strip() for entry in (value or "").split(";") if entry.strip()]


def _broadcast_environment_change() -> None:
    """Tell running programs the environment changed.

    Without this the variables are in the registry and nothing notices until the
    next sign-in, which reads as the restore not having worked.
    """
    import sys  # noqa: PLC0415

    if sys.platform != "win32":
        return
    try:
        import ctypes  # noqa: PLC0415

        HWND_BROADCAST = 0xFFFF
        WM_SETTINGCHANGE = 0x001A
        SMTO_ABORTIFHUNG = 0x0002
        result = ctypes.c_ulong()
        ctypes.windll.user32.SendMessageTimeoutW(
            HWND_BROADCAST, WM_SETTINGCHANGE, 0, "Environment",
            SMTO_ABORTIFHUNG, 5000, ctypes.byref(result),
        )
    except Exception as exc:  # noqa: BLE001 -- cosmetic; the values are written either way
        log.debug("could not broadcast the environment change: %s", exc)


def _first_line(text: str | None) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
