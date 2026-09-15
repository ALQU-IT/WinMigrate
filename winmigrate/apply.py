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

from .util import process

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
#: PATH is the dangerous one. It reads like a user setting and is really a list
#: of places software was installed on the *old* computer -- replacing the new
#: machine's copy with it breaks every tool that is not in the same place, and
#: the breakage looks nothing like a backup restore. It is reported instead, so
#: the user can copy across the entries they actually want.
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
