"""Browser profiles and sign-in state -- by folder copy and config reads only.

This module does two separable things, and it is important which is which:

* It copies each browser **profile folder** (bookmarks, history, extensions,
  preferences, open tabs), minus caches and minus the credential and cookie
  stores. The password and cookie databases are DPAPI-encrypted to the source
  machine, so they are dead weight on a different machine and account -- and
  carrying them is pure risk -- so they are excluded, not decrypted.

* It reads each browser's **configuration** to work out whether the user is
  signed in and whether sync is on, so the report can say how passwords come
  across. This is a read of JSON/ini files only. Nothing here opens ``Login
  Data`` or calls ``CryptUnprotectData``; that routine is out of scope by
  design, and the sanctioned paths (sync, or the browser's own export) are what
  the follow-ups point at.

Captured profiles are marked ``SECRET``: browsing history and autofill are
private even with the password store left behind, so they live in the encrypted
payload only.
"""

from __future__ import annotations

import configparser
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Sensitivity,
    Severity,
    SkipReason,
)
from ..platform_win import Environment
from ..util import paths as pathutil
from . import extensions as extensions_mod

log = logging.getLogger(__name__)

SECRETS_ARCHIVE_PREFIX = "secrets"


@dataclass(frozen=True, slots=True)
class ChromiumBrowser:
    """A Chromium-family browser and where its ``User Data`` lives."""

    key: str
    title: str
    relative: str  # under %LOCALAPPDATA%, POSIX form


#: The Chromium-family browsers WinMigrate knows, and their User Data location
#: relative to %LOCALAPPDATA%. Opera's layout differs and is handled separately.
CHROMIUM_BROWSERS: tuple[ChromiumBrowser, ...] = (
    ChromiumBrowser("chrome", "Google Chrome", "Google/Chrome/User Data"),
    ChromiumBrowser("edge", "Microsoft Edge", "Microsoft/Edge/User Data"),
    ChromiumBrowser("brave", "Brave", "BraveSoftware/Brave-Browser/User Data"),
    ChromiumBrowser("vivaldi", "Vivaldi", "Vivaldi/User Data"),
    ChromiumBrowser("chromium", "Chromium", "Chromium/User Data"),
)

FIREFOX_RELATIVE = "Mozilla/Firefox"  # under %APPDATA%


@dataclass(slots=True)
class SignInState:
    """What the config files say about sign-in and sync. Config-read only."""

    signed_in: bool = False
    sync_on: bool = False
    account_email: str | None = None

    def to_json(self) -> dict:
        return {
            "signed_in": self.signed_in,
            "sync_on": self.sync_on,
            "account_email": self.account_email,
        }


@dataclass(slots=True)
class BrowserProfile:
    """One profile of one browser."""

    browser_key: str
    browser_title: str
    engine: str            # "chromium" | "firefox"
    profile_dir: Path
    display_name: str
    state: SignInState = field(default_factory=SignInState)
    #: Read from the extensions' own manifests. Kept on the item's record, which
    #: is redacted from the plaintext sidecar along with the rest of a SECRET
    #: item -- an extension list says a lot about someone.
    extensions: list = field(default_factory=list)


# --- Chromium --------------------------------------------------------------
def _chromium_profile_dirs(user_data: Path) -> list[Path]:
    """Profile subdirectories of a Chromium ``User Data`` folder.

    A profile is a directory containing a ``Preferences`` file: ``Default`` and
    ``Profile 1``, ``Profile 2`` and so on. ``System Profile`` and ``Guest
    Profile`` are excluded -- they hold nothing the user put there.
    """
    profiles: list[Path] = []
    try:
        entries = sorted(user_data.iterdir(), key=lambda p: p.name)
    except OSError:
        return profiles
    for entry in entries:
        if entry.name in {"System Profile", "Guest Profile"}:
            continue
        if (entry / "Preferences").is_file():
            profiles.append(entry)
    return profiles


def parse_chromium_signin(preferences_text: str) -> SignInState:
    """Read sign-in and sync state from a Chromium ``Preferences`` JSON.

    Uses only ``account_info`` (a signed-in account, with its email) and the
    ``sync`` section. It never touches the password store, which is a separate,
    encrypted file this code does not open.
    """
    state = SignInState()
    try:
        prefs = json.loads(preferences_text or "{}")
    except json.JSONDecodeError:
        return state
    if not isinstance(prefs, dict):
        return state

    account_info = prefs.get("account_info")
    if isinstance(account_info, list) and account_info:
        first = account_info[0]
        if isinstance(first, dict):
            state.signed_in = True
            email = first.get("email")
            if isinstance(email, str) and email:
                state.account_email = email

    sync = prefs.get("sync")
    if isinstance(sync, dict):
        # Different Chromium versions spell this differently; any truthy
        # completion flag means the user turned sync on at some point.
        for flag in ("has_setup_completed", "requested", "initial_sync_done"):
            if sync.get(flag):
                state.sync_on = True
                break
    return state


def detect_chromium(env: Environment, browser: ChromiumBrowser) -> list[BrowserProfile]:
    user_data = env.appdata_local() / Path(browser.relative)
    if not user_data.is_dir():
        return []
    profiles: list[BrowserProfile] = []
    for profile_dir in _chromium_profile_dirs(user_data):
        state = _read_chromium_state(profile_dir)
        profiles.append(
            BrowserProfile(
                browser_key=browser.key,
                browser_title=browser.title,
                engine="chromium",
                profile_dir=profile_dir,
                display_name=_chromium_profile_name(profile_dir),
                state=state,
            )
        )
    return profiles


def _read_chromium_state(profile_dir: Path) -> SignInState:
    prefs = profile_dir / "Preferences"
    try:
        return parse_chromium_signin(prefs.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return SignInState()


def _chromium_profile_name(profile_dir: Path) -> str:
    """The friendly profile name from Preferences, falling back to the dir name."""
    try:
        prefs = json.loads((profile_dir / "Preferences").read_text(encoding="utf-8", errors="replace"))
        name = prefs.get("profile", {}).get("name")
        if isinstance(name, str) and name.strip():
            return name.strip()
    except (OSError, json.JSONDecodeError, AttributeError):
        pass
    return profile_dir.name


# --- Firefox ---------------------------------------------------------------
def _firefox_profiles(profiles_ini_text: str) -> list[tuple[str, str, bool]]:
    """Parse ``profiles.ini`` into ``(name, path, is_relative)`` tuples."""
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    try:
        parser.read_string(profiles_ini_text)
    except configparser.Error:
        return []
    out: list[tuple[str, str, bool]] = []
    for section in parser.sections():
        if not section.lower().startswith("profile"):
            continue
        path = parser.get(section, "Path", fallback="").strip()
        if not path:
            continue
        name = parser.get(section, "Name", fallback=path).strip()
        is_relative = parser.get(section, "IsRelative", fallback="1").strip() != "0"
        out.append((name, path, is_relative))
    return out


SYNC_USERNAME_PATTERN = re.compile(
    r'user_pref\(\s*"services\.sync\.username"\s*,\s*"([^"]*)"\s*\)'
)


def parse_firefox_prefs(prefs_js_text: str) -> SignInState:
    """Read Firefox sync state from ``prefs.js`` -- config only.

    ``services.sync.username`` is set to the account email once sync is
    configured; its presence is the signal. No credential store is read.
    """
    state = SignInState()
    match = SYNC_USERNAME_PATTERN.search(prefs_js_text or "")
    if match and match.group(1):
        state.signed_in = True
        state.sync_on = True
        state.account_email = match.group(1)
    return state


def detect_firefox(env: Environment) -> list[BrowserProfile]:
    root = env.appdata_roaming() / Path(FIREFOX_RELATIVE)
    ini = root / "profiles.ini"
    if not ini.is_file():
        return []
    try:
        entries = _firefox_profiles(ini.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return []
    profiles: list[BrowserProfile] = []
    for name, rel, is_relative in entries:
        profile_dir = (root / rel) if is_relative else Path(rel)
        if not profile_dir.is_dir():
            continue
        profiles.append(
            BrowserProfile(
                browser_key="firefox",
                browser_title="Mozilla Firefox",
                engine="firefox",
                profile_dir=profile_dir,
                display_name=name,
                state=_read_firefox_state(profile_dir),
            )
        )
    return profiles


def _read_firefox_state(profile_dir: Path) -> SignInState:
    state = SignInState()
    prefs = profile_dir / "prefs.js"
    if prefs.is_file():
        try:
            state = parse_firefox_prefs(prefs.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass
    # signedInUser.json is a second, independent signal of a signed-in account.
    signed = profile_dir / "signedInUser.json"
    if signed.is_file() and not state.signed_in:
        state.signed_in = True
    return state


# --- assembling the scan ---------------------------------------------------
def scan_browsers(env: Environment, files_only: bool = False):
    """Return browser-profile items and password follow-ups for this profile."""
    profiles: list[BrowserProfile] = []
    for browser in CHROMIUM_BROWSERS:
        profiles.extend(detect_chromium(env, browser))
    profiles.extend(detect_firefox(env))

    items: list[Item] = []
    followups: list[Followup] = []
    notes: list[Note] = []

    for profile in profiles:
        profile.extensions = extensions_mod.inventory(profile.profile_dir, profile.engine)
        items.append(_profile_item(profile, env, files_only))

    for followup in _password_followups(profiles):
        followups.append(followup)
    followups.extend(_relocation_followups(profiles, env))
    followups.extend(_extension_followups(profiles))

    return items, followups, notes


def _extension_followups(profiles: list[BrowserProfile]) -> list[Followup]:
    """One follow-up per browser that has extensions, naming them.

    This is the step people are most surprised by. The extension code and
    everything each extension saved are both in the bundle and both restore, but
    Chromium keeps its extension registry in ``Secure Preferences`` behind an
    HMAC tied to the machine, so a profile opened on a different computer
    generally comes up with those extensions disabled or absent. It looks like
    the migration lost them. It did not -- installing each one again picks the
    restored data straight back up, which is why the list is worth carrying:
    without it there is nothing on the new machine that says what you had.
    """
    followups: list[Followup] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile.browser_key in seen or not profile.extensions:
            continue
        seen.add(profile.browser_key)
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        by_id = {ext.ext_id: ext for p in same for ext in p.extensions}
        listed = sorted(by_id.values(), key=lambda ext: ext.name.lower())
        with_data = [ext for ext in listed if ext.has_data]
        store = (
            "addons.mozilla.org" if profile.engine == "firefox" else "the Chrome Web Store"
        )
        followups.append(
            Followup(
                id=f"browser:extensions:{profile.browser_key}",
                title=f"{profile.browser_title}: reinstall {len(listed)} extension(s)",
                why=(
                    f"{len(listed)} extension(s) travelled with the profile, "
                    f"{len(with_data)} of them carrying saved settings. "
                    f"{profile.browser_title} ties its extension registry to the "
                    "machine, so they usually need installing again on the new "
                    "computer -- their data is already restored and comes back with "
                    "them, so this is a reinstall, not a reconfigure."
                ),
                steps=[
                    f"Open {profile.browser_title} and check which extensions are "
                    "already active -- if sync was on, some will have returned by "
                    "themselves.",
                    f"Install the rest from {store}:",
                    *[
                        f"    {ext.name}" + (f" ({ext.version})" if ext.version else "")
                        + ("  -- has saved data" if ext.has_data else "")
                        for ext in listed
                    ],
                    "Open each one's options page and confirm your settings are "
                    "there before changing anything.",
                    "Anything installed from outside the store (a developer or "
                    "sideloaded extension) has no store page: its files are under "
                    "the profile's Extensions folder, to load unpacked if you need it.",
                ],
                category=Category.BROWSER_PROFILE,
            )
        )
    return followups


def _relocation_followups(profiles: list[BrowserProfile], env: Environment) -> list[Followup]:
    """One follow-up per profile that could not be stored where it lives.

    Restoring the data is only half the job for these: the browser looks for the
    profile at the path it was configured with, so unless the user puts it back
    (or repoints the browser), the restored copy sits there unused.
    """
    followups: list[Followup] = []
    for profile in profiles:
        relative, relocated = _placement(profile, env)
        if not relocated:
            continue
        landing = "%USERPROFILE%\\" + relative.replace("/", "\\")
        followups.append(
            Followup(
                id=f"browser:relocated:{profile.browser_key}:{_slug(profile.profile_dir.name)}",
                title=f"{profile.browser_title}: put {profile.display_name} back where it lives",
                why=(
                    f"This profile was at {profile.profile_dir}, outside your user folder, so "
                    f"the bundle could not record that location. It restores to {landing}; "
                    f"{profile.browser_title} will not find it there."
                ),
                steps=[
                    f"Close {profile.browser_title}.",
                    f"Move the restored folder from {landing} to {profile.profile_dir} "
                    "(create the parent folders, or the drive, if the new machine lacks them).",
                    f"If you would rather keep it where it landed, point "
                    f"{profile.browser_title} at the new path instead: Firefox uses "
                    "profiles.ini in %APPDATA%\\Mozilla\\Firefox; Chromium browsers use "
                    "the --user-data-dir switch on the shortcut.",
                    f"Start {profile.browser_title} and confirm your bookmarks are there.",
                ],
                category=Category.BROWSER_PROFILE,
            )
        )
    return followups


#: Where a profile that lives outside the user profile is put on restore. Its
#: real home is on another drive, which the bundle cannot address, so it is
#: parked here in the open and the user is told to move it back.
RELOCATED_DIR = "WinMigrate-Relocated"


def _placement(profile: BrowserProfile, env: Environment) -> tuple[str, bool]:
    """Return the profile-relative path to store the profile at, and whether it
    had to be relocated to get there.

    Firefox's ``profiles.ini`` may point anywhere -- ``D:\\FFProfiles\\work`` is a
    perfectly ordinary setup for someone who keeps their profile off the system
    drive. Archive names under ``secrets/`` are profile-relative by convention,
    so such a profile cannot be expressed in it. It used to be run through the
    drive-stripping fallback, which produced ``secrets/FFProfiles/work`` and
    quietly restored the profile to ``%USERPROFILE%\\FFProfiles\\work`` -- a
    path that means nothing to anyone, with no indication it had moved.
    """
    relative = pathutil.relative_within(profile.profile_dir, env.profile_root)
    if relative is not None and relative != ".":
        return relative, False
    return f"{RELOCATED_DIR}/{profile.browser_key}/{profile.profile_dir.name}", True


def _profile_item(profile: BrowserProfile, env: Environment, files_only: bool) -> Item:
    relative, relocated = _placement(profile, env)
    item = Item(
        # Keyed on the profile *directory* (Default, Profile 1, or Firefox's
        # random-suffixed dir), which is unique within a browser. The friendly
        # name is not: Chromium names every new profile "Person 1" by default,
        # so two profiles would otherwise collide on one id and the manifest
        # would fail validation on restore.
        id=f"browser:{profile.browser_key}:{_slug(profile.profile_dir.name)}",
        category=Category.BROWSER_PROFILE,
        kind=Kind.TREE,
        title=f"{profile.browser_title} — {profile.display_name}",
        source_path=str(profile.profile_dir),
        archive_path=f"{SECRETS_ARCHIVE_PREFIX}/{relative}",
        sensitivity=Sensitivity.SECRET,
        restore=RestoreSpec(
            target="%USERPROFILE%\\" + relative.replace("/", "\\"),
            strategy=RestoreStrategy.MERGE,
            notes=["Close the browser before restoring so files are not overwritten under it."],
        ),
        record={
            "sign_in": profile.state.to_json(),
            "engine": profile.engine,
            "extensions": [ext.to_json() for ext in profile.extensions],
        },
    )
    item.notes.append(
        Note(
            Severity.INFO,
            "Bookmarks, history, extensions and settings; caches and the password "
            "and cookie stores are left out (they do not transfer between machines).",
        )
    )
    item.notes.append(
        Note(
            Severity.INFO,
            "Your open tabs travel with the profile, and the saved session holds "
            "the cookies for them.",
            "So you may arrive still signed in to sites that were open. Close the "
            "tabs you would rather not carry before capturing.",
        )
    )
    if profile.extensions:
        with_data = [ext for ext in profile.extensions if ext.has_data]
        item.notes.append(
            Note(
                Severity.INFO,
                f"{len(profile.extensions)} extension(s), "
                f"{len(with_data)} with saved data (settings, rules, lists).",
                "The code and the data both travel. Chromium ties its extension "
                "registry to the machine, so expect to reinstall them on the new "
                "computer -- the saved data is already there and comes back with "
                "them.",
            )
        )
    if relocated:
        item.notes.append(
            Note(
                Severity.WARNING,
                f"This profile lives outside your user folder, at {profile.profile_dir}.",
                f"It restores to %USERPROFILE%\\{RELOCATED_DIR}\\{profile.browser_key}"
                f"\\{profile.profile_dir.name} instead; see the follow-up to put it back.",
            )
        )
    if files_only:
        from ..models import Action  # noqa: PLC0415

        item.action = Action.SKIP
        item.skip_reason = SkipReason.FILES_ONLY_MODE
    return item


def _password_followups(profiles: list[BrowserProfile]) -> list[Followup]:
    """One password follow-up per browser that has any signed-in profile,
    plus a guided-export follow-up for browsers with no sync."""
    followups: list[Followup] = []
    seen: set[str] = set()
    for profile in profiles:
        if profile.browser_key in seen:
            continue
        # Aggregate the browser's profiles to decide the strongest signal.
        same = [p for p in profiles if p.browser_key == profile.browser_key]
        seen.add(profile.browser_key)
        synced = any(p.state.sync_on for p in same)
        signed = any(p.state.signed_in for p in same)
        account = next((p.state.account_email for p in same if p.state.account_email), None)

        if synced:
            followups.append(
                Followup(
                    id=f"browser:passwords:{profile.browser_key}",
                    title=f"{profile.browser_title}: passwords sync down on sign-in",
                    why=(
                        f"{profile.browser_title} sync is on, so its saved passwords are in "
                        "the cloud, not in this bundle. Signing in on the new machine brings "
                        "them back. WinMigrate never reads the password store."
                    ),
                    steps=[
                        f"Install {profile.browser_title} and sign in"
                        + (f" as {account}" if account else "")
                        + ".",
                        "Wait for sync to finish; passwords, bookmarks and extensions reappear.",
                    ],
                    category=Category.BROWSER_PASSWORDS,
                )
            )
        else:
            followups.append(
                Followup(
                    id=f"browser:passwords:{profile.browser_key}",
                    title=f"{profile.browser_title}: export passwords yourself if you want them",
                    why=(
                        f"{profile.browser_title} sync is "
                        + ("off" if signed else "not set up")
                        + ", so its passwords are only on this machine. WinMigrate does not "
                        "read the password store; the browser's own export does, behind its "
                        "own Windows Hello prompt."
                    ),
                    steps=[
                        f"In {profile.browser_title}: Settings → Passwords → Export, and "
                        "authenticate when Windows asks.",
                        "Import the resulting CSV in the same place on the new machine.",
                        "Delete the CSV afterwards -- it is plaintext.",
                    ],
                    category=Category.BROWSER_PASSWORDS,
                )
            )
    return followups


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "profile"
