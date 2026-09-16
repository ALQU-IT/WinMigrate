"""The settings that make a machine feel like the one it replaced.

Everything else WinMigrate carries is *content*: files, profiles, software. This
module carries the opposite -- the small registry values behind how Windows
looks, sounds and responds. None of them is worth anything on its own. Together
they are the difference between logging into your own computer and logging into
a new one.

They matter most to the person least able to put them back. Someone who has
spent an afternoon making the text large enough to read, the pointer big enough
to find and the double-click slow enough to land does not experience losing
that as "a setting did not migrate"; they experience it as the new computer
being unusable, with no idea which of a hundred screens fixed it last time.
That is the whole reason this module exists, and it is why accessibility is
first in the table rather than somewhere after the accent colour.

**Value names, not keys.** Every entry lists the values it carries by name. A
registry key is a shared drawer: ``Control Panel\\Desktop`` holds the wallpaper,
the screen saver, and a dozen things that describe the graphics hardware this
profile last ran on. Carrying a whole key means carrying whatever Microsoft
adds to it next, onto hardware it was never read from. The three keys that do
travel whole say so, and say why.

Nothing here is secret: sizes, speeds, colours and layout codes. There are no
paths to another person's files and no credentials -- the one value that names
a file, the screen saver, is checked to be a screen saver that exists before it
is written anywhere.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

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

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Setting:
    """One registry key's worth of personalisation, and why it is carried.

    ``values`` names what travels. ``whole_key`` is the exception, for a key
    whose value *names* are themselves the data -- a keyboard layout list is
    "1", "2", "3" -- and it is never used for a key that Windows also writes
    machine-specific state into.
    """

    slot: str
    key: str
    title: str
    why: str
    values: tuple[str, ...] = ()
    whole_key: bool = False
    hive: str = HKCU

    def wanted(self, name: str) -> bool:
        return self.whole_key or name in self.values


#: What travels, in the order it matters to somebody sitting down at a new
#: machine. Accessibility first: it is the one that decides whether the rest of
#: the screen can be read at all.
SETTINGS: tuple[Setting, ...] = (
    Setting(
        slot="accessibility",
        key=r"Control Panel\Accessibility",
        title="Ease of Access",
        why="Sticky keys, filter keys and the other typing and hearing aids.",
        whole_key=True,
    ),
    Setting(
        slot="accessibility_keyboard",
        key=r"Control Panel\Accessibility\StickyKeys",
        title="Sticky Keys",
        why="Whether one finger can hold Shift.",
        whole_key=True,
    ),
    Setting(
        slot="accessibility_mouse_keys",
        key=r"Control Panel\Accessibility\MouseKeys",
        title="Mouse Keys",
        why="Moving the pointer from the number pad.",
        whole_key=True,
    ),
    Setting(
        slot="accessibility_high_contrast",
        key=r"Control Panel\Accessibility\HighContrast",
        title="High Contrast",
        why="The high-contrast theme, and whether the shortcut turns it on.",
        whole_key=True,
    ),
    Setting(
        slot="text_scale",
        key=r"Software\Microsoft\Accessibility",
        title="Text size",
        why="How large Windows draws text everywhere. Set once, missed instantly.",
        values=("TextScaleFactor",),
    ),
    Setting(
        slot="magnifier",
        key=r"Software\Microsoft\ScreenMagnifier",
        title="Magnifier",
        why="Whether the magnifier starts with Windows, and how far it zooms.",
        values=("Magnification", "FollowFocus", "FollowCaret", "FollowMouse",
                "Invert", "RunningState", "UseBitmapSmoothing", "ZoomIncrement"),
    ),
    Setting(
        slot="cursor_size",
        key=r"Control Panel\Cursors",
        title="Mouse pointer",
        why="The pointer scheme, including the large and coloured pointers.",
        values=("Scheme Source", "CursorBaseSize"),
    ),
    Setting(
        slot="keyboard_layout",
        key=r"Keyboard Layout\Preload",
        title="Keyboard layout",
        why=(
            "Which keyboard this is. A Swiss keyboard that comes back as a US one "
            "means the letters are in the wrong places, in every program, including "
            "the box asking for a password."
        ),
        whole_key=True,
    ),
    Setting(
        slot="keyboard_substitutes",
        key=r"Keyboard Layout\Substitutes",
        title="Keyboard layout substitutions",
        why="The custom layouts that go with the list above.",
        whole_key=True,
    ),
    Setting(
        slot="region",
        key=r"Control Panel\International",
        title="Region and formats",
        why="How dates, times, numbers and currency are written.",
        values=(
            "Locale", "LocaleName", "sCountry", "sLanguage",
            "sShortDate", "sLongDate", "sTimeFormat", "sShortTime",
            "sDecimal", "sThousand", "sCurrency", "iFirstDayOfWeek",
            "iMeasure", "sList",
        ),
    ),
    Setting(
        slot="theme",
        key=r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        title="Light or dark",
        why="Whether Windows and its apps are light or dark, and colour on the taskbar.",
        values=("AppsUseLightTheme", "SystemUsesLightTheme", "ColorPrevalence",
                "EnableTransparency"),
    ),
    Setting(
        slot="accent_colour",
        key=r"Software\Microsoft\Windows\DWM",
        title="Accent colour",
        why="The colour on window borders, the taskbar and the Start menu.",
        values=("AccentColor", "ColorizationColor", "ColorizationAfterglow",
                "AccentColorInactive", "ColorPrevalence", "EnableWindowColorization"),
    ),
    Setting(
        slot="mouse",
        key=r"Control Panel\Mouse",
        title="Mouse",
        why=(
            "Pointer speed, double-click speed, and whether the buttons are swapped. "
            "A slow double-click is usually deliberate, and a machine without it is "
            "one where nothing opens."
        ),
        values=("MouseSpeed", "MouseThreshold1", "MouseThreshold2", "MouseSensitivity",
                "DoubleClickSpeed", "DoubleClickHeight", "DoubleClickWidth",
                "SwapMouseButtons", "MouseHoverTime", "MouseTrails"),
    ),
    Setting(
        slot="keyboard_speed",
        key=r"Control Panel\Keyboard",
        title="Keyboard repeat",
        why="How long a held key waits, and how fast it then repeats.",
        values=("KeyboardDelay", "KeyboardSpeed", "InitialKeyboardIndicators"),
    ),
    Setting(
        slot="sounds",
        key=r"AppEvents\Schemes",
        title="Sound scheme",
        why="Which set of sounds Windows uses.",
        values=("",),  # the default value: the scheme's name
    ),
    Setting(
        slot="screensaver",
        key=r"Control Panel\Desktop",
        title="Screen saver",
        why="Which screen saver, after how long, and whether it asks for the password.",
        values=("SCRNSAVE.EXE", "ScreenSaveActive", "ScreenSaveTimeOut",
                "ScreenSaverIsSecure"),
    ),
    Setting(
        slot="desktop_icons",
        key=r"Software\Microsoft\Windows\CurrentVersion\Explorer\HideDesktopIcons\NewStartPanel",
        title="Desktop icons",
        why="Whether This PC, the Recycle Bin and your own folder are on the desktop.",
        whole_key=True,
    ),
    Setting(
        slot="explorer",
        key=r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced",
        title="File Explorer",
        why=(
            "Hidden files, file extensions, what Explorer opens to, and which "
            "buttons are on the taskbar."
        ),
        values=("Hidden", "HideFileExt", "LaunchTo", "ShowTaskViewButton",
                "TaskbarAl", "TaskbarDa", "TaskbarMn", "NavPaneShowAllFolders",
                "SeparateProcess", "ShowSuperHidden", "DontPrettyPath"),
    ),
)


def scan_personalization(env: Environment):
    """Return the personalisation item, or nothing if this profile has none."""
    captured: dict[str, dict[str, Any]] = {}
    for setting in SETTINGS:
        values = read_setting(env, setting)
        if values:
            captured[setting.slot] = values

    if not captured:
        return [], []

    counted = sum(len(values) for values in captured.values())
    return [
        Item(
            id="settings:personalization",
            category=Category.PERSONALIZATION,
            kind=Kind.RECORD,
            title=f"How Windows looks and responds ({counted} setting(s))",
            record={"settings": captured},
            # Not credential material, and the restore reads it to say what it
            # will re-apply before anything is decrypted.
            record_public=True,
            restore=RestoreSpec(
                target="HKCU",
                strategy=RestoreStrategy.MERGE,
                notes=["Re-applied on the new machine; some need a sign-out to show."],
            ),
            notes=[
                Note(
                    Severity.INFO,
                    "Ease of Access, keyboard layout, text size, colours, mouse and "
                    "sounds -- the settings a new machine otherwise loses silently.",
                )
            ],
        )
    ], []


def read_setting(env: Environment, setting: Setting) -> dict[str, Any]:
    """The values this setting carries, as they are on this machine.

    Values Windows has not written are simply absent, and absent is not the
    same as zero: writing a default into a key the new machine has never had
    one in is how a tool "restores" a setting nobody ever set.
    """
    present = env.read_registry_key(setting.hive, setting.key)
    if not present:
        return {}
    return {
        name: value
        for name, value in present.items()
        if setting.wanted(name) and isinstance(value, (str, int))
    }
