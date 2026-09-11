"""Monitor layouts, one per set of screens the machine has met.

Windows remembers how you arranged your screens, and it remembers it *per
combination of screens*: dock at the desk and the three monitors come back
where you left them; undock and the laptop panel takes over; dock at a
different desk and that arrangement returns instead. It does this by keying
each layout on the identities of the monitors attached at the time, read from
their EDID, so the same dock and the same screens produce the same key on a
different computer.

That last part is what makes this worth carrying. Someone whose laptop is being
replaced will plug the new one into the same dock, and the arrangement they
spent time getting right -- which screen is left of which, which is primary,
what scaling each one uses -- is not in any folder and is not in any account.
It is in the registry of the machine about to be wiped.

**Nothing here is restored automatically, and that is deliberate.** The layouts
live under ``GraphicsDrivers`` alongside adapter LUIDs and source/target ids
that belong to the graphics hardware that wrote them. Replaying those onto a
different GPU is how a machine ends up booting to a screen that never lights
up, and a backup tool that can leave someone with no display has failed at
something more important than remembering where their monitors were. So the
arrangement is read, decoded into something a person can act on, and handed
over as instructions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from ..models import (
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    Severity,
)
from ..platform_win import HKCU, HKLM, Environment

log = logging.getLogger(__name__)

#: Where Windows keeps the per-monitor-set arrangements.
CONFIGURATION_KEY = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers\Configuration"

#: Which monitors were attached for each of those sets.
CONNECTIVITY_KEY = r"SYSTEM\CurrentControlSet\Control\GraphicsDrivers\Connectivity"

#: Per-monitor scaling the user chose, which is per-user rather than per-machine.
PER_MONITOR_SETTINGS_KEY = r"Control Panel\Desktop\PerMonitorSettings"

#: Orientation values as Windows stores them.
ORIENTATIONS = {0: "landscape", 1: "portrait", 2: "landscape (flipped)", 3: "portrait (flipped)"}

#: A machine accumulates one of these for every combination of screens it has
#: ever seen, including one-offs from a meeting room projector. Reporting all of
#: them would bury the two or three that are someone's actual desks.
MAX_LAYOUTS = 12


@dataclass(slots=True)
class Screen:
    """One monitor within a layout."""

    monitor_id: str
    width: int = 0
    height: int = 0
    position_x: int = 0
    position_y: int = 0
    orientation: str = ""
    refresh_hz: float = 0.0
    scaling_percent: int = 0

    @property
    def primary(self) -> bool:
        """Windows puts the primary screen at the origin."""
        return self.position_x == 0 and self.position_y == 0

    def describe(self) -> str:
        parts = [monitor_name(self.monitor_id)]
        if self.width and self.height:
            parts.append(f"{self.width}×{self.height}")
        if self.refresh_hz:
            parts.append(f"{self.refresh_hz:g} Hz")
        if self.scaling_percent:
            parts.append(f"{self.scaling_percent}% scaling")
        if self.orientation and self.orientation != "landscape":
            parts.append(self.orientation)
        parts.append("primary" if self.primary else f"at ({self.position_x}, {self.position_y})")
        return ", ".join(parts)

    def to_json(self) -> dict[str, Any]:
        return {
            "monitor_id": self.monitor_id,
            "name": monitor_name(self.monitor_id),
            "width": self.width,
            "height": self.height,
            "position_x": self.position_x,
            "position_y": self.position_y,
            "orientation": self.orientation,
            "refresh_hz": self.refresh_hz,
            "scaling_percent": self.scaling_percent,
            "primary": self.primary,
        }


@dataclass(slots=True)
class Layout:
    """One remembered arrangement: the screens, and the key Windows filed it under."""

    key: str
    screens: list[Screen] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {"key": self.key, "screens": [screen.to_json() for screen in self.screens]}


def monitor_name(monitor_id: str) -> str:
    """A readable name for a monitor id such as ``DELA1CF`` or ``MONITOR\\DELA1CF\\...``.

    The first three letters are the manufacturer's PNP id, which is the only
    part that means anything without a lookup table, so it is expanded where it
    is recognised and left alone where it is not. Inventing a friendly name for
    an unknown panel would be worse than showing the id someone can match
    against the label on the back.
    """
    parts = [part for part in monitor_id.strip().split("\\") if part]
    if not parts:
        return monitor_id
    # In a device path like MONITOR\DELA1CF\{4d36e96e...}\0004 the useful part
    # is the hardware id in the middle, not the instance number on the end.
    token = next((part for part in parts if _looks_like_pnp_id(part)), parts[-1])
    vendor = PNP_VENDORS.get(token[:3].upper())
    return f"{vendor} {token}" if vendor else token


def _looks_like_pnp_id(text: str) -> bool:
    """Three letters of manufacturer followed by the model's hex code."""
    return (
        len(text) >= 6
        and text[:3].isalpha()
        and all(character in "0123456789abcdefABCDEF" for character in text[3:7])
    )


#: The manufacturers likely to be on a desk. Not exhaustive by design -- an
#: unrecognised id is shown as-is rather than guessed at.
PNP_VENDORS = {
    "AAC": "AcerView", "ACI": "Asus", "ACR": "Acer", "AOC": "AOC", "APP": "Apple",
    "AUO": "AU Optronics", "BNQ": "BenQ", "BOE": "BOE", "CMN": "Chi Mei",
    "DEL": "Dell", "ENC": "Eizo", "FUS": "Fujitsu", "GSM": "LG", "HPN": "HP",
    "HWP": "HP", "IVM": "Iiyama", "LEN": "Lenovo", "LGD": "LG Display",
    "MSI": "MSI", "NEC": "NEC", "PHL": "Philips", "SAM": "Samsung",
    "SDC": "Samsung Display", "SEC": "Seiko Epson", "SHP": "Sharp",
    "SNY": "Sony", "VSC": "ViewSonic",
}


def scan_displays(env: Environment):
    """Return the monitor-layout item and its follow-up, if there is one."""
    layouts = read_layouts(env)
    if not layouts:
        return [], []

    scaling = read_per_monitor_scaling(env)
    for layout in layouts:
        for screen in layout.screens:
            if not screen.scaling_percent:
                screen.scaling_percent = scaling.get(screen.monitor_id, 0)

    item = Item(
        id="settings:display_layouts",
        category=Category.DISPLAYS,
        kind=Kind.RECORD,
        title=f"Monitor arrangements ({len(layouts)})",
        record={"layouts": [layout.to_json() for layout in layouts]},
        # Monitor ids carry serial numbers off the panel's EDID. Harmless, but
        # there is no reason to put them in the one file that sits unencrypted
        # beside the bundle.
        record_public=False,
        restore=RestoreSpec(
            target="Settings → System → Display",
            strategy=RestoreStrategy.GUIDED,
            notes=[
                "Re-created by hand after connecting the same monitors, not "
                "applied automatically.",
            ],
        ),
    )
    item.notes.append(
        Note(
            Severity.INFO,
            f"{len(layouts)} arrangement(s) Windows remembers, one per set of screens "
            "it has seen.",
            "Recorded, not restored: the stored form is tied to this machine's "
            "graphics adapter, and replaying it onto different hardware is how a "
            "computer ends up with a display that never lights up.",
        )
    )
    return [item], [_followup(layouts)]


def read_layouts(env: Environment) -> list[Layout]:
    """Decode the remembered arrangements, newest first."""
    layouts: list[Layout] = []
    for set_key in env.registry_subkeys(HKLM, CONFIGURATION_KEY):
        screens = _screens_for(env, set_key)
        if screens:
            layouts.append(Layout(key=set_key, screens=screens))
    # Most screens first: a docked three-monitor desk is more interesting than
    # the one-off a projector left behind.
    layouts.sort(key=lambda layout: len(layout.screens), reverse=True)
    if len(layouts) > MAX_LAYOUTS:
        log.info("%d monitor layouts found; reporting the %d largest", len(layouts), MAX_LAYOUTS)
        layouts = layouts[:MAX_LAYOUTS]
    return layouts


def _screens_for(env: Environment, set_key: str) -> list[Screen]:
    """Every screen in one remembered set.

    The shape under a set key has changed between Windows versions -- sometimes
    the screens are numbered subkeys, sometimes they sit under a timestamp
    first. Rather than encode one version's layout, this looks one level down
    for anything that carries the values a screen has.
    """
    base = f"{CONFIGURATION_KEY}\\{set_key}"
    screens: list[Screen] = []
    for child in env.registry_subkeys(HKLM, base):
        values = env.read_registry_key(HKLM, f"{base}\\{child}") or {}
        screen = _screen_from(values, child)
        if screen is not None:
            screens.append(screen)
            continue
        # Not a screen itself; look inside it.
        for grandchild in env.registry_subkeys(HKLM, f"{base}\\{child}"):
            nested = env.read_registry_key(HKLM, f"{base}\\{child}\\{grandchild}") or {}
            screen = _screen_from(nested, grandchild)
            if screen is not None:
                screens.append(screen)
    return screens


def _screen_from(values: dict[str, Any], fallback_id: str) -> Screen | None:
    """A Screen from one registry key's values, or None if it is not one."""
    width = _as_int(values.get("PrimSurfSize.cx")) or _as_int(values.get("ActiveSize.cx"))
    height = _as_int(values.get("PrimSurfSize.cy")) or _as_int(values.get("ActiveSize.cy"))
    if not width or not height:
        return None
    numerator = _as_int(values.get("RefreshRate.Numerator"))
    denominator = _as_int(values.get("RefreshRate.Denominator")) or 1
    return Screen(
        monitor_id=str(values.get("MonitorID") or fallback_id),
        width=width,
        height=height,
        position_x=_as_int(values.get("Position.cx")),
        position_y=_as_int(values.get("Position.cy")),
        orientation=ORIENTATIONS.get(_as_int(values.get("Orientation")), ""),
        refresh_hz=round(numerator / denominator, 3) if numerator else 0.0,
    )


def read_per_monitor_scaling(env: Environment) -> dict[str, int]:
    """The scaling the user chose per monitor, which is theirs rather than the
    machine's and so is the part most worth carrying."""
    scaling: dict[str, int] = {}
    for monitor in env.registry_subkeys(HKCU, PER_MONITOR_SETTINGS_KEY):
        values = env.read_registry_key(HKCU, f"{PER_MONITOR_SETTINGS_KEY}\\{monitor}") or {}
        raw = values.get("DpiValue")
        if isinstance(raw, int):
            scaling[monitor] = _dpi_step_to_percent(raw)
    return scaling


#: DpiValue is an offset from the display's own recommended scaling, not an
#: absolute: 0 means "whatever Windows recommends for this panel", and each step
#: moves one notch along the scale the Settings app offers.
DPI_STEPS = (100, 125, 150, 175, 200, 225, 250, 300, 350, 400, 450, 500)


def _dpi_step_to_percent(value: int) -> int:
    """Turn a DpiValue offset into a percentage, as best it can be turned.

    Without the panel's recommended scaling the offset cannot be resolved
    exactly, so 0 -- by far the most common value -- is reported as the
    recommended setting rather than guessed at.
    """
    if value == 0:
        return 0
    index = max(0, min(len(DPI_STEPS) - 1, value))
    return DPI_STEPS[index]


def _as_int(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return 0


def _followup(layouts: list[Layout]) -> Followup:
    steps = [
        "Connect the new machine to the same dock and monitors, then let Windows "
        "detect them.",
        "Open Settings → System → Display and drag the screens into the "
        "arrangement below, setting the primary one first.",
    ]
    for number, layout in enumerate(layouts, start=1):
        steps.append(f"  Arrangement {number} — {len(layout.screens)} screen(s):")
        for screen in layout.screens:
            steps.append(f"      {screen.describe()}")
    steps.append(
        "Windows will remember each arrangement once you have set it, and bring it "
        "back by itself the next time you connect those screens."
    )
    return Followup(
        id="settings:display_layouts",
        title="Re-create your monitor arrangements",
        why=(
            f"{len(layouts)} arrangement(s) travelled as a record. Windows stores them "
            "against this machine's graphics adapter, so they are written down for you "
            "to re-create rather than applied — restoring them onto different hardware "
            "risks a display that does not come back."
        ),
        steps=steps,
        category=Category.DISPLAYS,
    )
