"""Wi-Fi profiles -- opt-in, because the export contains the network passwords.

A Wi-Fi profile export (``netsh wlan export profile key=clear``) writes an XML
file per network that includes the pre-shared key in the clear. That is real
credential material, so Wi-Fi is off by default and included only with
``--include-wifi``. When included, the profiles are captured as SECRET material
-- encrypted-only, redacted from the sidecar, kept out of the log.

The profile-name listing (``netsh wlan show profiles``) and the XML export are
Windows-only shell-outs; the parsing of their output is what this module unit
tests, and the capture-time export hook is isolated for that reason.
"""

from __future__ import annotations

import logging
import re
import tempfile
from pathlib import Path

from ..models import (
    Action,
    Category,
    Followup,
    Item,
    Kind,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
    SkipReason,
)
from ..models import Note
from ..platform_win import Environment
from ..util import process

log = logging.getLogger(__name__)

PROFILE_NAME_PATTERN = re.compile(r"^\s*All User Profile\s*:\s*(.+?)\s*$", re.MULTILINE)


def parse_profile_names(netsh_output: str) -> list[str]:
    """Pull SSIDs out of ``netsh wlan show profiles`` output.

    Independent of display language would be ideal, but netsh localises the
    label; this matches the common English form and is where a localisation fix
    would go. Returns names in listed order, de-duplicated.
    """
    names: list[str] = []
    for match in PROFILE_NAME_PATTERN.finditer(netsh_output or ""):
        name = match.group(1).strip()
        if name and name not in names:
            names.append(name)
    return names


def list_profiles(runner=process.run) -> tuple[list[str], str | None]:
    """List saved Wi-Fi profile names. Windows-only; returns (names, error)."""
    result = runner(["netsh", "wlan", "show", "profiles"], timeout=60)
    if not result.ok:
        return [], result.summary()
    return parse_profile_names(result.stdout), None


def scan_wifi(env: Environment, include_wifi: bool, files_only: bool = False):
    """Return Wi-Fi items and follow-ups.

    Without ``--include-wifi`` this returns nothing at all -- not even a
    report -- so an un-opted migration makes no mention of network keys.

    ``files_only`` still reports what was found but captures none of it: a
    Wi-Fi profile carries the network password, and files-only mode promises no
    credential material travels.
    """
    if not include_wifi:
        return [], []
    if not env.is_windows:
        # The listing needs netsh; on a fixture we can only note the intent.
        return [], []
    names, error = list_profiles()
    if error:
        return [], [
            Followup(
                id="wifi:unavailable",
                title="Wi-Fi profiles could not be listed",
                why=f"netsh wlan show profiles failed: {error}",
                steps=["Run the capture from an account that can read Wi-Fi profiles."],
                category=Category.WIFI,
            )
        ]
    if not names:
        return [], []
    item = Item(
        id="wifi:profiles",
        category=Category.WIFI,
        kind=Kind.RECORD,
        title=f"Wi-Fi networks ({len(names)})",
        record={"profiles": sorted(names)},
        sensitivity=Sensitivity.SECRET,  # the captured XML holds the keys
        record_public=False,
        restore=RestoreSpec(
            target="netsh wlan add profile",
            strategy=RestoreStrategy.GUIDED,
            notes=["Restore each XML with: netsh wlan add profile filename=<file>"],
        ),
        notes=[Note(Severity.WARNING, "Includes network passwords; encrypted-only.")],
    )
    if files_only:
        item.action = Action.SKIP
        item.skip_reason = SkipReason.FILES_ONLY_MODE
    return [item], []


def export_profiles(out_dir: Path, runner=process.run) -> tuple[list[Path], str | None]:  # pragma: no cover - Windows
    """Export every Wi-Fi profile as XML (with keys) into ``out_dir``.

    ``key=clear`` writes the pre-shared keys in the clear, which is why the
    result is captured as SECRET. Windows-only; not exercised by tests.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    result = runner(
        ["netsh", "wlan", "export", "profile", "key=clear", f"folder={out_dir}"],
        timeout=120,
    )
    if not result.ok:
        return [], result.summary()
    return sorted(out_dir.glob("*.xml")), None


def temp_export_dir() -> Path:  # pragma: no cover - trivial
    return Path(tempfile.mkdtemp(prefix="winmigrate-wifi-"))
