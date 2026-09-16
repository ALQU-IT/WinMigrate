"""The desktop background: the picture, or the fact that there isn't one.

A wallpaper is a small thing that makes a new machine feel like the old one,
and it is the first thing you see. Windows stores it in three places at once,
which is why carrying it across is more than copying one registry value:

* ``HKCU\\Control Panel\\Desktop`` holds ``Wallpaper`` -- but usually not the
  file you chose. When a picture is set through Settings, Windows re-encodes it
  into ``%AppData%\\Microsoft\\Windows\\Themes\\TranscodedWallpaper``, a JPEG
  with no extension, and points that value there. The path you actually picked
  is kept separately, in ``BackgroundHistoryPath0``.
* ``...\\Explorer\\Wallpapers\\BackgroundType`` says what *kind* of background
  is on: a picture, a solid colour, a slideshow, or Windows Spotlight. Without
  it, a machine showing Spotlight looks exactly like a machine showing whatever
  picture Spotlight happened to leave behind in the cache -- and copying that
  picture across would replace a background that changes every day with one
  frozen photograph.
* ``HKCU\\Control Panel\\Colors\\Background`` holds the solid colour, as three
  numbers, for when there is no picture at all.

So the type is read first and decides the rest. Spotlight and a slideshow carry
as a setting with no file; a picture carries the file itself, because the
original may live somewhere the new machine has never heard of -- a second
disk, a folder that is not in the profile -- and a background that restores as
a missing-file icon is worse than none.

Nothing here is secret: a file path, an image, and three numbers that are a
colour.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from ..models import (
    Category,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, Environment
from ..util import paths as pathutil

log = logging.getLogger(__name__)

DESKTOP_KEY = r"Control Panel\Desktop"
COLORS_KEY = r"Control Panel\Colors"
WALLPAPERS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers"

#: Where the image rides in the bundle, and the folder it restores into.
WALLPAPER_DIR = "WinMigrate-Wallpaper"

#: ``BackgroundType`` as Windows writes it.
BACKGROUND_TYPES = {0: "picture", 1: "solid_colour", 2: "slideshow", 3: "spotlight"}

#: ``WallpaperStyle``, for a report a person can read. Windows keeps these as
#: strings even though they are numbers, and restores them the same way.
STYLES = {
    "0": "centred", "1": "tiled", "2": "stretched",
    "6": "fitted", "10": "filled", "22": "spanned",
}

#: What a picture is allowed to be. An entry in the registry is a path, not a
#: promise: it can point at a file that has been deleted, or at something that
#: is not an image at all.
IMAGE_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"})

#: 64 MB. A desktop background is a photograph, not a video; anything larger is
#: not the thing this was written for and is not worth putting in a backup
#: twice over.
MAX_IMAGE_BYTES = 64 * 1024 * 1024


def scan_wallpaper(env: Environment):
    """Return the wallpaper item and any follow-ups. Both may be empty."""
    desktop = env.read_registry_key(HKCU, DESKTOP_KEY) or {}
    wallpapers = env.read_registry_key(HKCU, WALLPAPERS_KEY) or {}
    colors = env.read_registry_key(HKCU, COLORS_KEY) or {}
    if not desktop and not wallpapers:
        return [], []

    kind = background_kind(wallpapers, desktop)
    style = str(desktop.get("WallpaperStyle", "") or "")
    record: dict[str, object] = {
        "type": kind,
        "style": style,
        "style_name": STYLES.get(style, ""),
        "tile": str(desktop.get("TileWallpaper", "") or ""),
    }
    if kind == "solid_colour":
        record["colour"] = str(colors.get("Background", "") or "")

    if kind != "picture":
        return [_setting_only(kind, record)], []

    source = picture_path(wallpapers, desktop, env)
    if source is None:
        record["type"] = "picture"
        return [_setting_only("picture", record, missing=True)], []

    record["file_name"] = source.name
    record["archive_name"] = f"{WALLPAPER_DIR}/{source.name}"
    try:
        size = os.stat(pathutil.extended(source)).st_size
    except OSError:
        size = 0
    return [
        Item(
            id="settings:wallpaper",
            category=Category.WALLPAPER,
            kind=Kind.FILE,
            title=f"Desktop background ({source.name})",
            source_path=str(source),
            archive_path=f"{WALLPAPER_DIR}/{source.name}",
            size_bytes=size,
            record=record,
            record_public=True,
            restore=RestoreSpec(
                target=f"%USERPROFILE%\\\\{WALLPAPER_DIR}\\\\{source.name}",
                strategy=RestoreStrategy.REPLACE,
                notes=["Set as the desktop background on the new machine."],
            ),
        )
    ], []


def _setting_only(kind: str, record: dict, missing: bool = False) -> Item:
    """A background with no file behind it: Spotlight, a colour, a slideshow.

    Also the picture whose file has gone: the *setting* is still worth carrying
    -- style and type -- and saying the image could not be found beats copying
    whatever Windows had left in its cache and calling it the user's choice.
    """
    titles = {
        "spotlight": "Desktop background (Windows Spotlight)",
        "solid_colour": "Desktop background (a solid colour)",
        "slideshow": "Desktop background (a slideshow)",
        "picture": "Desktop background (the picture is missing)",
    }
    notes = []
    if missing:
        notes.append(
            Note(
                Severity.INFO,
                "The picture this background points at is not on this machine any more, "
                "so only the setting travels.",
            )
        )
    if kind == "slideshow":
        notes.append(
            Note(
                Severity.INFO,
                "A slideshow is a folder of pictures Windows cycles. The folder travels "
                "with your files if it is in your profile; the slideshow itself is set "
                "up again on the new machine.",
            )
        )
    return Item(
        id="settings:wallpaper",
        category=Category.WALLPAPER,
        kind=Kind.RECORD,
        title=titles.get(kind, "Desktop background"),
        record=record,
        record_public=True,
        restore=RestoreSpec(
            target="HKCU\\Control Panel\\Desktop",
            strategy=RestoreStrategy.MERGE,
            notes=["Re-applied on the new machine."],
        ),
        notes=notes,
    )


def background_kind(wallpapers: dict, desktop: dict) -> str:
    """What sort of background this is: picture, colour, slideshow, Spotlight.

    ``BackgroundType`` is the answer when it is there. When it is not -- an
    older Windows, or a profile that has never been through Settings -- the
    fallback is the oldest rule there is: a wallpaper path means a picture, and
    no wallpaper path means the colour behind it.
    """
    raw = wallpapers.get("BackgroundType")
    if isinstance(raw, int) and raw in BACKGROUND_TYPES:
        return BACKGROUND_TYPES[raw]
    if isinstance(raw, str) and raw.isdigit() and int(raw) in BACKGROUND_TYPES:
        return BACKGROUND_TYPES[int(raw)]
    return "picture" if str(desktop.get("Wallpaper", "") or "").strip() else "solid_colour"


def picture_path(wallpapers: dict, desktop: dict, env: Environment) -> Path | None:
    """The image file the user chose, not the copy Windows re-encoded.

    ``BackgroundHistoryPath0`` is the most recent thing they picked, and is the
    real file: their own JPEG, in their own folder, with its own name. The
    ``Wallpaper`` value usually points at ``TranscodedWallpaper`` instead -- a
    re-encoded copy, extensionless, in AppData. That copy is the fallback
    rather than the answer, because a backup that restores "TranscodedWallpaper"
    into someone's folder has told them nothing about what the picture is.
    """
    candidates = [
        wallpapers.get("BackgroundHistoryPath0"),
        desktop.get("Wallpaper"),
    ]
    for candidate in candidates:
        if not isinstance(candidate, str) or not candidate.strip():
            continue
        path = Path(pathutil.to_posix(pathutil.expand(candidate.strip(), env.environ)))
        if not usable_image(path):
            continue
        return path
    return None


def usable_image(path: Path) -> bool:
    """Is this a file we can carry and set as a background on the far side?

    A registry value is a path, not a promise. It survives the file being
    deleted, the drive being unplugged, and the picture being replaced by
    something that is not a picture.
    """
    try:
        if not path.is_file():
            return False
        size = os.stat(pathutil.extended(path)).st_size
    except OSError:
        return False
    if size == 0 or size > MAX_IMAGE_BYTES:
        log.info("%s is %s bytes; not carrying it as a background", path, size)
        return False
    # TranscodedWallpaper has no suffix at all and is still a real image, so a
    # missing suffix is allowed; a wrong one is not.
    suffix = path.suffix.lower()
    return suffix in IMAGE_SUFFIXES or not suffix
