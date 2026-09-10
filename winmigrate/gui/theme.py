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

#: Windows' own accent blue, near enough. Used for the active rail entry, the
#: heading rule and the default button.
ACCENT = "#0067c0"
ACCENT_DARK = "#005499"

INK = "#1b1b1b"          # body text
INK_SOFT = "#5d5d5d"     # subtitles, secondary lines
INK_FAINT = "#8a8a8a"    # disabled rows, hints

PAGE = "#ffffff"         # the content area
BAND = "#f3f3f3"         # header and footer bands
RULE = "#e0e0e0"         # separators
RAIL = "#fafafa"         # the step rail down the left

SECRET = "#7a3fb8"       # encrypted-only items
WARN = "#9a6700"
GOOD = "#0f7b32"
BAD = "#b42318"

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


def apply(style, family: str) -> None:
    """Configure the ttk styles the window uses.

    ``style`` is a ``ttk.Style``. Only named styles are touched, never the
    defaults, so anything not explicitly styled still looks like the platform.
    """
    try:
        style.theme_use("vista")
    except Exception:  # noqa: BLE001 -- not on Windows, or no such theme
        try:
            style.theme_use("clam")
        except Exception:  # noqa: BLE001
            pass

    style.configure("Page.TFrame", background=PAGE)
    style.configure("Band.TFrame", background=BAND)
    style.configure("Rail.TFrame", background=RAIL)

    style.configure("Title.TLabel", background=PAGE, foreground=INK,
                    font=(family, TITLE_SIZE))
    style.configure("Subtitle.TLabel", background=PAGE, foreground=INK_SOFT,
                    font=(family, SUBTITLE_SIZE))
    style.configure("Body.TLabel", background=PAGE, foreground=INK,
                    font=(family, BODY_SIZE))
    style.configure("Hint.TLabel", background=PAGE, foreground=INK_SOFT,
                    font=(family, SMALL_SIZE))
    style.configure("Warn.TLabel", background=PAGE, foreground=WARN,
                    font=(family, SMALL_SIZE))
    style.configure("Bad.TLabel", background=PAGE, foreground=BAD,
                    font=(family, SMALL_SIZE))
    style.configure("Good.TLabel", background=PAGE, foreground=GOOD,
                    font=(family, BODY_SIZE))
    style.configure("BandHint.TLabel", background=BAND, foreground=INK_SOFT,
                    font=(family, SMALL_SIZE))

    style.configure("RailOn.TLabel", background=RAIL, foreground=ACCENT,
                    font=(family, BODY_SIZE, "bold"))
    style.configure("RailDone.TLabel", background=RAIL, foreground=INK_SOFT,
                    font=(family, BODY_SIZE))
    style.configure("RailOff.TLabel", background=RAIL, foreground=INK_FAINT,
                    font=(family, BODY_SIZE))
    style.configure("RailTitle.TLabel", background=RAIL, foreground=INK,
                    font=(family, BODY_SIZE, "bold"))

    style.configure("Wizard.TButton", font=(family, BODY_SIZE), padding=(18, 7))
    style.configure("Wizard.TCheckbutton", background=PAGE, font=(family, BODY_SIZE))
    style.configure("Wizard.TRadiobutton", background=PAGE, font=(family, BODY_SIZE + 1))
    style.configure("Band.TCheckbutton", background=BAND, font=(family, SMALL_SIZE))
    style.configure("Wizard.Treeview", font=(family, BODY_SIZE), rowheight=26)
    style.configure("Wizard.Treeview.Heading", font=(family, SMALL_SIZE, "bold"))


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
