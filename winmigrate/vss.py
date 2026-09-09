"""Volume Shadow Copy support, so locked and in-use files can still be captured.

Without a shadow copy, capturing a live profile means every file a running
program holds open -- browser databases, Outlook stores, anything mid-write --
either fails to open or is copied in a torn state. VSS gives a frozen,
point-in-time view of the volume that reads cleanly while the machine keeps
running.

Creating a snapshot requires administrator rights. When that is not available
(or VSS fails, as it does on some editions), capture falls back to reading files
directly and reports every file it could not open, rather than pretending the
capture was complete.

The path translation is pure and unit-tested. The snapshot lifecycle shells out
to PowerShell's WMI interface and can only be exercised on real Windows.
"""

from __future__ import annotations

import logging
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from .util import paths as pathutil

log = logging.getLogger(__name__)

GLOBALROOT_PREFIX = "\\\\?\\GLOBALROOT"

#: Creating and deleting a snapshot is not instant; do not hang a capture on it.
SNAPSHOT_TIMEOUT_SECONDS = 180


class ShadowCopyError(Exception):
    """A shadow copy could not be created. Callers fall back to direct reads."""


def is_windows() -> bool:
    return sys.platform == "win32"


def is_elevated() -> bool:
    """True when the process can create a shadow copy."""
    if not is_windows():
        return False
    try:
        import ctypes  # noqa: PLC0415

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001 -- any failure here means "assume not"
        return False


def volume_of(path: str | Path) -> str:
    """The volume root a path lives on, e.g. ``C:\\``."""
    # PureWindowsPath, not Path: this is Windows semantics by definition, and on
    # a POSIX host Path would report no drive at all.
    text = pathutil.strip_extended(str(path))
    drive = PureWindowsPath(text).drive
    if not drive:
        raise ShadowCopyError(f"cannot determine the volume for {path}")
    return drive + "\\"


def map_into_snapshot(path: str | Path, volume: str, device: str) -> str:
    """Rewrite a real path to its equivalent inside a shadow copy.

    ``C:\\Users\\alice\\f.txt`` under device
    ``\\Device\\HarddiskVolumeShadowCopy3`` becomes
    ``\\\\?\\GLOBALROOT\\Device\\HarddiskVolumeShadowCopy3\\Users\\alice\\f.txt``.
    """
    text = pathutil.strip_extended(str(path))
    volume_text = volume.rstrip("\\/") + "\\"
    if not pathutil.is_within(text, volume_text):
        raise ShadowCopyError(f"{path} is not on volume {volume}")
    relative = text[len(volume_text) :].lstrip("\\/")
    return f"{GLOBALROOT_PREFIX}{device.rstrip(chr(92))}\\{relative}"


@dataclass(slots=True)
class ShadowCopy:
    """A live shadow copy of one volume. Use as a context manager."""

    volume: str
    shadow_id: str
    device: str

    def map(self, path: str | Path) -> str:
        return map_into_snapshot(path, self.volume, self.device)

    def __enter__(self) -> "ShadowCopy":
        return self

    def __exit__(self, *exc_info) -> None:
        self.remove()

    def remove(self) -> None:
        """Delete the snapshot. Failure is logged, never raised.

        A leaked snapshot consumes disk until Windows recycles it, which is
        worth a warning but must not fail an otherwise successful capture.
        """
        try:
            _run_powershell(
                f'(Get-WmiObject Win32_ShadowCopy | Where-Object {{ $_.ID -eq "{self.shadow_id}" }}).Delete()'
            )
            log.info("shadow copy removed: %s", self.shadow_id)
        except Exception as exc:  # noqa: BLE001
            log.warning("could not remove shadow copy %s: %s", self.shadow_id, exc)


def create(volume: str) -> ShadowCopy:
    """Create a shadow copy of ``volume``. Raises :class:`ShadowCopyError`."""
    if not is_windows():
        raise ShadowCopyError("shadow copies are a Windows feature")
    if not is_elevated():
        raise ShadowCopyError(
            "creating a shadow copy needs administrator rights; "
            "run the capture from an elevated prompt to include locked files"
        )
    output = _run_powershell(
        f'$r = (Get-WmiObject -List Win32_ShadowCopy).Create("{volume}", "ClientAccessible"); '
        '$r.ReturnValue; $r.ShadowID'
    )
    shadow_id = _parse_shadow_id(output)
    if shadow_id is None:
        raise ShadowCopyError(f"shadow copy creation did not return an id: {output.strip()!r}")
    device = _device_for(shadow_id)
    log.info("shadow copy created for %s: %s", volume, device)
    return ShadowCopy(volume=volume, shadow_id=shadow_id, device=device)


def _device_for(shadow_id: str) -> str:
    output = _run_powershell(
        f'(Get-WmiObject Win32_ShadowCopy | Where-Object {{ $_.ID -eq "{shadow_id}" }}).DeviceObject'
    )
    device = output.strip().splitlines()[-1].strip() if output.strip() else ""
    if not device:
        raise ShadowCopyError(f"could not resolve the device for shadow copy {shadow_id}")
    return device


GUID_PATTERN = re.compile(r"\{[0-9A-Fa-f-]{36}\}")


def _parse_shadow_id(output: str) -> str | None:
    """Pull the ``{GUID}`` out of the Create() call's output."""
    match = GUID_PATTERN.search(output)
    return match.group(0) if match else None


def _run_powershell(script: str) -> str:
    completed = subprocess.run(  # noqa: S603 -- fixed executable, no shell
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ],
        capture_output=True,
        text=True,
        timeout=SNAPSHOT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise ShadowCopyError(
            f"powershell failed ({completed.returncode}): {completed.stderr.strip()}"
        )
    return completed.stdout
