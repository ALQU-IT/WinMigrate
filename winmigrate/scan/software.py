"""Installed-software inventory, merged from the three places Windows keeps it.

No single source is complete:

* **Registry uninstall keys** list classic desktop installers (per-machine,
  32-bit-on-64 via ``WOW6432Node``, and per-user), which is the only place most
  of them appear.
* **winget** knows which of those it can reinstall, and under what package id.
* **Appx/MSIX** covers Store and UWP apps, which appear in neither of the above.

The three are merged and de-duplicated into one record, and every entry is
marked with whether winget can reinstall it. Anything it cannot goes into a
"reinstall by hand" list rather than being quietly dropped -- a migration that
silently loses software is worse than one that admits it.

The import file written for restore is **winget's own export**, verbatim, rather
than something reconstructed here.

Which applications winget can reinstall is answered by ``winget list``, not by
guessing: it reports every installed application with either a real package id
or a synthetic ``ARP\\...`` id meaning "no package for this". That is an exact
answer, and the display names it prints are the same ones the registry holds, so
they join up without fuzzy matching. A name-similarity fallback covers only what
``winget list`` did not report at all -- and even then it decides a report label,
never the reinstall, which replays winget's export regardless.
"""

from __future__ import annotations

import json
import logging
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..platform_win import HKCU, HKLM, Environment
from ..util import process

log = logging.getLogger(__name__)

UNINSTALL_KEYS: tuple[tuple[str, str], ...] = (
    (HKLM, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
    (HKLM, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
    (HKCU, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
)

#: Entries that are Windows updates rather than applications.
UPDATE_RELEASE_TYPES = {"security update", "update rollup", "hotfix", "servicepack"}

UPDATE_NAME_PATTERN = re.compile(
    r"^(kb\d{6,}|update for |security update for |hotfix for |definition update)", re.IGNORECASE
)

#: Appx packages that ship with Windows and would only be noise in a report.
APPX_NOISE_PREFIXES = (
    "microsoft.windows.",
    "microsoft.ui.",
    "microsoft.vclibs",
    "microsoft.net.native",
    "microsoft.services.store",
    "windows.",
)


#: Runtimes, redistributables and driver packages. They are real installed
#: entries, but nobody reinstalls them deliberately -- whatever needs them
#: brings them along -- so listing them as chores to do by hand is noise.
COMPONENT_PATTERNS = (
    re.compile(r"visual c\+\+ .*redistributable", re.IGNORECASE),
    re.compile(r"\bvcredist\b", re.IGNORECASE),
    re.compile(r"\.net\s*(core\s*)?(framework|runtime|sdk|desktop runtime)", re.IGNORECASE),
    re.compile(r"microsoft asp\.net core", re.IGNORECASE),
    re.compile(r"windows (software development kit|sdk|driver kit)", re.IGNORECASE),
    re.compile(r"\bdriver( package)?\b", re.IGNORECASE),
    re.compile(r"webview2 runtime", re.IGNORECASE),
    re.compile(r"\bruntime\b.*\b(x64|x86|arm64)\b", re.IGNORECASE),
    re.compile(r"microsoft (visual studio )?tools for", re.IGNORECASE),
    re.compile(r"^(intel|nvidia|amd|realtek)\b.*\b(driver|chipset|audio|graphics)\b", re.IGNORECASE),
)


@dataclass(slots=True)
class SoftwareEntry:
    """One installed application, from whichever source knew about it."""

    name: str
    version: str = ""
    publisher: str = ""
    sources: list[str] = field(default_factory=list)   # registry | winget | appx
    winget_id: str | None = None
    appx_family: str | None = None
    scope: str = ""                                    # machine | user
    architecture: str = ""
    #: True when winget reported this application but has no package for it, so
    #: the "no match" is winget's own answer rather than a failure to guess.
    winget_knows_no_package: bool = False
    #: True for the Click-to-Run entries that the Office item already covers.
    #: Listing them as applications to reinstall by hand contradicts the Office
    #: follow-up sitting next to them.
    covered_by_office: bool = False

    @property
    def reinstallable(self) -> bool:
        return self.winget_id is not None

    @property
    def is_component(self) -> bool:
        """A runtime or driver that arrives with whatever needs it."""
        return any(pattern.search(self.name) for pattern in COMPONENT_PATTERNS)

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name, "version": self.version}
        if self.publisher:
            data["publisher"] = self.publisher
        data["sources"] = sorted(set(self.sources))
        if self.winget_id:
            data["winget_id"] = self.winget_id
        if self.appx_family:
            data["appx_family"] = self.appx_family
        if self.scope:
            data["scope"] = self.scope
        if self.architecture:
            data["architecture"] = self.architecture
        if self.is_component:
            data["component"] = True
        if self.winget_knows_no_package:
            data["winget_has_no_package"] = True
        if self.covered_by_office:
            data["covered_by_office"] = True
        return data


@dataclass(slots=True)
class SoftwareInventory:
    """The merged inventory plus winget's own export, ready for restore."""

    entries: list[SoftwareEntry] = field(default_factory=list)
    winget_export: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)
    join_stats: "JoinStats | None" = None

    @property
    def reinstallable(self) -> list[SoftwareEntry]:
        return [entry for entry in self.entries if entry.reinstallable]

    @property
    def manual(self) -> list[SoftwareEntry]:
        """Applications a person would actually have to reinstall themselves."""
        return [
            entry
            for entry in self.entries
            if not entry.reinstallable
            and not entry.is_component
            and not entry.covered_by_office
        ]

    @property
    def components(self) -> list[SoftwareEntry]:
        return [
            entry for entry in self.entries if not entry.reinstallable and entry.is_component
        ]

    def to_json(self) -> dict[str, Any]:
        return {
            "counts": {
                "total": len(self.entries),
                "reinstallable_with_winget": len(self.reinstallable),
                "manual": len(self.manual),
                "components": len(self.components),
                "covered_by_office": sum(1 for e in self.entries if e.covered_by_office),
                "winget_packages_in_export": len(packages_from_export(self.winget_export)),
            },
            "applications": [entry.to_json() for entry in self.entries],
            "winget_export": self.winget_export,
            "diagnostics": self.join_stats.to_json() if self.join_stats else {},
            "notes": list(self.notes),
        }


# --- registry --------------------------------------------------------------
def read_registry_entries(env: Environment) -> list[SoftwareEntry]:
    """Enumerate the uninstall keys, skipping updates and system components."""
    entries: list[SoftwareEntry] = []
    for hive, key in UNINSTALL_KEYS:
        for subkey in env.registry_subkeys(hive, key):
            values = env.read_registry_key(hive, f"{key}\\{subkey}") or {}
            entry = _entry_from_registry(values, hive, key)
            if entry is not None:
                entries.append(entry)
    return entries


def _entry_from_registry(values: dict, hive: str, key: str) -> SoftwareEntry | None:
    name = str(values.get("DisplayName") or "").strip()
    if not name:
        return None
    if _as_int(values.get("SystemComponent")) == 1:
        return None
    if str(values.get("ReleaseType") or "").strip().lower() in UPDATE_RELEASE_TYPES:
        return None
    if values.get("ParentKeyName"):
        # A child entry of another product's update tree, not a product itself.
        return None
    if UPDATE_NAME_PATTERN.match(name):
        return None
    return SoftwareEntry(
        name=name,
        version=str(values.get("DisplayVersion") or "").strip(),
        publisher=str(values.get("Publisher") or "").strip(),
        sources=["registry"],
        scope="user" if hive == HKCU else "machine",
        architecture="x86" if "WOW6432Node" in key else "",
    )


def _as_int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


# --- winget ----------------------------------------------------------------
def run_winget_export(runner=process.run) -> tuple[dict[str, Any] | None, str | None]:
    """Ask winget for a machine-readable export of what it can reinstall.

    ``winget export`` produces the same JSON that ``winget import`` consumes,
    which is why it is preferred over scraping ``winget list``'s column output.
    Returns ``(export, error)``.
    """
    with tempfile.TemporaryDirectory() as directory:
        target = Path(directory) / "winget-export.json"
        result = runner(
            [
                "winget",
                "export",
                "-o",
                str(target),
                "--accept-source-agreements",
                "--disable-interactivity",
                "--include-versions",
            ],
            timeout=180,
        )
        if not target.is_file():
            # winget exits non-zero when some installed packages are not in any
            # source, but it still writes the file; only a missing file is fatal.
            return None, result.summary()
        try:
            return json.loads(target.read_text(encoding="utf-8")), None
        except (OSError, json.JSONDecodeError) as exc:
            return None, f"winget export was unreadable: {exc}"


def packages_from_export(export: dict[str, Any] | None) -> list[tuple[str, str]]:
    """Flatten an export into ``(package_id, version)`` pairs."""
    if not export:
        return []
    packages: list[tuple[str, str]] = []
    for source in export.get("Sources", []) or []:
        for package in source.get("Packages", []) or []:
            identifier = package.get("PackageIdentifier")
            if identifier:
                packages.append((identifier, package.get("Version", "")))
    return packages


# --- winget list -----------------------------------------------------------
@dataclass(slots=True)
class WingetListing:
    """One row of ``winget list``."""

    name: str
    identifier: str
    version: str = ""
    source: str = ""

    @property
    def has_package(self) -> bool:
        """False for the synthetic ``ARP\\...`` ids winget invents for unknowns.

        winget lists everything installed. Entries it has no package for get a
        generated id containing backslashes and no source, which is winget
        telling us plainly that it cannot reinstall that one.
        """
        return bool(self.identifier) and "\\" not in self.identifier


def run_winget_list(runner=process.run) -> tuple[list[WingetListing], str | None]:
    """Ask winget what it sees installed, and under which package ids."""
    result = runner(
        ["winget", "list", "--accept-source-agreements", "--disable-interactivity"],
        timeout=180,
    )
    if not result.ok and not result.stdout.strip():
        return [], result.summary()
    listings = parse_winget_list(result.stdout)
    if not listings:
        # Log what it actually printed: the shape of this table is the one thing
        # here that varies by winget version and locale, so a report of "no
        # readable rows" is useless without a sample to fix the parser against.
        sample = strip_ansi(result.stdout or "").splitlines()[:6]
        log.warning(
            "winget list output was not parseable; first lines: %s",
            " | ".join(line[:120] for line in sample) or "(no output)",
        )
        return [], "winget list produced no readable rows"
    return listings, None


def parse_winget_list(text: str) -> list[WingetListing]:
    """Parse ``winget list``'s fixed-width table.

    Deliberately independent of the header's *words*. winget is localised, so on
    a German machine the columns read ``Name / Kennung / Version / Verfügbar /
    Quelle`` and matching the literal "Id" finds nothing -- which is exactly how
    this silently fell back to guessing on the first real machine it met.

    What is stable across languages is the shape: a header row, a rule of dashes
    under it, then rows, with columns in a fixed order (name, id, version,
    available, source). Column positions are taken from the header's word starts
    and mapped by position. Terminal escape sequences are stripped first.
    """
    lines = strip_ansi(text or "").splitlines()
    header_index, offsets = _find_header(lines)
    if not offsets:
        return []

    listings: list[WingetListing] = []
    for line in lines[header_index + 1 :]:
        if not line.strip() or _is_rule(line):
            continue
        fields = _slice_columns(line, offsets)
        name = fields.get("Name", "").strip()
        identifier = fields.get("Id", "").strip()
        if not name or not identifier:
            continue
        listings.append(
            WingetListing(
                name=name,
                identifier=identifier,
                version=fields.get("Version", "").strip(),
                source=fields.get("Source", "").strip(),
            )
        )
    return listings


WINGET_COLUMNS = ("Name", "Id", "Version", "Available", "Source")

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

#: The rule under winget's header, in ASCII or box-drawing form.
RULE_CHARACTERS = set("-\u2500\u2501\u2504\u2505 ")


def strip_ansi(text: str) -> str:
    """Remove terminal escape sequences, which shift every column offset."""
    return ANSI_PATTERN.sub("", text).replace("\x08", "")


def _is_rule(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and set(stripped) <= RULE_CHARACTERS


def _find_header(lines: list[str]) -> tuple[int, dict[str, int] | None]:
    """Locate the header row and the start offset of each column.

    The rule of dashes is the reliable anchor: whatever the language, the line
    above it is the header. An English header is still accepted directly, for
    output that arrives without a rule.
    """
    for index, line in enumerate(lines):
        if not _is_rule(line) or len(line.strip()) < 20 or index == 0:
            continue
        header = lines[index - 1]
        offsets = _columns_by_position(header)
        if offsets:
            return index - 1, offsets

    for index, line in enumerate(lines):
        if "Name" in line and "Id" in line:
            offsets = _columns_by_position(line)
            if offsets:
                return index, offsets
    return -1, None


WORD_START_PATTERN = re.compile(r"\S+")
#: Fallback for a locale whose column names contain spaces.
WIDE_GAP_PATTERN = re.compile(r"(?:^|\s{2,})(\S)")


def _columns_by_position(header: str) -> dict[str, int] | None:
    """Map winget's fixed column order onto the header's word-start offsets.

    Header names are single words, so word starts give the column boundaries --
    and they must, because winget separates two columns by a single space when a
    column happens to be exactly as wide as its heading. Only if that yields
    more starts than there are columns does a locale evidently use a multi-word
    heading, and the wider two-space rule is used instead.
    """
    starts = [match.start() for match in WORD_START_PATTERN.finditer(header)]
    if len(starts) > len(WINGET_COLUMNS):
        starts = [match.start(1) for match in WIDE_GAP_PATTERN.finditer(header)]
    if len(starts) < 2:
        return None
    return {name: start for name, start in zip(WINGET_COLUMNS, starts)}


def _slice_columns(line: str, offsets: dict[str, int]) -> dict[str, str]:
    ordered = sorted(offsets.items(), key=lambda item: item[1])
    fields: dict[str, str] = {}
    for position, (column, start) in enumerate(ordered):
        end = ordered[position + 1][1] if position + 1 < len(ordered) else len(line)
        fields[column] = line[start:end]
    return fields


#: winget truncates long names to fit its columns, marking them with an ellipsis.
TRUNCATION_MARKERS = ("\u2026", "...")

TOKEN_PATTERN = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def tokens(text: str) -> list[str]:
    """Split an identifier into lowercase words, honouring CamelCase.

    ``Microsoft.DesktopAppInstaller`` -> ``[microsoft, desktop, app, installer]``.
    """
    return [match.group(0).lower() for match in TOKEN_PATTERN.finditer(text or "")]


def is_subsequence(needle: list[str], haystack: list[str]) -> bool:
    """True when every word of ``needle`` appears in ``haystack``, in order."""
    iterator = iter(haystack)
    return all(word in iterator for word in needle)


def identity_resembles(appx_name: str, identifier: str) -> bool:
    """True when an MSIX package identity plainly denotes a winget package.

    Microsoft ships these under names that differ by a word or two -- the
    package ``Microsoft.DesktopAppInstaller`` is winget's ``Microsoft.AppInstaller``,
    and ``Microsoft.OutlookForWindows`` is ``Microsoft.Outlook``. The publisher
    must agree and the id's remaining words must appear in the package's name in
    order, which is loose enough for those and tight enough to keep unrelated
    packages apart.
    """
    package_words = tokens(appx_name)
    id_words = tokens(identifier)
    if len(package_words) < 2 or len(id_words) < 2:
        return False
    if package_words[0] != id_words[0]:
        return False
    return is_subsequence(id_words[1:], package_words[1:])


@dataclass(slots=True)
class JoinStats:
    """How well ``winget list`` joined onto the registry inventory.

    Recorded because the join rate is the one number that says whether the
    automatic/manual split can be trusted, and it cannot be checked from here --
    only on a real machine with real software on it.
    """

    listed_rows: int = 0
    rows_with_package: int = 0
    joined_exactly: int = 0
    joined_by_identity: int = 0
    joined_by_prefix: int = 0
    unjoined_rows_with_package: int = 0
    #: Framework packages the Appx inventory deliberately filters out. They are
    #: not failures to join, so counting them as such overstates the problem.
    unjoined_framework_packages: int = 0
    #: The ids that genuinely joined to nothing, so the remainder is
    #: diagnosable rather than merely counted.
    unjoined_identifiers: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "winget_list_rows": self.listed_rows,
            "winget_list_rows_with_package": self.rows_with_package,
            "joined_by_exact_name": self.joined_exactly,
            "joined_by_package_identity": self.joined_by_identity,
            "joined_by_truncated_name": self.joined_by_prefix,
            "winget_packages_not_joined": self.unjoined_rows_with_package,
            "excluded_framework_packages": self.unjoined_framework_packages,
            "unjoined_examples": self.unjoined_identifiers[:25],
        }


def apply_winget_listings(
    entries: list[SoftwareEntry], listings: list[WingetListing]
) -> JoinStats:
    """Attach package ids from ``winget list`` by exact display-name match.

    The names winget prints for desktop applications come from the same registry
    values we read, so they join up directly. Truncated names are matched on
    their prefix instead.
    """
    by_name: dict[str, WingetListing] = {}
    #: MSIX packages join on identity, not display name: Get-AppxPackage reports
    #: "Microsoft.WindowsTerminal" where winget list prints "Windows Terminal",
    #: so the two only meet through the package id.
    by_identifier: dict[str, WingetListing] = {}
    truncated: list[tuple[str, WingetListing]] = []
    for listing in listings:
        if listing.has_package:
            by_identifier.setdefault(squash(listing.identifier), listing)
        name = listing.name
        marker = next((m for m in TRUNCATION_MARKERS if name.endswith(m)), None)
        if marker:
            truncated.append((squash(name[: -len(marker)]), listing))
        else:
            by_name.setdefault(squash(name), listing)

    stats = JoinStats(
        listed_rows=len(listings),
        rows_with_package=sum(1 for listing in listings if listing.has_package),
    )
    used_listings: set[int] = set()
    for entry in entries:
        key = squash(entry.name)
        listing = by_name.get(key)
        by_prefix = False
        by_identity = False
        if listing is None and entry.appx_family:
            listing = by_identifier.get(key)
            if listing is None:
                listing = next(
                    (
                        candidate
                        for candidate in listings
                        if candidate.has_package
                        and identity_resembles(entry.name, candidate.identifier)
                    ),
                    None,
                )
            by_identity = listing is not None
        if listing is None:
            listing = next(
                (item for prefix, item in truncated if prefix and key.startswith(prefix)), None
            )
            by_prefix = listing is not None
        if listing is None:
            continue
        used_listings.add(id(listing))
        if listing.has_package:
            entry.winget_id = listing.identifier
            entry.sources.append("winget")
            if by_prefix:
                stats.joined_by_prefix += 1
            elif by_identity:
                stats.joined_by_identity += 1
            else:
                stats.joined_exactly += 1
        else:
            entry.winget_knows_no_package = True

    # Counted per package id, not per row: one application installed at two
    # versions produces two rows with the same id, and the second was being
    # reported as a package that joined to nothing.
    claimed = {squash(entry.winget_id) for entry in entries if entry.winget_id}
    seen_identifiers: set[str] = set()
    for listing in listings:
        identifier = squash(listing.identifier)
        if not listing.has_package or id(listing) in used_listings:
            continue
        if identifier in claimed or identifier in seen_identifiers:
            continue
        seen_identifiers.add(identifier)
        if is_framework_package(listing.identifier):
            stats.unjoined_framework_packages += 1
            continue
        stats.unjoined_rows_with_package += 1
        stats.unjoined_identifiers.append(listing.identifier)
    return stats


#: winget ids for the runtime frameworks the Appx inventory filters out. Listed
#: separately from APPX_NOISE_PREFIXES because the two naming schemes disagree:
#: the package is "Microsoft.NET.Native.Runtime.2.2", winget calls the same
#: thing "Microsoft.DotNet.Native.Runtime".
FRAMEWORK_PACKAGE_PREFIXES = (
    "Microsoft.UI.Xaml",
    "Microsoft.VCLibs",
    "Microsoft.DotNet.Native",
    "Microsoft.NET.Native",
    "Microsoft.Services.Store",
    "Microsoft.Windows.",
)


def is_framework_package(identifier: str) -> bool:
    """True for the runtime frameworks the Appx inventory filters out.

    ``Microsoft.UI.Xaml.2.8`` and friends are installed and winget does have
    packages for them, but they are excluded from the inventory as noise, so
    their absence from it is deliberate rather than a join that failed.
    """
    # Matched on component boundaries, not on squashed text: squashing turns
    # "Microsoft.Windows." into "microsoftwindows", which also prefixes
    # "Microsoft.WindowsTerminal" -- a real application, not a framework.
    lowered = identifier.lower()
    for prefix in (*FRAMEWORK_PACKAGE_PREFIXES, *APPX_NOISE_PREFIXES):
        candidate = prefix.lower().rstrip(".")
        if lowered == candidate or lowered.startswith(candidate + "."):
            return True
    return False


# --- appx ------------------------------------------------------------------
APPX_SCRIPT = (
    "Get-AppxPackage | Select-Object Name,PackageFamilyName,Publisher,Version,Architecture "
    "| ConvertTo-Json -Compress -Depth 3"
)


def read_appx_entries(runner=process.powershell) -> tuple[list[SoftwareEntry], str | None]:
    """Enumerate Store/UWP packages for the current user."""
    result = runner(APPX_SCRIPT, timeout=180)
    if not result.ok:
        return [], result.summary()
    return parse_appx_json(result.stdout), None


def parse_appx_json(text: str) -> list[SoftwareEntry]:
    """Parse ``Get-AppxPackage | ConvertTo-Json`` output.

    PowerShell emits a bare object rather than a list when there is exactly one
    result, which is a classic source of breakage, so both shapes are accepted.
    """
    text = (text or "").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    entries = []
    for package in data:
        if not isinstance(package, dict):
            continue
        name = str(package.get("Name") or "").strip()
        if not name or name.lower().startswith(APPX_NOISE_PREFIXES):
            continue
        entries.append(
            SoftwareEntry(
                name=name,
                version=str(package.get("Version") or "").strip(),
                publisher=_publisher_common_name(str(package.get("Publisher") or "")),
                sources=["appx"],
                appx_family=str(package.get("PackageFamilyName") or "").strip() or None,
                scope="user",
                architecture=str(package.get("Architecture") or "").strip(),
            )
        )
    return entries


def _publisher_common_name(publisher: str) -> str:
    """``CN=Microsoft Corporation, O=..`` reads better as just the CN."""
    match = re.search(r"CN=([^,]+)", publisher)
    return match.group(1).strip() if match else publisher.strip()


# --- merging ---------------------------------------------------------------
def squash(text: str) -> str:
    """Reduce a name to comparable characters only: ``7-Zip 23.01`` -> ``7zip2301``."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


#: A candidate this short matches by accident more often than on purpose.
#: Four, not five: "7zip" is a real product token, and this is only a fallback.
MIN_CANDIDATE_LENGTH = 4

#: Words that appear in half of all package ids and identify nothing on their
#: own. Without this, Microsoft.VCLibs.Desktop.14 matches "PowerAutomateDesktop"
#: on the word "desktop", and Microsoft.DotNet.Native.Runtime matches anything
#: with "runtime" in its name -- both seen on a real machine.
GENERIC_TOKENS = frozenset(
    {
        "desktop", "runtime", "runtimes", "client", "clients", "tools", "common",
        "core", "base", "apps", "application", "applications", "library",
        "libraries", "framework", "redistributable", "redist", "community",
        "professional", "enterprise", "standard", "essentials", "launcher",
        "player", "manager", "service", "services", "software", "package",
        "packages", "installer", "update", "setup", "windows", "microsoft",
        "native", "shared", "support", "utility", "utilities", "driver",
        "drivers", "x64", "x86", "arm64", "win32", "win64",
    }
)


def id_candidates(identifier: str) -> list[str]:
    """Squashed substrings of a package id worth looking for in a display name.

    Ids are ``Publisher.Product``, but often deeper --
    ``Microsoft.VCRedist.2015+.x64``, ``Microsoft.VisualStudio.2022.Community``.
    Taking only the last component matches those on ``x64`` and ``community``,
    which is how a matcher ends up either wrong or silent. Every contiguous run
    of components after the publisher is tried instead, longest first.
    """
    parts = [part for part in identifier.split(".") if part]
    if not parts:
        return []
    candidates: list[str] = [squash("".join(parts))]
    tail = parts[1:] if len(parts) > 1 else parts
    for start in range(len(tail)):
        for end in range(len(tail), start, -1):
            candidates.append(squash("".join(tail[start:end])))
    seen: set[str] = set()
    ordered = []
    for candidate in candidates:
        if len(candidate) < MIN_CANDIDATE_LENGTH or candidate in seen:
            continue
        if candidate in GENERIC_TOKENS:
            continue
        seen.add(candidate)
        ordered.append(candidate)
    return sorted(ordered, key=len, reverse=True)


def match_winget_id(entry: SoftwareEntry, packages: list[tuple[str, str]]) -> str | None:
    """Guess which winget package id corresponds to an installed application.

    Only a fallback: ``winget list`` answers this exactly for anything it
    reported. Used for entries winget did not mention at all, and it decides a
    report label rather than the reinstall, which replays winget's own export.
    """
    name = squash(entry.name)
    if not name:
        return None
    publisher = squash(entry.publisher)
    best: tuple[int, str] | None = None
    for identifier, _version in packages:
        parts = identifier.split(".")
        publisher_part = squash(parts[0]) if len(parts) > 1 else ""
        confident = (
            not publisher_part
            or publisher_part in publisher
            or publisher_part in name
        )
        for candidate in id_candidates(identifier):
            if candidate not in name:
                continue
            score = len(candidate) + (10 if confident else 0)
            if best is None or score > best[0]:
                best = (score, identifier)
            break
    return best[1] if best else None


def merge(
    registry_entries: list[SoftwareEntry],
    appx_entries: list[SoftwareEntry],
    packages: list[tuple[str, str]],
    listings: list[WingetListing] | None = None,
) -> tuple[list[SoftwareEntry], JoinStats | None]:
    """Combine the sources, de-duplicate, and attach winget ids.

    ``winget list`` output, when available, is applied first and treated as
    authoritative; name matching only fills gaps it left.
    """
    merged: dict[str, SoftwareEntry] = {}
    for entry in [*registry_entries, *appx_entries]:
        key = f"{squash(entry.name)}|{squash(entry.version)}"
        existing = merged.get(key)
        if existing is None:
            merged[key] = entry
            continue
        existing.sources.extend(entry.sources)
        existing.publisher = existing.publisher or entry.publisher
        existing.appx_family = existing.appx_family or entry.appx_family
        existing.architecture = existing.architecture or entry.architecture

    entries = list(merged.values())
    stats = apply_winget_listings(entries, listings) if listings else None

    used = {entry.winget_id for entry in entries if entry.winget_id}
    for entry in entries:
        if entry.winget_id or entry.winget_knows_no_package:
            # Already answered, and winget's own answer beats a guess.
            continue
        identifier = match_winget_id(entry, packages)
        if identifier and identifier not in used:
            entry.winget_id = identifier
            entry.sources.append("winget")
            used.add(identifier)
    return sorted(entries, key=lambda item: item.name.lower()), stats


def scan_software(env: Environment) -> SoftwareInventory:
    """Build the merged inventory for this machine."""
    inventory = SoftwareInventory()
    registry_entries = read_registry_entries(env)

    export, export_error = (None, "winget is only queried on Windows")
    listings, listing_error = [], None
    appx_entries, appx_error = [], "Appx packages are only queried on Windows"
    if env.is_windows:
        export, export_error = run_winget_export()
        listings, listing_error = run_winget_list()
        appx_entries, appx_error = read_appx_entries()

    if export_error:
        inventory.notes.append(f"winget export: {export_error}")
    if listing_error:
        inventory.notes.append(
            f"winget list: {listing_error} — falling back to name matching, "
            "so the automatic/manual split is approximate"
        )
    if appx_error:
        inventory.notes.append(f"appx: {appx_error}")

    inventory.winget_export = export
    inventory.entries, inventory.join_stats = merge(
        registry_entries, appx_entries, packages_from_export(export), listings
    )
    stats = inventory.join_stats
    if stats and stats.unjoined_rows_with_package:
        # winget knows a package for these, but its display name did not join to
        # any registry entry, so they are being reported as manual work wrongly.
        inventory.notes.append(
            f"{stats.unjoined_rows_with_package} of {stats.rows_with_package} packages "
            "winget listed could not be matched to an installed application by name, "
            "so the by-hand count is higher than it should be "
            f"(examples: {', '.join(stats.unjoined_identifiers[:5])})"
        )
    return inventory


#: Click-to-Run products register one uninstall entry per product and language.
OFFICE_ENTRY_PATTERN = re.compile(
    r"^Microsoft (Office|Visio|Project|365|Access|Excel|OneNote|Outlook|"
    r"PowerPoint|Publisher|Word)\b",
    re.IGNORECASE,
)


def flag_office_entries(inventory: SoftwareInventory, office_version: str) -> int:
    """Mark applications that the Office item already accounts for.

    Matched on the Click-to-Run build version rather than on the name alone, so
    a separately installed Microsoft application is not swept up with it.
    Returns how many were flagged.
    """
    if not office_version:
        return 0
    flagged = 0
    for entry in inventory.entries:
        if entry.version == office_version and OFFICE_ENTRY_PATTERN.match(entry.name):
            entry.covered_by_office = True
            flagged += 1
    return flagged
