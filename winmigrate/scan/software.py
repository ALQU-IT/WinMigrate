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
than something reconstructed here. Our name-to-package-id matching only decides
what the *report* calls automatic versus manual; it never has to be right for
the reinstall itself to work.
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

    @property
    def reinstallable(self) -> bool:
        return self.winget_id is not None

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
        return data


@dataclass(slots=True)
class SoftwareInventory:
    """The merged inventory plus winget's own export, ready for restore."""

    entries: list[SoftwareEntry] = field(default_factory=list)
    winget_export: dict[str, Any] | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def reinstallable(self) -> list[SoftwareEntry]:
        return [entry for entry in self.entries if entry.reinstallable]

    @property
    def manual(self) -> list[SoftwareEntry]:
        return [entry for entry in self.entries if not entry.reinstallable]

    def to_json(self) -> dict[str, Any]:
        return {
            "counts": {
                "total": len(self.entries),
                "reinstallable_with_winget": len(self.reinstallable),
                "manual": len(self.manual),
            },
            "applications": [entry.to_json() for entry in self.entries],
            "winget_export": self.winget_export,
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


def match_winget_id(entry: SoftwareEntry, packages: list[tuple[str, str]]) -> str | None:
    """Guess which winget package id corresponds to an installed application.

    Package ids are ``Publisher.Product`` (sometimes deeper). The product part
    is matched against the squashed display name, longest first so that
    ``Microsoft.VisualStudioCode`` wins over a shorter accidental substring.

    This only labels the report. The reinstall itself replays winget's own
    export, so a wrong guess here costs a misleading line, not a failed install.
    """
    name = squash(entry.name)
    if not name:
        return None
    best: tuple[int, str] | None = None
    for identifier, _version in packages:
        parts = identifier.split(".")
        product = squash(parts[-1])
        if len(product) < 3 or product not in name:
            continue
        publisher_part = squash(parts[0]) if len(parts) > 1 else ""
        confident = (
            not publisher_part
            or publisher_part in squash(entry.publisher)
            or publisher_part in name
        )
        score = len(product) + (10 if confident else 0)
        if best is None or score > best[0]:
            best = (score, identifier)
    return best[1] if best else None


def merge(
    registry_entries: list[SoftwareEntry],
    appx_entries: list[SoftwareEntry],
    packages: list[tuple[str, str]],
) -> list[SoftwareEntry]:
    """Combine the sources, de-duplicate, and attach winget ids."""
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

    used: set[str] = set()
    for entry in merged.values():
        identifier = match_winget_id(entry, packages)
        if identifier and identifier not in used:
            entry.winget_id = identifier
            entry.sources.append("winget")
            used.add(identifier)
    return sorted(merged.values(), key=lambda item: item.name.lower())


def scan_software(env: Environment) -> SoftwareInventory:
    """Build the merged inventory for this machine."""
    inventory = SoftwareInventory()
    registry_entries = read_registry_entries(env)

    export, export_error = (None, "winget is only queried on Windows")
    appx_entries, appx_error = [], "Appx packages are only queried on Windows"
    if env.is_windows:
        export, export_error = run_winget_export()
        appx_entries, appx_error = read_appx_entries()

    if export_error:
        inventory.notes.append(f"winget: {export_error}")
    if appx_error:
        inventory.notes.append(f"appx: {appx_error}")

    inventory.winget_export = export
    inventory.entries = merge(registry_entries, appx_entries, packages_from_export(export))
    return inventory
