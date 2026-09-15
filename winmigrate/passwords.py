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

#: What a browser will actually accept from *us*, which is rarely the page
#: above.
#:
#: A browser does not navigate to its own internal pages on the say-so of
#: another program. Chromium filters the addresses it is handed at startup down
#: to web-safe schemes, ``file:`` -- and, of its own pages, the settings root
#: and nothing below it; "exposing other settings pages is a security risk" is
#: how its source puts it. So ``brave.exe brave://password-manager/settings``
#: starts Brave, drops the address on the floor and shows the new tab page.
#: Which is exactly what it looked like: it opens the browser, but it does not
#: go to the link.
#:
#: That filter is right, and not something to defeat. A program that could
#: steer somebody's browser into its password settings from outside is the
#: beginning of an attack, not a feature -- and this tool asking Windows for
#: the right to do it would be the same tool that promises it never touches the
#: password store. So the browser is sent to the one page it will accept, its
#: settings root, which is one click from Passwords; the real address goes on
#: the clipboard for a single paste; and the screen says which of the two
#: happened rather than claiming a page that is not there.
#:
#: Firefox has no such filter and goes straight to ``about:logins``.
LANDING_PAGES: dict[str, str] = {
    "chrome": "chrome://settings/",
    "edge": "edge://settings/",
    "brave": "brave://settings/",
    "vivaldi": "vivaldi://settings/",
    "chromium": "chrome://settings/",
    "firefox": "about:logins",
}


@dataclass(slots=True)
class ExportTarget:
    """One browser *profile* whose passwords are local and worth exporting.

    A profile, not a browser. People keep work and personal profiles in one
    Brave, and Chromium's password store, its export dialog and its sign-in
    state are all per profile -- so offering one export per browser leaves
    every profile but one behind, silently, which is exactly what it did.
    """

    browser_key: str
    title: str
    engine: str
    export_page: str
    account_email: str | None = None
    #: The profile's folder name -- "Default", "Profile 2" -- which is what
    #: Chromium's ``--profile-directory`` takes.
    profile_id: str = ""
    #: What the browser calls it: "Person 1", "Work".
    profile_name: str = ""
    #: Empty for a browser's primary profile, a slug for every other one. It is
    #: what keeps a second profile's item id and file name from landing on the
    #: first's, and it stays empty on the ordinary one-profile machine so
    #: nothing about those bundles changes.
    suffix: str = ""

    @property
    def key(self) -> str:
        """How the window and the command line name this target."""
        return f"{self.browser_key}:{self.suffix}" if self.suffix else self.browser_key

    @property
    def label(self) -> str:
        """What to call it on screen, with the profile only when there is one."""
        if self.profile_name and self.profile_id:
            return f"{self.title} — {self.profile_name}"
        return self.title


@dataclass(slots=True)
class CloudAccount:
    """A browser profile whose passwords are already in the cloud.

    Nothing to export and nothing to do on this machine -- but it is worth
    saying so, because "where did my passwords go" is the question this answers
    on the new machine: sign in, and they come back.
    """

    browser_key: str
    title: str
    account_email: str | None = None
    profile_name: str = ""

    @property
    def label(self) -> str:
        return f"{self.title} — {self.profile_name}" if self.profile_name else self.title


def _browser_profiles(env: Environment) -> list:
    from .scan import browsers as browsers_mod  # noqa: PLC0415 -- avoid a cycle

    profiles: list = []
    for browser in browsers_mod.CHROMIUM_BROWSERS:
        profiles.extend(browsers_mod.detect_chromium(env, browser))
    profiles.extend(browsers_mod.detect_firefox(env))
    return profiles


def _slug(text: str) -> str:
    keep = [c if (c.isalnum() or c in "-_") else "-" for c in text.strip().lower()]
    return "-".join(part for part in "".join(keep).split("-") if part)


def _primary(same: list):
    """The profile a browser opens by default.

    "Default" is what Chromium calls it; where there is no such folder -- a
    Firefox install, or a Chromium whose Default was deleted -- the first in a
    stable order stands in, so the answer does not change between runs.
    """
    for profile in same:
        if profile.profile_dir.name == "Default":
            return profile
    return sorted(same, key=lambda profile: str(profile.profile_dir))[0]


def profile_suffix(profile, same: list) -> str:
    """Empty for the primary profile, a slug for the others.

    The primary keeps the plain name, so a one-profile machine -- nearly all of
    them -- produces exactly the ids and file names it always did, and a second
    profile gets ``brave-profile-2-passwords.csv`` beside it rather than on top
    of it.
    """
    primary = _primary(same)
    if profile.profile_dir == primary.profile_dir:
        return ""
    return _slug(profile.profile_dir.name)


def followup_id(browser_key: str, suffix: str = "") -> str:
    """The id of the password follow-up for one browser profile.

    One rule, used by the scan that writes the "export these yourself" note and
    by the ingest that replaces it with the import instruction. Two spellings of
    this would mean a bundle carrying both halves of a conversation.
    """
    return f"browser:passwords:{browser_key}:{suffix}" if suffix else f"browser:passwords:{browser_key}"


def synced_browsers(env: Environment) -> list[CloudAccount]:
    """Browser profiles whose passwords are already synced.

    The counterpart to :func:`export_targets`: between them they account for
    every profile found, which is what lets a page say something true about all
    of them rather than silently listing none.
    """
    profiles = _browser_profiles(env)
    accounts: list[CloudAccount] = []
    for profile in profiles:
        if not profile.state.sync_on:
            continue
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        accounts.append(
            CloudAccount(
                browser_key=profile.browser_key,
                title=profile.browser_title,
                account_email=profile.state.account_email,
                profile_name=profile.display_name if len(same) > 1 else "",
            )
        )
    return accounts


def export_targets(env: Environment) -> list[ExportTarget]:
    """Browser profiles whose passwords are *not* already synced to the cloud.

    Per profile, because everything about this is per profile: the password
    store, the export dialog, and whether sync is on at all. Aggregating to one
    row per browser meant a machine with a work profile and a personal one was
    offered a single export -- and a browser whose *other* profile happened to
    sync was skipped entirely, taking the local profile's passwords with it.

    A profile with sync on is still left out, on its own account rather than its
    browser's: its passwords come back on sign-in and there is nothing to do.
    """
    profiles = _browser_profiles(env)

    targets: list[ExportTarget] = []
    for profile in profiles:
        if profile.state.sync_on:
            continue  # synced -- no local export needed
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        targets.append(
            ExportTarget(
                browser_key=profile.browser_key,
                title=profile.browser_title,
                engine=profile.engine,
                export_page=EXPORT_PAGES.get(profile.browser_key, ""),
                account_email=profile.state.account_email,
                profile_id=profile.profile_dir.name,
                profile_name=profile.display_name if len(same) > 1 else "",
                suffix=profile_suffix(profile, same),
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
    stem = f"{target.browser_key}-{target.suffix}" if target.suffix else target.browser_key
    archive = f"{PASSWORDS_ARCHIVE_DIR}/{stem}-passwords.csv"
    item = Item(
        id=f"browser:passwords-csv:{target.key}",
        category=Category.BROWSER_PASSWORDS,
        kind=Kind.FILE,
        title=f"{target.label} — exported passwords (CSV)",
        source_path=str(csv_path),
        archive_path=archive,
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target=f"%USERPROFILE%\\{PASSWORDS_RESTORE_DIR}\\{stem}-passwords.csv",
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
        followup for followup in scan.followups if followup.id != followup_id(
            target.browser_key, target.suffix
        )
    ]
    scan.followups.append(import_followup(target))
    log.info("%s password export staged as encrypted-only material", target.key)
    return Ingest(True, f"{target.title} passwords added — encrypted only.", item)


def import_followup(target: ExportTarget) -> Followup:
    """The restore-side instruction for a captured password CSV."""
    stem = f"{target.browser_key}-{target.suffix}" if target.suffix else target.browser_key
    in_profile = (
        f" in the {target.profile_name} profile" if target.profile_name else ""
    )
    return Followup(
        id=followup_id(target.browser_key, target.suffix),
        title=f"Import your {target.label} passwords, then delete the file",
        why=(
            f"You exported {target.label}'s passwords into the bundle. They restore as "
            f"a plaintext CSV in {PASSWORDS_RESTORE_DIR}\\, which you import and then delete."
        ),
        steps=[
            f"Open {target.title}{in_profile} and go to "
            f"{target.export_page or 'the password manager'}.",
            f"Choose Import (the same page the export came from) and select "
            f"{PASSWORDS_RESTORE_DIR}\\{stem}-passwords.csv.",
            f"Delete {PASSWORDS_RESTORE_DIR}\\{stem}-passwords.csv afterwards -- "
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
    """Start the browser as close to its password page as it allows.

    True when it was launched -- which is not the same as it having navigated.
    Ask :func:`opens_directly` which of the two the user is about to see.

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

    Where it lands is :func:`landing_page`: for the Chromium family, the
    settings root, because Chromium refuses an internal address that came from
    another program. The caller says so and puts the real one on the clipboard.

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
    return winlaunch.launch(executable, launch_arguments(target))


def launch_arguments(target: ExportTarget) -> list[str]:
    """What to hand the browser: the page, and which profile to open it in.

    Chromium opens the profile it used last, which on a machine with a work and
    a personal profile is a coin toss -- and the export dialog only ever exports
    the profile whose window it is in. ``--profile-directory`` names the folder,
    which is the only identifier that does not change when someone renames a
    profile. Firefox picks its profile at startup and will not switch while it
    is running, so there is nothing honest to pass and the page says which
    profile to be in instead.

    The page is :func:`landing_page`, not ``export_page``: a Chromium handed
    its own password address on a command line starts up and ignores it. See
    :data:`LANDING_PAGES`.
    """
    pages = landing_pages(target)
    if target.engine == "chromium" and target.profile_id:
        return [f"--profile-directory={target.profile_id}", *pages]
    return pages


def landing_page(target: ExportTarget) -> str:
    """The address to hand the browser, which is not always the one we want.

    A Chromium fork nobody has heard of gets the same treatment as the ones
    listed, by taking its scheme from the page we would have liked to open --
    so it lands in its settings rather than on a blank tab.
    """
    known = LANDING_PAGES.get(target.browser_key)
    if known:
        return known
    if target.engine == "chromium" and "://" in target.export_page:
        return f"{target.export_page.split('://', 1)[0]}://settings/"
    return target.export_page


def landing_pages(target: ExportTarget) -> list[str]:
    """Every address worth handing the browser, because at most one is taken.

    Chromium compares what it was given against exactly one address of its own,
    and every fork rebrands that address differently: Brave answers to
    ``brave://settings/``, Edge to ``edge://settings/``, and Chromium itself to
    ``chrome://settings/``. Which one a given fork kept is not knowable from out
    here, and guessing wrong is a browser that opens on a blank tab again --
    the symptom this is fixing. Passing both costs nothing, because the one that
    does not match is discarded before it can become a tab: the user gets one
    settings window either way, never a stray search for "chrome://settings/".
    """
    primary = landing_page(target)
    alternate = "chrome://settings/"
    if target.engine != "chromium" or primary == alternate:
        return [primary]
    return [primary, alternate]


def opens_directly(target: ExportTarget) -> bool:
    """True when the browser lands on the export page itself, not merely near it.

    What the window and the command line say next hangs on this. Being told a
    page "should now be showing" when it is not is worse than being told where
    to go: it sends the user looking for a tab that was never opened.
    """
    return bool(target.export_page) and landing_page(target) == target.export_page