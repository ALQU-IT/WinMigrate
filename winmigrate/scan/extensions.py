"""Which extensions are in a browser profile, and whether their data came too.

The extensions themselves already travel: they live inside the profile
directory, which is copied whole. ``Extensions/<id>/<version>/`` holds the code,
``Local Extension Settings/<id>/`` and ``IndexedDB/chrome-extension_<id>_0...``
hold what the extension has saved -- your uBlock filter lists, your tab
manager's groups. Firefox is the same shape under ``extensions/*.xpi``,
``browser-extension-data/<id>/`` and ``storage/default/moz-extension+++<uuid>``.

What was missing is knowing about any of it. A profile copy reports "15 files"
and the user finds out on the new machine whether their extensions and settings
survived. That matters more than usual here, because of what happens on the far
side: Chromium records its extension registry in ``Secure Preferences`` behind
an HMAC that is tied to the machine, so a profile opened on a different computer
generally finds the registry invalid and starts with those extensions disabled
or gone -- while the code and the saved data sit there in place, unused, and
come back the moment the extension is installed again.

So this module reads the manifests to say what is there, and the follow-up says
plainly that reinstalling is expected and that reinstalling is enough. Nothing
here interprets extension *data* -- only the manifests that name the extension,
the same config-file reading the rest of the browser scan does.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

#: Firefox add-on locations that are Mozilla's own, not the user's choices.
FIREFOX_BUILTIN_LOCATIONS = frozenset(
    {"app-builtin", "app-system-defaults", "app-system-addons", "app-global", "app-profile-defaults"}
)


@dataclass(slots=True)
class Extension:
    """One installed extension, as named by its own manifest."""

    ext_id: str
    name: str
    version: str
    #: True when the extension has stored data in the profile (settings, rules,
    #: whatever it keeps). That data travels; whether it is *used* depends on
    #: the extension being present again on the new machine.
    has_data: bool = False
    data_bytes: int = 0

    def to_json(self) -> dict:
        return {
            "id": self.ext_id,
            "name": self.name,
            "version": self.version,
            "has_data": self.has_data,
            "data_bytes": self.data_bytes,
        }


def inventory(profile_dir: Path, engine: str) -> list[Extension]:
    """Every extension in ``profile_dir``, sorted by name."""
    try:
        if engine == "firefox":
            found = _firefox(profile_dir)
        else:
            found = _chromium(profile_dir)
    except OSError as exc:
        log.warning("could not inventory extensions in %s: %s", profile_dir, exc)
        return []
    return sorted(found, key=lambda ext: (ext.name.lower(), ext.ext_id))


# --- Chromium --------------------------------------------------------------
def _chromium(profile_dir: Path) -> list[Extension]:
    root = profile_dir / "Extensions"
    if not root.is_dir():
        return []
    extensions: list[Extension] = []
    for entry in _iterdir(root):
        if not entry.is_dir():
            continue
        version_dir = _newest_version_dir(entry)
        if version_dir is None:
            continue
        manifest = _read_json(version_dir / "manifest.json")
        if manifest is None:
            continue
        name = _chromium_name(manifest, version_dir) or entry.name
        version = str(manifest.get("version", "") or "")
        data_bytes = _tree_size(profile_dir / "Local Extension Settings" / entry.name)
        data_bytes += _tree_size(
            profile_dir / "IndexedDB" / f"chrome-extension_{entry.name}_0.indexeddb.leveldb"
        )
        extensions.append(
            Extension(entry.name, name, version, has_data=data_bytes > 0, data_bytes=data_bytes)
        )
    return extensions


def _newest_version_dir(extension_dir: Path) -> Path | None:
    """Chromium keeps ``<id>/<version>_<n>/``, sometimes more than one.

    An update leaves the old version behind until the browser cleans it up, so
    the newest is the one that describes what is actually installed.
    """
    candidates = [child for child in _iterdir(extension_dir) if child.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda child: _version_key(child.name))


def _version_key(name: str) -> tuple:
    """Sort ``1.60.0_0`` after ``1.9.0_0`` -- numerically, not lexically."""
    base = name.split("_", 1)[0]
    parts = []
    for chunk in base.split("."):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return (tuple(parts), name)


def _chromium_name(manifest: dict, version_dir: Path) -> str:
    """The extension's display name, resolving a ``__MSG_...__`` placeholder.

    Most Store extensions localise their name, so ``manifest.json`` holds
    ``__MSG_extName__`` and the real string is in
    ``_locales/<default_locale>/messages.json``. Without this the inventory is a
    list of ``__MSG_appName__`` and tells the user nothing.
    """
    raw = str(manifest.get("name", "") or "")
    if not (raw.startswith("__MSG_") and raw.endswith("__")):
        return raw
    key = raw[len("__MSG_") : -2]
    locale = str(manifest.get("default_locale", "") or "en")
    for candidate in (locale, "en", "en_US"):
        messages = _read_json(version_dir / "_locales" / candidate / "messages.json")
        if not isinstance(messages, dict):
            continue
        # Message keys are matched case-insensitively by Chrome.
        for message_key, value in messages.items():
            if message_key.lower() != key.lower() or not isinstance(value, dict):
                continue
            text = value.get("message")
            if isinstance(text, str) and text.strip():
                return text.strip()
    return raw


# --- Firefox ---------------------------------------------------------------
def _firefox(profile_dir: Path) -> list[Extension]:
    data = _read_json(profile_dir / "extensions.json")
    if not isinstance(data, dict):
        return []
    extensions: list[Extension] = []
    for addon in data.get("addons", []):
        if not isinstance(addon, dict):
            continue
        if addon.get("type") not in (None, "extension"):
            continue  # themes, dictionaries, langpacks
        if str(addon.get("location", "")) in FIREFOX_BUILTIN_LOCATIONS:
            continue  # shipped with Firefox, not something the user chose
        ext_id = str(addon.get("id", "") or "")
        if not ext_id:
            continue
        locale = addon.get("defaultLocale")
        name = ""
        if isinstance(locale, dict):
            name = str(locale.get("name", "") or "")
        data_bytes = _tree_size(profile_dir / "browser-extension-data" / ext_id)
        extensions.append(
            Extension(
                ext_id,
                name or ext_id,
                str(addon.get("version", "") or ""),
                has_data=data_bytes > 0,
                data_bytes=data_bytes,
            )
        )
    return extensions


# --- helpers ---------------------------------------------------------------
def _iterdir(path: Path) -> list[Path]:
    try:
        return sorted(path.iterdir(), key=lambda child: child.name)
    except OSError:
        return []


def _read_json(path: Path):
    """Parse a JSON config file, or None. Never raises on bad input."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    # Chromium writes some files with a UTF-8 BOM.
    try:
        return json.loads(text.lstrip("﻿"))
    except (ValueError, RecursionError):
        log.debug("could not parse %s", path)
        return None


def _tree_size(path: Path) -> int:
    """Bytes under ``path``, or 0 when it is not there or cannot be read."""
    if not path.is_dir():
        return 0
    total = 0
    stack = [path]
    while stack:
        for entry in _iterdir(stack.pop()):
            try:
                if entry.is_dir():
                    stack.append(entry)
                else:
                    total += entry.stat().st_size
            except OSError:
                continue
    return total
