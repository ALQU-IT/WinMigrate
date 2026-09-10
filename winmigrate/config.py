"""Scan/capture configuration: what to look at, what to leave out.

Exclusion patterns follow one simple rule so a user can predict them:

* a pattern **without** ``/`` matches any single path segment -- ``node_modules``
  excludes every directory with that name, ``*.tmp`` every file so named;
* a pattern **with** ``/`` is matched (fnmatch-style, ``*`` crossing separators)
  against the path relative to the profile root, e.g.
  ``AppData/Local/Temp/*``.

Matching is case-insensitive, because the filesystem being scanned is.
"""

from __future__ import annotations

import fnmatch
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import ConfigError

#: Junk, caches and OS artefacts. Excluded unconditionally; the report says how
#: much they came to so the number is never a mystery.
EXCLUDE_ALWAYS: tuple[str, ...] = (
    "Thumbs.db",
    "desktop.ini",
    "ehthumbs.db",
    "~$*",
    "*.tmp",
    "*.temp",
    "hiberfil.sys",
    "pagefile.sys",
    "swapfile.sys",
    "NTUSER.DAT*",
    "ntuser.dat*",
    "$RECYCLE.BIN",
    "System Volume Information",
    "AppData/Local/Temp/*",
    "AppData/Local/Microsoft/Windows/INetCache/*",
    "AppData/Local/Microsoft/Windows/Explorer/*",
    "AppData/Local/Microsoft/Windows/WebCache/*",
    "AppData/Local/CrashDumps/*",
    "AppData/Local/Packages/*/LocalCache/*",
    "AppData/Local/Google/Chrome/User Data/*/Cache/*",
    "AppData/Local/pip/cache/*",
    "AppData/Local/npm-cache/*",
    # Browser credential and cookie stores: encrypted to the source machine, so
    # useless on another, and sensitive. Never carried in a profile copy.
    "Login Data",
    "Login Data-journal",
    "Cookies",
    "Login Data For Account",
    "Login Data For Account-journal",
    "Cookies-journal",
)

#: Regenerable build/dependency output. Excluded by default but reported and
#: re-includable with ``--include-regenerable``; these are the directories that
#: otherwise dominate a developer's bundle.
EXCLUDE_REGENERABLE: tuple[str, ...] = (
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".gradle",
    ".nuget",
    "bin",
    "obj",
    "target",
    "dist",
    "build",
    ".next",
    ".cache",
    # Chromium-family and Firefox cache directories -- regenerable, and the
    # bulk of a browser profile's size.
    "Cache",
    "Cache_Data",
    "cache2",
    "Code Cache",
    "GPUCache",
    "GrShaderCache",
    "ShaderCache",
    "DawnCache",
    "DawnGraphiteCache",
    "DawnWebGPUCache",
    "GraphiteDawnCache",
    "Service Worker",
    "component_crx_cache",
    "Crashpad",
    "Media Cache",
)

#: Known folders captured by default, in report order.
DEFAULT_KNOWN_FOLDERS: tuple[str, ...] = (
    "desktop",
    "documents",
    "downloads",
    "pictures",
    "music",
    "videos",
    "favorites",
    "links",
    "saved_games",
    "contacts",
)

#: Profile-root directories that are never treated as "other user data".
PROFILE_DIRS_NOT_USER_DATA: frozenset[str] = frozenset(
    {
        "appdata",
        "application data",
        "local settings",
        "cookies",
        "recent",
        "nethood",
        "printhood",
        "sendto",
        "start menu",
        "templates",
        "my documents",
        "intelgraphicsprofiles",
        "onedrive",  # handled as a sync root, not as a plain directory
    }
)


@dataclass(slots=True)
class ScanConfig:
    """Everything that changes what a scan reports."""

    profile_root: Path | None = None
    known_folders: tuple[str, ...] = DEFAULT_KNOWN_FOLDERS
    include_other_profile_dirs: bool = True
    skip_synced: bool = True
    include_regenerable: bool = False
    files_only: bool = False
    follow_reparse_points: bool = False
    extra_excludes: tuple[str, ...] = ()
    extra_includes: tuple[str, ...] = ()  # patterns that override an exclusion
    #: Names of exclusion presets applied, for the report only (not matching).
    active_presets: tuple[str, ...] = ()
    #: Inventory installed software and Office. Costs a winget and a PowerShell
    #: call, so it can be switched off for a quick file-only scan.
    include_software: bool = True

    #: Wi-Fi profiles include the network passwords, so they are opt-in.
    include_wifi: bool = False

    #: Walk excluded/synced subtrees to report how many bytes they came to.
    #: Honest numbers cost one extra stat pass; ``--fast`` turns it off.
    measure_skipped: bool = True

    #: Populated by :meth:`compiled_excludes`; cached to keep the walk cheap.
    _exclude_cache: tuple[tuple[str, ...], tuple[str, ...]] | None = field(
        default=None, repr=False, compare=False
    )

    def compiled_excludes(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Return ``(segment_patterns, path_patterns)``, both lowercased."""
        if self._exclude_cache is None:
            patterns = list(EXCLUDE_ALWAYS) + list(self.extra_excludes)
            if not self.include_regenerable:
                patterns += list(EXCLUDE_REGENERABLE)
            segments = tuple(p.lower() for p in patterns if "/" not in p)
            full = tuple(p.lower().replace("\\", "/") for p in patterns if "/" in p)
            self._exclude_cache = (segments, full)
        return self._exclude_cache

    def is_excluded(self, relative_posix: str, name: str) -> bool:
        """True when a path (relative to the profile root) is excluded."""
        lowered_name = name.lower()
        lowered_path = relative_posix.lower()
        for pattern in self.extra_includes:
            lowered_pattern = pattern.lower().replace("\\", "/")
            if "/" in lowered_pattern:
                if fnmatch.fnmatchcase(lowered_path, lowered_pattern):
                    return False
            elif fnmatch.fnmatchcase(lowered_name, lowered_pattern):
                return False
        segments, full = self.compiled_excludes()
        for pattern in segments:
            if fnmatch.fnmatchcase(lowered_name, pattern):
                return True
        for pattern in full:
            if fnmatch.fnmatchcase(lowered_path, pattern):
                return True
        return False

    def is_regenerable(self, name: str) -> bool:
        """True when a directory name is regenerable build/dependency output."""
        lowered = name.lower()
        return any(fnmatch.fnmatchcase(lowered, pattern.lower()) for pattern in EXCLUDE_REGENERABLE)


def load_config_file(path: os.PathLike[str] | str) -> dict[str, Any]:
    """Load a JSON config file of ScanConfig overrides."""
    file_path = Path(os.fspath(path))
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file is not valid JSON: {file_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"config file must contain a JSON object: {file_path}")
    return data


def config_from_dict(data: dict[str, Any], base: ScanConfig | None = None) -> ScanConfig:
    """Apply a dict of overrides onto a :class:`ScanConfig`."""
    config = base or ScanConfig()
    known = {
        "profile_root",
        "known_folders",
        "include_other_profile_dirs",
        "skip_synced",
        "include_regenerable",
        "files_only",
        "follow_reparse_points",
        "extra_excludes",
        "extra_includes",
        "measure_skipped",
        "include_software",
        "include_wifi",
        "exclude_presets",  # resolved by the CLI into exclusion patterns
    }
    unknown = set(data) - known
    if unknown:
        raise ConfigError(f"unknown config keys: {', '.join(sorted(unknown))}")
    for key, value in data.items():
        if key == "exclude_presets":
            continue  # not a ScanConfig field; handled where presets are resolved
        if key == "profile_root":
            value = Path(value)
        elif key in {"known_folders", "extra_excludes", "extra_includes"}:
            value = tuple(value)
        setattr(config, key, value)
    config._exclude_cache = None
    return config
