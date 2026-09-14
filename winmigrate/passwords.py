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
from .util import hashing, paths as pathutil

log = logging.getLogger(__name__)

#: Where the CSVs land on restore (under the destination). Named so a user can
#: find, import and then delete them, rather than having plaintext scattered.
PASSWORDS_ARCHIVE_DIR = "secrets/WinMigrate-Passwords"
PASSWORDS_RESTORE_DIR = "WinMigrate-Passwords"

#: The page each browser opens to, chosen so the user lands *on* the control
#: they need rather than one navigation away from it.
#:
#: For the Chromium family that is the password manager's **settings** page,
#: which is where both "Export passwords" and "Import passwords" live. The
#: passwords list (``.../password-manager/passwords``) shows the saved entries
#: and no export button at all -- landing there means finding Settings in the
#: sidebar first, which is exactly the fiddling this is meant to remove. The
#: same page serves the restore direction, so one address covers both.
#:
#: Firefox keeps both behind the "..." menu on ``about:logins``; there is no
#: deeper URL to aim at.
EXPORT_PAGES: dict[str, str] = {
    "chrome": "chrome://password-manager/settings",
    "edge": "edge://settings/passwords",
    "brave": "brave://password-manager/settings",
    "vivaldi": "vivaldi://settings/passwords",
    "chromium": "chrome://password-manager/settings",
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


@dataclass(slots=True)
class CloudAccount:
    """An installed browser whose passwords are already in the cloud.

    Nothing to export and nothing to do on this machine -- but it is worth
    saying so, because "where did my passwords go" is the question this answers
    on the new machine: sign in, and they come back.
    """

    browser_key: str
    title: str
    account_email: str | None = None


def _browser_profiles(env: Environment) -> list:
    from .scan import browsers as browsers_mod  # noqa: PLC0415 -- avoid a cycle

    profiles: list = []
    for browser in browsers_mod.CHROMIUM_BROWSERS:
        profiles.extend(browsers_mod.detect_chromium(env, browser))
    profiles.extend(browsers_mod.detect_firefox(env))
    return profiles


def synced_browsers(env: Environment) -> list[CloudAccount]:
    """Installed browsers whose passwords are already synced.

    The counterpart to :func:`export_targets`: between them they account for
    every browser found, which is what lets a page say something true about all
    of them rather than silently listing none.
    """
    profiles = _browser_profiles(env)
    accounts: list[CloudAccount] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile.browser_key in seen:
            continue
        seen.add(profile.browser_key)
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        if not any(p.state.sync_on for p in same):
            continue
        email = next((p.state.account_email for p in same if p.state.account_email), None)
        accounts.append(
            CloudAccount(
                browser_key=profile.browser_key,
                title=profile.browser_title,
                account_email=email,
            )
        )
    return accounts


def export_targets(env: Environment) -> list[ExportTarget]:
    """Installed browsers whose passwords are *not* already synced to the cloud.

    A browser with sync on needs no export -- its passwords come back on
    sign-in -- so only browsers with sync off (or never configured) are offered.
    """
    profiles = _browser_profiles(env)

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


@dataclass(frozen=True, slots=True)
class Ingest:
    """What happened to a CSV the user pointed at, in words they can read."""

    ok: bool
    message: str
    item: Item | None = None


def ingest_csv(target: ExportTarget, csv_path: Path, scan) -> Ingest:
    """Put a user-produced CSV into ``scan`` as encrypted-only material.

    One implementation for both front ends. The window and the command line
    disagreeing about what a password export is, or about which follow-up
    replaces which, would mean two answers to "what is in this bundle" -- and
    the untested one would be wrong.

    Only the header is ever read, and only to refuse a file that is not a
    password export. The rows are never parsed: the item is hashed and staged
    like any other file, marked SECRET, so it travels inside the encrypted
    payload and appears in the plaintext sidecar as a redacted stub.
    """
    if not csv_path.is_file():
        return Ingest(False, f"There is no file at {csv_path}.")
    try:
        head = csv_path.read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError as exc:
        return Ingest(False, f"Could not read {csv_path.name}: {exc}")
    if not looks_like_password_csv(head):
        return Ingest(
            False,
            f"{csv_path.name} does not look like a password export -- a browser's "
            "own export has url, username and password columns.",
        )
    item = build_password_item(target, csv_path)
    # Pointing at a second file for the same browser replaces the first rather
    # than capturing both: the usual reason for doing it is having picked the
    # wrong file, and two plaintext exports of one browser is the last thing
    # this should quietly produce.
    scan.items = [existing for existing in scan.items if existing.id != item.id]
    scan.items.append(item)
    # The scan-time "export these yourself" note is now answered; what is left
    # for the new machine is the import instruction.
    scan.followups = [
        followup
        for followup in scan.followups
        if followup.id != f"browser:passwords:{target.browser_key}"
    ]
    scan.followups.append(import_followup(target))
    log.info("%s password export staged as encrypted-only material", target.browser_key)
    return Ingest(True, f"{target.title} passwords added — encrypted only.", item)


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
            f"Choose Import (the same page the export came from) and select "
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

#: Where each browser installs itself when ``App Paths`` cannot answer.
#:
#: It usually can, but not always: a browser installed for another user, one
#: updated in a way that left the key stale, a machine where the key was never
#: written. The cost of missing is that the button does nothing and the user is
#: told to navigate there themselves -- which is the one job this had -- so the
#: standard locations are worth trying before giving up. Per-user installs first
#: for Chrome and Brave, which default to %LOCALAPPDATA%.
WELL_KNOWN_INSTALLS: dict[str, tuple[str, ...]] = {
    "chrome": (
        r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    ),
    "edge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
    ),
    "brave": (
        r"%LOCALAPPDATA%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles%\BraveSoftware\Brave-Browser\Application\brave.exe",
        r"%ProgramFiles(x86)%\BraveSoftware\Brave-Browser\Application\brave.exe",
    ),
    "vivaldi": (
        r"%LOCALAPPDATA%\Vivaldi\Application\vivaldi.exe",
        r"%ProgramFiles%\Vivaldi\Application\vivaldi.exe",
    ),
    "chromium": (
        r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
        r"%ProgramFiles%\Chromium\Application\chrome.exe",
    ),
    "firefox": (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    ),
}


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
    for pattern in WELL_KNOWN_INSTALLS.get(target.browser_key, ()):
        # Forward slashes: Windows accepts them everywhere, and it is what lets
        # this be exercised against a fixture tree on the machine it is written
        # on rather than only on the machine it runs on.
        candidate = Path(pathutil.to_posix(pathutil.expand(pattern, env.environ)))
        if candidate.is_file():
            log.info("%s found at its standard location, not in App Paths", target.browser_key)
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
    which is the only thing that can resolve it -- and, when WinMigrate is
    running elevated for a shadow copy, as the signed-in user rather than as
    the administrator. An elevated launch cannot hand its command line to the
    browser the user already has open, and what that looks like is a window
    coming to the front without going anywhere. See :mod:`winmigrate.winlaunch`.

    When the executable cannot be found this returns False rather than opening
    anything, and the caller tells the user the address to go to instead -- a
    wrong dialog is worse than none.
    """
    from . import winlaunch  # noqa: PLC0415

    if not winlaunch.is_windows() or not target.export_page:
        return False
    executable = browser_executable(target, env or Environment.live())
    if executable is None:
        log.info("no executable found for %s; not opening its password page", target.browser_key)
        return False
    return winlaunch.launch(executable, [target.export_page])