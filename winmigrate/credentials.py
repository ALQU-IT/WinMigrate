"""Saved Windows credentials, moved the way Windows itself moves them.

After a migration the files are back, the programs are back, and everything
asks to sign in again. A large share of that is Windows Credential Manager: the
network shares, the mapped drives, the Office and Outlook sign-ins, the "remember
me" boxes ticked years ago. None of it is in a file the backup can copy, because
every entry is encrypted with DPAPI and tied to the account and machine that
created it.

Windows has a supported way to move them, and it is the only one this tool will
use. Credential Manager can back its own store up to a ``.crd`` file: it asks
for a Ctrl+Alt+Del on the secure desktop, then for a password of the user's
choosing, and writes the file itself. WinMigrate cannot see inside that file and
does not try to -- it takes what the user hands back, puts it in the encrypted
bundle, restores it on the far side and points them at the matching Restore
button.

This is the same shape as the browser password handoff, for the same reason.
Reading the credential store directly would mean calling ``CredEnumerate`` and
``CryptUnprotectData`` to turn somebody's saved passwords into plaintext, which
is a credential dumper however politely it is described. What WinMigrate does
instead is detect what is there -- from ``cmdkey /list``, which prints names and
never secrets -- so it can tell the user what they are about to lose, and then
get out of the way of Windows' own export.

The detected list is encrypted-only. It holds no passwords, but it names the
servers and accounts somebody signs into, and that belongs in the payload
rather than in a plaintext file sitting beside the bundle.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from .models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
)
from .platform_win import Environment
from .util import hashing, process

log = logging.getLogger(__name__)

#: Where the backup rides in the bundle, and the folder it restores into.
CREDENTIALS_ARCHIVE_DIR = "secrets/WinMigrate-Credentials"
CREDENTIALS_RESTORE_DIR = "WinMigrate-Credentials"

#: What Credential Manager writes, and the only thing this accepts.
BACKUP_SUFFIX = ".crd"

#: A credential backup is small -- a few kilobytes of encrypted entries. A file
#: far larger than that is not one, and is not worth putting in a bundle.
MAX_BACKUP_BYTES = 8 * 1024 * 1024

#: The command that opens the wizard, including its Back up and Restore buttons.
#: Not a Settings page: this dialog is the only place Windows exposes them.
MANAGER_COMMAND = ("rundll32.exe", "keymgr.dll,KRShowKeyMgr")

_TARGET = re.compile(r"^\s*Target:\s*(?P<target>.+?)\s*$", re.MULTILINE)
_USER = re.compile(r"^\s*User:\s*(?P<user>.+?)\s*$", re.MULTILINE)
_TYPE = re.compile(r"^\s*Type:\s*(?P<type>.+?)\s*$", re.MULTILINE)


def parse_cmdkey(output: str) -> list[dict[str, str]]:
    """Pull the saved credential entries out of ``cmdkey /list``.

    That command prints names, types and user names. It does not print
    passwords, and there is no switch that makes it -- which is exactly why it
    is the one used here.

    Entries are separated by blank lines and each begins with a Target line.
    The English labels are matched, the same limitation as the Wi-Fi listing,
    and a machine in another display language yields an empty list rather than
    a wrong one.
    """
    entries: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", output or ""):
        target = _TARGET.search(block)
        if not target:
            continue
        user = _USER.search(block)
        kind = _TYPE.search(block)
        entries.append({
            "target": target.group("target"),
            "type": kind.group("type") if kind else "",
            "user": user.group("user") if user else "",
        })
    return entries


def list_credentials(runner=process.run) -> tuple[list[dict[str, str]], str | None]:
    """What is in Credential Manager. Windows-only; returns (entries, error)."""
    result = runner(["cmdkey", "/list"], timeout=60)
    entries = parse_cmdkey(result.stdout)
    if entries:
        return entries, None
    if not result.ok:
        return [], result.summary()
    return [], None


def scan_credentials(env: Environment, files_only: bool = False):
    """Return the credential item and the follow-up that hands off to Windows.

    ``files_only`` promises no credential material travels, and while the list
    itself holds no passwords it is a map of where somebody has accounts. Under
    that flag nothing is said at all.
    """
    if not env.is_windows or files_only:
        return [], []
    entries, error = list_credentials()
    if error:
        log.info("could not list saved credentials: %s", error)
        return [], []
    if not entries:
        return [], []

    item = Item(
        id="credentials:saved",
        category=Category.CREDENTIALS,
        kind=Kind.REPORT,
        title=f"Saved Windows sign-ins ({len(entries)})",
        record={"entries": entries},
        # No passwords in it, but it names the servers and accounts somebody
        # signs into. That belongs in the payload, not beside the bundle.
        record_public=False,
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target="Credential Manager",
            strategy=RestoreStrategy.GUIDED,
            notes=["Windows exports and imports these itself; see the follow-up."],
        ),
        notes=[
            Note(
                Severity.INFO,
                "Detected by name only. WinMigrate never reads the credential "
                "store: Windows exports it itself, behind its own prompt.",
            )
        ],
    )
    return [item], [export_followup(entries)]


def export_followup(entries: list[dict[str, str]]) -> Followup:
    """Ask Windows to do the export, and say why it has to be Windows."""
    return Followup(
        id="credentials:export",
        title=f"Let Windows back up your {len(entries)} saved sign-in(s)",
        why=(
            "These are the saved passwords for network shares, mapped drives and "
            "programs that remembered you. They are locked to this machine and "
            "account, so no backup can copy them -- but Credential Manager can "
            "export them itself, and WinMigrate will carry what it writes."
        ),
        steps=[
            "Press Windows+R, run:  rundll32.exe keymgr.dll,KRShowKeyMgr",
            "Choose 'Back up...', pick a location, and follow the prompts. Windows "
            "will ask for Ctrl+Alt+Del and then for a password of your choosing.",
            "Remember that password: it is the only thing that opens the file, and "
            "it is not the same as your WinMigrate passphrase.",
            "Hand the .crd file to WinMigrate with --credentials <file>, and it "
            "travels in the encrypted bundle.",
            f"On the new machine it restores into {CREDENTIALS_RESTORE_DIR}\\. Open "
            "the same dialog there, choose 'Restore...', and pick it.",
            "Delete the .crd afterwards -- it is a copy of every saved sign-in you "
            "have, and its only protection is that password.",
        ],
        category=Category.CREDENTIALS,
    )


def import_followup(name: str) -> Followup:
    """The restore-side instruction, once a backup is actually in the bundle."""
    return Followup(
        id="credentials:import",
        title="Put your saved sign-ins back, then delete the file",
        why=(
            "Your Credential Manager backup travelled in the encrypted bundle. "
            "Windows restores it itself, with the password you chose when you "
            "made it."
        ),
        steps=[
            "Press Windows+R, run:  rundll32.exe keymgr.dll,KRShowKeyMgr",
            f"Choose 'Restore...' and pick {CREDENTIALS_RESTORE_DIR}\\{name}.",
            "Enter the password you chose when you made the backup.",
            f"Delete {CREDENTIALS_RESTORE_DIR}\\{name} afterwards -- it is a copy "
            "of every saved sign-in you have.",
        ],
        category=Category.CREDENTIALS,
    )


def looks_like_backup(path: Path) -> tuple[bool, str]:
    """Is this the file Credential Manager writes? Returns (ok, why not).

    Checked by name and size and nothing else. The contents are Windows' own
    encrypted format and this tool does not open them -- that is the whole
    point of the handoff.
    """
    if path.suffix.lower() != BACKUP_SUFFIX:
        return False, f"that is not a {BACKUP_SUFFIX} file"
    try:
        size = path.stat().st_size
    except OSError as exc:
        return False, str(exc)
    if size == 0:
        return False, "the file is empty"
    if size > MAX_BACKUP_BYTES:
        return False, "far too large to be a credential backup"
    return True, ""


def ingest_backup(backup_path: Path, scan) -> Item:
    """Put a credential backup into the scan as encrypted-only material."""
    ok, why = looks_like_backup(backup_path)
    if not ok:
        raise ValueError(why)
    name = backup_path.name
    item = Item(
        id="credentials:backup",
        category=Category.CREDENTIALS,
        kind=Kind.FILE,
        title="Saved Windows sign-ins (Credential Manager backup)",
        source_path=str(backup_path),
        archive_path=f"{CREDENTIALS_ARCHIVE_DIR}/{name}",
        sensitivity=Sensitivity.SECRET,
        size_bytes=backup_path.stat().st_size,
        file_count=1,
        digest=hashing.hash_file(backup_path),
        digest_algo="sha256",
        restore=RestoreSpec(
            target=f"%USERPROFILE%\\{CREDENTIALS_RESTORE_DIR}\\{name}",
            strategy=RestoreStrategy.REPLACE,
            notes=["Restore it through Credential Manager's own dialog."],
        ),
        notes=[
            Note(
                Severity.WARNING,
                "A copy of every saved sign-in, protected only by the password "
                "chosen when it was made. Encrypted-only; delete it afterwards.",
            )
        ],
    )
    scan.items = [entry for entry in scan.items if entry.id != item.id]
    scan.items.append(item)
    scan.followups = [
        entry for entry in scan.followups if entry.id not in ("credentials:export",)
    ]
    scan.followups.append(import_followup(name))
    return item


def open_manager(env: Environment | None = None) -> bool:
    """Open Credential Manager's own dialog. True when it was launched.

    De-elevated when WinMigrate is running as administrator, for the same
    reason the browser is: the dialog belongs to the signed-in user, and an
    elevated one would show the administrator's credentials instead of theirs.
    """
    from . import winlaunch  # noqa: PLC0415

    env = env or Environment.live()
    # The environment, not the platform: handed a fixture, this must not open a
    # dialog on the machine the tests happen to be running on.
    if not env.is_windows or not winlaunch.is_windows():
        return False
    executable = Path(MANAGER_COMMAND[0])
    return winlaunch.launch(executable, [MANAGER_COMMAND[1]])
