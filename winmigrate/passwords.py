"""The browser password handoff -- the browser exports, WinMigrate encrypts.

WinMigrate never reads a browser's password store and never touches DPAPI. The
one legitimate way to move saved passwords is the browser's *own* export, which
prompts for OS re-authentication (Windows Hello, on a machine that has it) and
writes a CSV itself. This module's job is only what happens either side of that:

* work out which installed browsers have passwords worth exporting (sync off, so
  they are not already in the cloud), and where each browser's export page is;
* open that page for the user, on request, so the browser can run its own
  authenticated export;
* take the CSV the *user* produced, sanity-check that it is a password export,
  and stage it into the bundle as encrypted-only material;
* on restore, place it in a clearly-named folder with an import-then-delete
  instruction, and shred the plaintext copy when asked.

The CSV is plaintext by nature -- that is what the browser writes and what the
import expects -- so it lives only inside the encrypted bundle, is written to a
named folder on restore rather than scattered, and the report tells the user to
delete it once imported.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
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
from .platform_win import HKCU, HKLM, Environment
from .util import hashing

log = logging.getLogger(__name__)

#: Where the CSVs land on restore (under the destination). Named so a user can
#: find, import and then delete them, rather than having plaintext scattered.
PASSWORDS_ARCHIVE_DIR = "secrets/WinMigrate-Passwords"
PASSWORDS_RESTORE_DIR = "WinMigrate-Passwords"

#: The built-in password manager page each browser opens to for export/import.
EXPORT_PAGES: dict[str, str] = {
    "chrome": "chrome://password-manager/passwords",
    "edge": "edge://wallet/passwords",
    "brave": "brave://password-manager/passwords",
    "vivaldi": "chrome://password-manager/passwords",
    "chromium": "chrome://password-manager/passwords",
    "firefox": "about:logins",
}


@dataclass(slots=True)
class ExportTarget:
    """An installed browser whose passwords are local and worth exporting."""

    browser_key: str
    title: str
    engine: str
    export_page: str
    account_email: str | None = None


def export_targets(env: Environment) -> list[ExportTarget]:
    """Installed browsers whose passwords are *not* already synced to the cloud.

    A browser with sync on needs no export -- its passwords come back on
    sign-in -- so only browsers with sync off (or never configured) are offered.
    """
    from .scan import browsers as browsers_mod  # noqa: PLC0415 -- avoid a cycle

    profiles: list = []
    for browser in browsers_mod.CHROMIUM_BROWSERS:
        profiles.extend(browsers_mod.detect_chromium(env, browser))
    profiles.extend(browsers_mod.detect_firefox(env))

    targets: list[ExportTarget] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile.browser_key in seen:
            continue
        seen.add(profile.browser_key)
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        if any(p.state.sync_on for p in same):
            continue  # synced -- no local export needed
        account = next((p.state.account_email for p in same if p.state.account_email), None)
        targets.append(
            ExportTarget(
                browser_key=profile.browser_key,
                title=profile.browser_title,
                engine=profile.engine,
                export_page=EXPORT_PAGES.get(profile.browser_key, ""),
                account_email=account,
            )
        )
    return targets


#: Column names a browser password export is expected to contain. Chromium
#: writes ``name,url,username,password,note``; Firefox ``url,username,password,...``.
REQUIRED_CSV_COLUMNS = ("url", "username", "password")


def looks_like_password_csv(text: str) -> bool:
    """True when ``text`` is a browser password export.

    A guard against a user pointing WinMigrate at the wrong file: the header row
    must carry url, username and password columns. Only the header is inspected,
    never the rows -- the point is to avoid capturing something that is *not* a
    password CSV, not to read the passwords.
    """
    header = (text or "").lstrip("﻿").splitlines()[:1]
    if not header:
        return False
    columns = {cell.strip().strip('"').lower() for cell in header[0].split(",")}
    return all(column in columns for column in REQUIRED_CSV_COLUMNS)


def build_password_item(target: ExportTarget, csv_path: Path) -> Item:
    """Stage a user-produced CSV as an encrypted-only password item.

    The CSV is hashed for the manifest but never parsed. It is SECRET, so it
    lives only in the encrypted payload and appears in the sidecar as a redacted
    stub.
    """
    archive = f"{PASSWORDS_ARCHIVE_DIR}/{target.browser_key}-passwords.csv"
    item = Item(
        id=f"browser:passwords-csv:{target.browser_key}",
        category=Category.BROWSER_PASSWORDS,
        kind=Kind.FILE,
        title=f"{target.title} — exported passwords (CSV)",
        source_path=str(csv_path),
        archive_path=archive,
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target=f"%USERPROFILE%\\{PASSWORDS_RESTORE_DIR}\\{target.browser_key}-passwords.csv",
            strategy=RestoreStrategy.REPLACE,
            notes=["Import into the browser, then delete this file -- it is plaintext."],
        ),
    )
    item.notes.append(
        Note(
            Severity.WARNING,
            "Plaintext password export, encrypted only inside the bundle. Delete it "
            "after importing on the new machine.",
        )
    )
    return item


def import_followup(target: ExportTarget) -> Followup:
    """The restore-side instruction for a captured password CSV."""
    return Followup(
        id=f"browser:passwords:{target.browser_key}",
        title=f"Import your {target.title} passwords, then delete the file",
        why=(
            f"You exported {target.title}'s passwords into the bundle. They restore as "
            f"a plaintext CSV in {PASSWORDS_RESTORE_DIR}\\, which you import and then delete."
        ),
        steps=[
            f"Open {target.title} and go to {target.export_page or 'the password manager'}.",
            f"Settings -> Passwords -> Import, and choose "
            f"{PASSWORDS_RESTORE_DIR}\\{target.browser_key}-passwords.csv.",
            f"Delete {PASSWORDS_RESTORE_DIR}\\{target.browser_key}-passwords.csv afterwards -- "
            "it is plaintext.",
        ],
        category=Category.BROWSER_PASSWORDS,
    )


def hash_csv(csv_path: Path) -> str:
    """SHA-256 of a CSV, so the manifest records it like any other file."""
    return hashing.hash_file(csv_path)


def shred(path: Path) -> bool:
    """Best-effort secure delete of a plaintext CSV.

    Overwrites the file's bytes before unlinking, then removes it. Best-effort
    by nature -- on an SSD or a copy-on-write filesystem the overwrite may not
    hit the original blocks -- so the real protection is that the CSV never had
    to exist for long. Returns True if the file is gone afterwards.
    """
    from .util import paths as pathutil  # noqa: PLC0415

    try:
        size = os.path.getsize(pathutil.extended(path))
        with open(pathutil.extended(path), "r+b") as handle:
            handle.write(b"\x00" * size)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError:
        pass
    try:
        os.remove(pathutil.extended(path))
    except OSError:
        return False
    return True


#: The executable to launch for each browser, as registered under
#: ``App Paths``. Per-user installs (Chrome's default) register under HKCU and
#: machine-wide ones under HKLM, so both hives are consulted.
BROWSER_EXECUTABLES: dict[str, str] = {
    "chrome": "chrome.exe",
    "edge": "msedge.exe",
    "brave": "brave.exe",
    "vivaldi": "vivaldi.exe",
    "chromium": "chrome.exe",
    "firefox": "firefox.exe",
}

APP_PATHS_KEY = r"Software\Microsoft\Windows\CurrentVersion\App Paths"


def browser_executable(target: ExportTarget, env: Environment) -> Path | None:
    """Where ``target``'s browser is installed, or None if it cannot be found.

    Read from the ``App Paths`` registry key, which is what Windows itself uses
    to resolve a bare ``chrome.exe``. Per-user installs land in HKCU and
    machine-wide ones in HKLM; Chrome defaults to per-user, so both are tried.
    """
    executable = BROWSER_EXECUTABLES.get(target.browser_key)
    if not executable:
        return None
    for hive in (HKCU, HKLM):
        raw = env.read_registry_value(hive, f"{APP_PATHS_KEY}\\{executable}", "")
        if isinstance(raw, str) and raw.strip():
            candidate = Path(raw.strip().strip('"'))
            if candidate.is_file():
                return candidate
    return None


def open_export_page(target: ExportTarget, env: Environment | None = None) -> bool:
    """Open the browser at its own password page. True when it was launched.

    The page is an *internal* browser URL -- ``brave://password-manager``,
    ``edge://wallet``, ``about:logins``. Those schemes are not registered with
    Windows: they mean something only inside the browser that defines them.
    Handing one to the shell (``start "" brave://...``) therefore does not open
    Brave; it makes Windows hunt for an app that handles a "brave" protocol,
    find none, and offer the Microsoft Store. That went for every browser here,
    not just the one that happened to be tried first.

    So the browser's own executable is launched with the URL as an argument,
    which is the only thing that can resolve it. When the executable cannot be
    found this returns False rather than opening anything, and the caller tells
    the user the address to paste instead -- a wrong dialog is worse than none.
    """
    import subprocess  # noqa: PLC0415
    import sys  # noqa: PLC0415

    if sys.platform != "win32" or not target.export_page:
        return False
    executable = browser_executable(target, env or Environment.live())
    if executable is None:
        log.info("no executable found for %s; not opening its password page", target.browser_key)
        return False
    try:
        # No shell, no "start": the browser resolves its own scheme, and the URL
        # never passes through a command interpreter that could reinterpret it.
        subprocess.Popen(  # noqa: S603 -- fixed executable from the registry
            [str(executable), target.export_page],
            close_fds=True,
        )
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("could not launch %s: %s", executable, exc)
        return False
