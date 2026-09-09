"""Windows platform access, wrapped so the scan stage stays testable.

Every Windows-specific read (registry, known folders, file attributes) goes
through :class:`Environment`. On Windows the live implementation uses ``winreg``
and ``os.stat``; elsewhere -- development hosts and the test suite -- an
``Environment`` can be built over a fixture profile tree with a supplied
registry dictionary, so the scan logic itself is exercised on any OS.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .util import paths as pathutil

# --- Win32 file attribute bits we care about -------------------------------
FILE_ATTRIBUTE_REPARSE_POINT = 0x0400
FILE_ATTRIBUTE_OFFLINE = 0x1000
FILE_ATTRIBUTE_RECALL_ON_OPEN = 0x00040000
FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS = 0x00400000

#: A file with any of these set has no local content: reading it would make a
#: sync client download it. We record such files and skip their bytes.
CLOUD_PLACEHOLDER_MASK = (
    FILE_ATTRIBUTE_OFFLINE
    | FILE_ATTRIBUTE_RECALL_ON_OPEN
    | FILE_ATTRIBUTE_RECALL_ON_DATA_ACCESS
)

HKCU = "HKCU"
HKLM = "HKLM"

USER_SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
SHELL_FOLDERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"

#: Known folder id -> (registry value name under User Shell Folders,
#: profile-relative fallback, human title).
KNOWN_FOLDERS: dict[str, tuple[str, str, str]] = {
    "desktop": ("Desktop", "Desktop", "Desktop"),
    "documents": ("Personal", "Documents", "Documents"),
    "downloads": ("{374DE290-123F-4565-9164-39C4925E467B}", "Downloads", "Downloads"),
    "pictures": ("My Pictures", "Pictures", "Pictures"),
    "music": ("My Music", "Music", "Music"),
    "videos": ("My Video", "Videos", "Videos"),
    "favorites": ("Favorites", "Favorites", "Favorites"),
    "links": ("{BFB9D5E0-C6A9-404C-B2B2-AE6DB6AF4968}", "Links", "Links"),
    "saved_games": ("{4C5C32FF-BB9D-43B0-B5B4-2D72E54EAAA4}", "Saved Games", "Saved Games"),
    "searches": ("{7D1D3A04-DEBB-4115-95CF-2F29DA2920DA}", "Searches", "Searches"),
    "contacts": ("{56784854-C6CB-462B-8169-88E350ACB882}", "Contacts", "Contacts"),
}


@dataclass(slots=True)
class Environment:
    """The machine as the scan stage sees it.

    ``registry`` short-circuits live reads. Keys are ``"HKCU\\Some\\Key"`` and
    values are ``{value_name: value}`` dicts; a fixture can therefore describe
    OneDrive accounts, shell folder redirection and so on without a Windows box.
    """

    profile_root: Path
    environ: dict[str, str] = field(default_factory=lambda: dict(os.environ))
    registry: dict[str, dict[str, Any]] | None = None
    is_windows: bool = field(default_factory=lambda: sys.platform == "win32")

    # -- construction -------------------------------------------------------
    @classmethod
    def live(cls) -> "Environment":
        """The real current user's environment."""
        profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        return cls(profile_root=Path(profile))

    @classmethod
    def fixture(
        cls,
        profile_root: os.PathLike[str] | str,
        registry: dict[str, dict[str, Any]] | None = None,
        environ: dict[str, str] | None = None,
    ) -> "Environment":
        """An environment rooted at a fake profile tree, for tests and dev runs."""
        root = Path(os.fspath(profile_root))
        env = dict(environ or {})
        env.setdefault("USERPROFILE", str(root))
        env.setdefault("APPDATA", str(root / "AppData" / "Roaming"))
        env.setdefault("LOCALAPPDATA", str(root / "AppData" / "Local"))
        return cls(profile_root=root, environ=env, registry=registry or {}, is_windows=False)

    # -- registry -----------------------------------------------------------
    def read_registry_value(self, hive: str, key: str, name: str) -> Any | None:
        """Read a single registry value, or ``None`` if absent/unreadable."""
        values = self.read_registry_key(hive, key)
        if values is None:
            return None
        return values.get(name)

    def read_registry_key(self, hive: str, key: str) -> dict[str, Any] | None:
        """Read every value under a key, or ``None`` if the key is absent."""
        if self.registry is not None:
            return self.registry.get(f"{hive}\\{key}")
        if not self.is_windows:
            return None
        return self._read_live_registry_key(hive, key)

    def registry_subkeys(self, hive: str, key: str) -> list[str]:
        """List subkey names, empty when the key does not exist."""
        if self.registry is not None:
            prefix = f"{hive}\\{key}\\"
            names = set()
            for full in self.registry:
                if full.startswith(prefix):
                    names.add(full[len(prefix) :].split("\\", 1)[0])
            return sorted(names)
        if not self.is_windows:
            return []
        return self._list_live_subkeys(hive, key)

    def _read_live_registry_key(self, hive: str, key: str) -> dict[str, Any] | None:
        import winreg  # noqa: PLC0415 -- Windows-only import

        try:
            with winreg.OpenKey(self._hive(hive), key) as handle:
                _, value_count, _ = winreg.QueryInfoKey(handle)
                values: dict[str, Any] = {}
                for index in range(value_count):
                    name, value, _kind = winreg.EnumValue(handle, index)
                    values[name] = value
                return values
        except OSError:
            return None

    def _list_live_subkeys(self, hive: str, key: str) -> list[str]:
        import winreg  # noqa: PLC0415 -- Windows-only import

        try:
            with winreg.OpenKey(self._hive(hive), key) as handle:
                subkey_count, _, _ = winreg.QueryInfoKey(handle)
                return [winreg.EnumKey(handle, index) for index in range(subkey_count)]
        except OSError:
            return []

    @staticmethod
    def _hive(hive: str):
        import winreg  # noqa: PLC0415 -- Windows-only import

        return {
            HKCU: winreg.HKEY_CURRENT_USER,
            HKLM: winreg.HKEY_LOCAL_MACHINE,
        }[hive]

    # -- environment --------------------------------------------------------
    def env_var(self, name: str) -> str | None:
        """Case-insensitive environment lookup.

        Windows environment variable names are case-insensitive, but
        ``dict(os.environ)`` snapshots them upper-cased, so a literal
        ``environ.get("OneDrive")`` never matches on the very platform the
        variable comes from.
        """
        value = self.environ.get(name)
        if value is not None:
            return value
        lowered = name.lower()
        for key, candidate in self.environ.items():
            if key.lower() == lowered:
                return candidate
        return None

    # -- path resolution ----------------------------------------------------
    def resolve_path(self, raw: str) -> Path:
        """Turn a registry-shaped path string into a usable :class:`Path`.

        Expands ``%VARS%`` against this environment. On a non-Windows host --
        i.e. a fixture run -- backslashes are also translated, so registry data
        copied verbatim from a real machine still addresses the fixture tree.
        """
        expanded = pathutil.expand(raw, self.environ)
        if not self.is_windows:
            expanded = expanded.replace("\\", "/")
        return Path(expanded)

    # -- known folders ------------------------------------------------------
    def known_folder(self, folder_id: str) -> Path:
        """Resolve a known folder, honouring redirection.

        OneDrive's Known Folder Move rewrites these registry values to point
        inside the sync root, which is exactly the case the sync-skip logic
        needs to see, so the registry is consulted before the default.
        """
        value_name, fallback, _title = KNOWN_FOLDERS[folder_id]
        raw = self.read_registry_value(HKCU, USER_SHELL_FOLDERS_KEY, value_name)
        if raw is None:
            raw = self.read_registry_value(HKCU, SHELL_FOLDERS_KEY, value_name)
        if isinstance(raw, str) and raw.strip():
            return self.resolve_path(raw)
        return self.profile_root / fallback

    def known_folder_title(self, folder_id: str) -> str:
        return KNOWN_FOLDERS[folder_id][2]

    # -- filesystem ---------------------------------------------------------
    def appdata_roaming(self) -> Path:
        value = self.env_var("APPDATA")
        return self.resolve_path(value) if value else self.profile_root / "AppData" / "Roaming"

    def appdata_local(self) -> Path:
        value = self.env_var("LOCALAPPDATA")
        return self.resolve_path(value) if value else self.profile_root / "AppData" / "Local"

    def file_attributes(self, entry: os.DirEntry[str] | os.stat_result) -> int:
        """Win32 attribute bits for a directory entry, 0 where unavailable."""
        stat_result = entry.stat(follow_symlinks=False) if hasattr(entry, "stat") else entry
        return getattr(stat_result, "st_file_attributes", 0)

    def is_cloud_placeholder(self, entry: os.DirEntry[str]) -> bool:
        """True when a file's bytes are not on this disk."""
        try:
            return bool(self.file_attributes(entry) & CLOUD_PLACEHOLDER_MASK)
        except OSError:
            return False


def require_windows(allow_override: bool = True) -> None:
    """Raise unless running on Windows.

    ``WINMIGRATE_ALLOW_NON_WINDOWS=1`` lets the read-only stages run against a
    fixture tree on a development host. It never enables capture or restore.
    """
    from .errors import PlatformError  # noqa: PLC0415 -- avoid import cycle

    if sys.platform == "win32":
        return
    if allow_override and os.environ.get("WINMIGRATE_ALLOW_NON_WINDOWS") == "1":
        return
    raise PlatformError(
        "WinMigrate runs on Windows. Set WINMIGRATE_ALLOW_NON_WINDOWS=1 to run "
        "read-only stages against a fixture profile for development."
    )
