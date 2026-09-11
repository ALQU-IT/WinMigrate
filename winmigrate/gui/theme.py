"""The look: a setup wizard, not a control panel.

Deliberately restrained. This is a tool someone runs once, on a machine they are
about to replace, usually while slightly anxious about losing something. It
should look like the installers they have used a hundred times -- a white page
with a heading, a quiet band of buttons at the bottom -- because that is a shape
they already know how to read.

The palette is defined here rather than sprinkled through the widgets so the
whole window can be adjusted in one place, and so the parts that are not tkinter
can be checked without a display.
"""

from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True, slots=True)
class Palette:
    """Every colour the window uses, so light and dark are the same shape.

    A dataclass rather than module constants because there are two of them and
    they have to stay in step: a colour added to one and forgotten in the other
    is a widget that renders black on black, which is the sort of thing nobody
    notices until it is in front of a user.
    """

    accent: str
    ink: str          # body text
    ink_soft: str     # subtitles, secondary lines
    ink_faint: str    # disabled rows, hints
    page: str         # the content area
    band: str         # header and footer bands
    rule: str         # separators
    rail: str         # the step rail down the left
    secret: str       # encrypted-only items
    warn: str
    good: str
    bad: str


LIGHT = Palette(
    accent="#0067c0",
    ink="#1b1b1b",
    ink_soft="#5d5d5d",
    ink_faint="#8a8a8a",
    page="#ffffff",
    band="#f3f3f3",
    rule="#e0e0e0",
    rail="#fafafa",
    secret="#7a3fb8",
    warn="#9a6700",
    good="#0f7b32",
    bad="#b42318",
)

#: Windows' own dark surfaces are near-black rather than mid-grey, and its
#: accent lightens rather than darkens -- a dark theme that simply inverts the
#: light one looks like a different operating system sitting on the desktop.
DARK = Palette(
    accent="#4cc2ff",
    ink="#f0f0f0",
    ink_soft="#b8b8b8",
    ink_faint="#7c7c7c",
    page="#202020",
    band="#272727",
    rule="#3d3d3d",
    rail="#1a1a1a",
    secret="#c9a0ff",
    warn="#e8b339",
    good="#4ad07a",
    bad="#ff6b5e",
)

#: Where Windows records the choice. 0 is dark, 1 is light -- the value is named
#: for the light theme, so the sense reads backwards.
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
APPS_USE_LIGHT_THEME = "AppsUseLightTheme"


def detect_dark_mode(env=None) -> bool:
    """Is Windows set to a dark theme for applications?

    ``AppsUseLightTheme`` is the one that governs app windows;
    ``SystemUsesLightTheme`` is the taskbar and Start menu and can differ, which
    is why it is not the one read here.

    Anything unreadable -- the value absent, a non-Windows host, an older build
    that predates the setting -- means light, because that is what Windows
    itself falls back to when the value is missing.
    """
    if env is None:  # pragma: no cover -- the live path
        from ..platform_win import Environment  # noqa: PLC0415

        env = Environment.live()
    from ..platform_win import HKCU  # noqa: PLC0415

    value = env.read_registry_value(HKCU, PERSONALIZE_KEY, APPS_USE_LIGHT_THEME)
    if isinstance(value, int):
        return value == 0
    return False


def palette_for(dark: bool) -> Palette:
    return DARK if dark else LIGHT


#: Segoe UI is on every supported Windows; the fallbacks are for a development
#: host that has neither.
FAMILY = ("Segoe UI", "Helvetica Neue", "DejaVu Sans", "TkDefaultFont")

TITLE_SIZE = 15
SUBTITLE_SIZE = 9
BODY_SIZE = 9
SMALL_SIZE = 8

WINDOW_WIDTH = 940
WINDOW_HEIGHT = 660
RAIL_WIDTH = 190
PAD = 22


def font_family(probe=None) -> str:
    """The first font in :data:`FAMILY` this machine has.

    ``probe`` is ``tkinter.font.families``; passing it in keeps this callable
    from a test with no display.
    """
    if probe is None:  # pragma: no cover -- needs a live Tk
        from tkinter import font as tkfont

        probe = tkfont.families
    try:
        available = {name.lower() for name in probe()}
    except Exception:  # noqa: BLE001 -- a font choice must never stop the window
        return FAMILY[-1]
    for candidate in FAMILY:
        if candidate.lower() in available:
            return candidate
    return FAMILY[-1]


def apply(style, family: str, palette: Palette = LIGHT) -> None:
    """Configure the ttk styles the window uses, in one palette or the other.

    ``style`` is a ``ttk.Style``. Only named styles are touched, never the
    defaults, so anything not explicitly styled still looks like the platform.

    On a dark palette the underlying ttk theme is switched away from "vista":
    vista draws its widgets from Windows' own light bitmaps, which cannot be
    recoloured, so a dark page would end up framed in white chrome. "clam" is
    drawn from the colours it is given and so can actually go dark.
    """
    wanted = ("clam",) if palette is DARK else ("vista", "clam")
    for name in wanted:
        try:
            style.theme_use(name)
            break
        except Exception:  # noqa: BLE001 -- not on Windows, or no such theme
            continue

    style.configure("Page.TFrame", background=palette.page)
    style.configure("Band.TFrame", background=palette.band)
    style.configure("Rail.TFrame", background=palette.rail)

    style.configure("Title.TLabel", background=palette.page, foreground=palette.ink,
                    font=(family, TITLE_SIZE))
    style.configure("Subtitle.TLabel", background=palette.page, foreground=palette.ink_soft,
                    font=(family, SUBTITLE_SIZE))
    style.configure("Body.TLabel", background=palette.page, foreground=palette.ink,
                    font=(family, BODY_SIZE))
    style.configure("Hint.TLabel", background=palette.page, foreground=palette.ink_soft,
                    font=(family, SMALL_SIZE))
    style.configure("Warn.TLabel", background=palette.page, foreground=palette.warn,
                    font=(family, SMALL_SIZE))
    style.configure("Bad.TLabel", background=palette.page, foreground=palette.bad,
                    font=(family, SMALL_SIZE))
    style.configure("Good.TLabel", background=palette.page, foreground=palette.good,
                    font=(family, BODY_SIZE))
    style.configure("BandHint.TLabel", background=palette.band, foreground=palette.ink_soft,
                    font=(family, SMALL_SIZE))

    style.configure("RailOn.TLabel", background=palette.rail, foreground=palette.accent,
                    font=(family, BODY_SIZE, "bold"))
    style.configure("RailDone.TLabel", background=palette.rail, foreground=palette.ink_soft,
                    font=(family, BODY_SIZE))
    style.configure("RailOff.TLabel", background=palette.rail, foreground=palette.ink_faint,
                    font=(family, BODY_SIZE))
    style.configure("RailTitle.TLabel", background=palette.rail, foreground=palette.ink,
                    font=(family, BODY_SIZE, "bold"))

    style.configure("Wizard.TButton", font=(family, BODY_SIZE), padding=(18, 7))
    style.configure("Wizard.TCheckbutton", background=palette.page, foreground=palette.ink,
                    font=(family, BODY_SIZE))
    style.configure("Wizard.TRadiobutton", background=palette.page, foreground=palette.ink,
                    font=(family, BODY_SIZE + 1))
    style.configure("Band.TCheckbutton", background=palette.band, foreground=palette.ink,
                    font=(family, SMALL_SIZE))
    style.configure(
        "Wizard.Treeview",
        background=palette.page,
        fieldbackground=palette.page,
        foreground=palette.ink,
        font=(family, BODY_SIZE),
        rowheight=26,
    )
    style.configure("Wizard.Treeview.Heading", background=palette.band,
                    foreground=palette.ink, font=(family, SMALL_SIZE, "bold"))
    # Without this the selection keeps clam's default blue, which on the dark
    # page is the one thing louder than the accent.
    style.map(
        "Wizard.Treeview",
        background=[("selected", palette.accent)],
        foreground=[("selected", palette.page)],
    )


#: Rail entries are drawn with one of these, depending on where the user is.
RAIL_ON = "RailOn.TLabel"
RAIL_DONE = "RailDone.TLabel"
RAIL_OFF = "RailOff.TLabel"


def rail_style(index: int, current: int) -> str:
    """Which style a rail entry uses: done, current, or still ahead."""
    if index == current:
        return RAIL_ON
    return RAIL_DONE if index < current else RAIL_OFF


def rail_marker(index: int, current: int) -> str:
    """The glyph beside a rail entry. Ticked once passed, so progress reads at
    a glance without needing colour."""
    if index < current:
        return "✓"
    return "●" if index == current else "○"
