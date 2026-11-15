"""Which programs came from the company: installed by Intune, via Company Portal.

Company Portal is the shop window; Intune does the installing, and it keeps
its own books on the machine. They matter to a migration because a company
app is not something to fetch from a public catalogue: it may be the company's
own build, with its own settings and licence, and on a managed machine it
either installs itself or is one click away in Company Portal. Reinstalling it
from winget instead gets the public version, and Intune then finds something
it did not put there.

Three places are read, and nothing else:

* ``HKLM\\SOFTWARE\\Microsoft\\EnterpriseDesktopAppManagement`` -- MSI packages
  pushed through MDM, keyed by product code, with a ``Status`` whose values
  Microsoft documents (70 is "Enforcement Completed"). The product code is the
  same one Windows files the program under, so this match is exact.
* ``HKLM\\SOFTWARE\\Microsoft\\IntuneManagementExtension\\Win32Apps`` -- the
  Intune agent's record of each packaged ("Win32") app, keyed by Intune's own
  id for the app. This layout is not documented by Microsoft; it is what the
  agent writes, and it is read defensively -- anything unexpected is skipped.
* The agent's logs, for the one thing the registry lacks: the app's *name*.
  Only the id and the name are taken from a line. The rest of it -- which
  includes the install command, and an install command can carry a licence
  key -- is never stored, logged or returned.

Nothing here is a secret: app ids, app names, and whether the machine is
managed. No account name is read.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..platform_win import HKLM, Environment

log = logging.getLogger(__name__)

MSI_KEY = r"SOFTWARE\Microsoft\EnterpriseDesktopAppManagement"
WIN32_KEY = r"SOFTWARE\Microsoft\IntuneManagementExtension\Win32Apps"
ENROLLMENTS_KEY = r"SOFTWARE\Microsoft\Enrollments"

#: ``ProviderID`` of an Intune enrolment. Other MDM products write their own.
INTUNE_PROVIDER = "MS DM Server"

#: EnterpriseDesktopAppManagement ``Status``: "70-Enforcement Completed".
MSI_INSTALLED = 70

#: Where the Intune agent logs, under %ProgramData%.
LOG_FOLDER = ("Microsoft", "IntuneManagementExtension", "Logs")
#: The two logs that carry app policies; older agents use the first only.
LOG_PREFIXES = ("intunemanagementextension", "appworkload")
#: No log is read past this. They rotate at a few megabytes.
MAX_LOG_BYTES = 32 * 1024 * 1024

#: The web Company Portal's page for one app. Documented by Microsoft: "/apps/
#: [appID] -- opens the Details page of the corresponding app".
PORTAL_APP_URL = "https://portal.manage.microsoft.com/apps/{app_id}"
PORTAL_URL = "https://portal.manage.microsoft.com/apps"

_GUID = r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}"
_GUID_ONLY = re.compile(rf"^{_GUID}$")
_APP_KEY = re.compile(rf"^({_GUID})(?:_\d+)?$")
_PRODUCT_CODE = re.compile(rf"^\{{{_GUID}\}}$")
#: An app's id and name as the agent writes them in a policy. Deliberately
#: this narrow: two fields, side by side, and nothing after them.
_ID_AND_NAME = re.compile(rf'"Id"\s*:\s*"({_GUID})"\s*,\s*"Name"\s*:\s*"((?:[^"\\]|\\.)*)"')


@dataclass(slots=True)
class CompanyApp:
    """One program Intune put on this machine."""

    kind: str                       # "win32" (packaged app) or "msi"
    app_id: str = ""                # Intune's id for the app; win32 only
    product_code: str = ""          # msi only
    name: str = ""                  # from the agent's log, or the matched program
    #: True when Intune's own record says it is installed, False when it says
    #: it is not, None when the record could not be read either way.
    installed: bool | None = None
    #: The installed program this was matched to, by its name in Windows.
    matched: str = ""

    @property
    def link(self) -> str:
        """Its page in the web Company Portal, when Intune's id is known."""
        return PORTAL_APP_URL.format(app_id=self.app_id.lower()) if self.app_id else ""

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind, "name": self.name or self.matched}
        if self.app_id:
            data["id"] = self.app_id.lower()
        if self.product_code:
            data["product_code"] = self.product_code
        if self.matched:
            data["matched"] = self.matched
        if self.installed is not None:
            data["installed"] = self.installed
        return data


@dataclass(slots=True)
class CompanyPortal:
    """What Intune's records say about this machine."""

    enrolled: bool = False
    apps: list[CompanyApp] = field(default_factory=list)
    #: How many of the packaged apps had no name in any log.
    unnamed: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "enrolled": self.enrolled,
            "apps": [app.to_json() for app in self.apps],
        }


def is_enrolled(env: Environment) -> bool:
    """Is this machine managed by Intune?"""
    for enrolment in env.registry_subkeys(HKLM, ENROLLMENTS_KEY):
        provider = env.read_registry_value(
            HKLM, f"{ENROLLMENTS_KEY}\\{enrolment}", "ProviderID"
        )
        if isinstance(provider, str) and provider.strip() == INTUNE_PROVIDER:
            return True
    return False


def read_msi_apps(env: Environment) -> list[CompanyApp]:
    """MSI packages pushed through MDM, whose install completed."""
    found: dict[str, CompanyApp] = {}
    for owner in env.registry_subkeys(HKLM, MSI_KEY):
        # One subkey per context: a user's SID, or a placeholder SID for the
        # device. Anything without an MSI key under it is not one of those.
        for code in env.registry_subkeys(HKLM, f"{MSI_KEY}\\{owner}\\MSI"):
            if not _PRODUCT_CODE.match(code):
                continue
            values = env.read_registry_key(HKLM, f"{MSI_KEY}\\{owner}\\MSI\\{code}") or {}
            if _as_int(values.get("Status")) != MSI_INSTALLED:
                continue
            found.setdefault(
                code.upper(), CompanyApp(kind="msi", product_code=code.upper(), installed=True)
            )
    return list(found.values())


def read_win32_apps(env: Environment) -> list[CompanyApp]:
    """The Intune agent's packaged apps, with whether it says each is installed."""
    found: dict[str, CompanyApp] = {}
    for owner in env.registry_subkeys(HKLM, WIN32_KEY):
        # One subkey per user (their directory id) and one of zeros for the
        # device. The agent keeps other things beside them; those are not ids.
        if not _GUID_ONLY.match(owner):
            continue
        for name in env.registry_subkeys(HKLM, f"{WIN32_KEY}\\{owner}"):
            match = _APP_KEY.match(name)
            if match is None:
                continue
            app_id = match.group(1).lower()
            installed = _win32_installed(env, f"{WIN32_KEY}\\{owner}\\{name}")
            existing = found.get(app_id)
            if existing is None:
                found[app_id] = CompanyApp(kind="win32", app_id=app_id, installed=installed)
            else:
                # The same app can be recorded for the device and for a user,
                # and at more than one revision. Installed anywhere is installed.
                existing.installed = _either(existing.installed, installed)
    return list(found.values())


def _either(first: bool | None, second: bool | None) -> bool | None:
    """Two records of one app: yes if either says yes, unknown if either is."""
    if first is True or second is True:
        return True
    if first is None or second is None:
        return None
    return False


def _win32_installed(env: Environment, key: str) -> bool | None:
    """What the agent's two state messages say, or None if neither can be read.

    ``ComplianceStateMessage`` carries ``ComplianceState`` (1 is installed, 2
    is not) and ``EnforcementStateMessage`` carries ``EnforcementState`` (the
    1000s are success). Both are JSON in a string value named after the key.
    A shape this does not recognise is "unknown", never a guess.
    """
    compliance = _state(env, f"{key}\\ComplianceStateMessage", "ComplianceStateMessage",
                        "ComplianceState")
    enforcement = _state(env, f"{key}\\EnforcementStateMessage", "EnforcementStateMessage",
                         "EnforcementState")
    if compliance == 1 or (enforcement is not None and 1000 <= enforcement < 2000):
        return True
    if compliance == 2:
        return False
    return None


def _state(env: Environment, key: str, value: str, field_name: str) -> int | None:
    raw = env.read_registry_value(HKLM, key, value)
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    return _as_int(data.get(field_name)) if isinstance(data, dict) else None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def log_folder(env: Environment) -> Path:
    base = env.env_var("ProgramData") or "C:\\ProgramData"
    return env.resolve_path(base).joinpath(*LOG_FOLDER)


def read_app_names(folder: Path) -> dict[str, str]:
    """Intune app id -> the name the company gave it, from the agent's logs.

    A policy line holds the whole policy, install command included. Two fields
    are lifted out of it and the line is dropped: nothing else from a log is
    kept, returned or written to this tool's own log.
    """
    names: dict[str, str] = {}
    try:
        logs = sorted(
            (path for path in folder.iterdir()
             if path.is_file() and path.suffix.lower() == ".log"
             and path.name.lower().startswith(LOG_PREFIXES)),
            # Oldest first, so the newest name for an app is the one kept.
            key=lambda path: path.stat().st_mtime,
        )
    except OSError:
        return names
    for path in logs:
        try:
            if path.stat().st_size > MAX_LOG_BYTES:
                continue
            with open(path, encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if '"Id"' not in line or '"Name"' not in line:
                        continue
                    for app_id, raw in _ID_AND_NAME.findall(line):
                        name = _unescape(raw)
                        if name:
                            names[app_id.lower()] = name
        except OSError:
            continue
    return names


def _unescape(raw: str) -> str:
    """A JSON string's contents, as text. Control characters do not survive."""
    try:
        text = json.loads(f'"{raw}"')
    except ValueError:
        text = raw
    return " ".join(str(text).split())[:200]


def scan(env: Environment) -> CompanyPortal:
    """Everything Intune's records say, with names where a log has them."""
    portal = CompanyPortal(enrolled=is_enrolled(env))
    win32 = [app for app in read_win32_apps(env) if app.installed is not False]
    msi = read_msi_apps(env)
    if not win32 and not msi:
        return portal
    names = read_app_names(log_folder(env)) if win32 else {}
    for app in win32:
        app.name = names.get(app.app_id, "")
    # Neither a name nor a clear "installed": an id the agent once heard of,
    # which would be a line on the list nobody could act on.
    win32 = [app for app in win32 if app.name or app.installed is True]
    portal.unnamed = sum(1 for app in win32 if not app.name)
    portal.apps = [*win32, *msi]
    log.info(
        "company apps: %d packaged (%d without a name in the logs), %d MSI; "
        "this machine is %s by Intune",
        len(win32), portal.unnamed, len(msi), "managed" if portal.enrolled else "not managed",
    )
    return portal


# --- matching to what Windows lists as installed -----------------------------
_VERSION = re.compile(r"\b v?\d+(?:[._]\d+)+\w* \b | \b \d{4} \b", re.VERBOSE)
_NOISE = re.compile(
    r"\b(x64|x86|amd64|arm64|64[- ]?bit|32[- ]?bit|win32|win64|msi|exe|"
    r"latest|current|setup|installer|required|available)\b",
    re.IGNORECASE,
)


def words(name: str) -> list[str]:
    """A name reduced to the words that say *which* program it is.

    Company names for apps are whatever an administrator typed: "7-Zip 23.01
    (x64)", "Adobe Acrobat Reader DC - Required". Versions, architectures and
    the administrator's own labels are not part of what the program is.
    """
    text = re.sub(r"\([^)]*\)|\[[^\]]*\]", " ", name)
    text = _VERSION.sub(" ", text)
    text = _NOISE.sub(" ", text)
    return re.findall(r"[a-z0-9+#]+", text.lower())


def same_program(company_name: str, installed_name: str) -> bool:
    """Is the company's app this installed program?

    Deliberately strict, because a wrong yes takes a program off the list of
    things to reinstall. The words must be the same, or one name must be the
    other with more on the end -- "Adobe Acrobat Reader" and "Adobe Acrobat
    Reader DC - Deutsch" -- and then only with at least two words in common,
    so that "Teams" does not claim "Teams Machine-Wide Installer"'s neighbours.
    """
    first, second = words(company_name), words(installed_name)
    if not first or not second:
        return False
    if first == second:
        return True
    short, long = sorted((first, second), key=len)
    return len(short) >= 2 and long[: len(short)] == short


def match(portal: CompanyPortal, entries: list) -> int:
    """Tie each company app to the installed program it is, where that is clear.

    ``entries`` are :class:`~winmigrate.scan.software.SoftwareEntry`. An MSI
    is matched by product code, exactly. A packaged app is matched by name,
    and only when exactly one installed program fits: two candidates is a
    guess, and a guess is not written down.
    """
    matched = 0
    by_code = {entry.product_code.upper(): entry for entry in entries if entry.product_code}
    for app in portal.apps:
        same: list = []
        if app.kind == "msi":
            entry = by_code.get(app.product_code.upper())
            same = [entry] if entry is not None else []
        elif app.name:
            candidates = [e for e in entries if same_program(app.name, e.name)]
            # One program can be listed twice -- 32 and 64 bit, or per user and
            # per machine -- under one name. Two *names* is two programs.
            if len({e.name for e in candidates}) == 1:
                same = candidates
        if not same:
            continue
        app.matched = same[0].name
        for entry in same:
            entry.company_app = app.to_json()
        matched += 1
    # An MSI nobody could name is of no use to anyone reading the list.
    portal.apps = [app for app in portal.apps if app.name or app.matched]
    return matched
