"""The look: a setup wizard, not a control panel.

The shape is unchanged and deliberately familiar. This is a tool someone runs
once, on a machine they are about to replace, usually while slightly anxious
about losing something, so it keeps the outline of the installers they have
used a hundred times: a list of steps down the left, a heading, a quiet band of
buttons at the bottom. That is a shape they already know how to read, and it is
not worth being clever with.

What changed is the surfaces. The window used to be a white page in grey
chrome. It is now a soft field of colour with frosted panels floating on it --
the same layout, drawn as glass. The arithmetic behind that lives in
:mod:`winmigrate.gui.glass`, because tkinter has no compositor and every blend
has to be worked out in advance; this module is the palette it works from.

Everything here is data and colour maths, kept out of the widgets, so the whole
window can be adjusted in one place and the parts that are not tkinter can be
checked without a display.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from . import glass

log = logging.getLogger(__name__)

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
    #: The wash painted behind everything: a vertical fade from the first to
    #: the second, with two soft colour blooms over it. It is what the frosted
    #: panels are frosted *over*, and without it they have nothing to be
    #: translucent against and read as plain grey boxes.
    wash_top: str = "#ffffff"
    wash_bottom: str = "#ffffff"
    tint_a: str = "#ffffff"
    tint_b: str = "#ffffff"
    #: Text on an accent-filled button. Not the page colour: on the dark theme
    #: the accent is light, so its label has to go dark, and the page is not.
    accent_ink: str = "#ffffff"
    #: Carried on the palette rather than inferred from object identity. An
    #: ``is`` check against the module's own instance stops being true the
    #: moment the module is imported twice -- a frozen build, a reload, a test
    #: that clears sys.modules -- and it does not raise, it just hands a dark
    #: page the light widget theme and leaves it framed in white.
    dark: bool = False


LIGHT = Palette(
    accent="#0f6cbd",
    ink="#16161a",
    ink_soft="#5a5a66",
    ink_faint="#787886",
    page="#ffffff",
    band="#f4f5f9",
    rule="#e3e4ec",
    rail="#fbfbfe",
    secret="#7a3fb8",
    warn="#9a6700",
    good="#0f7b32",
    bad="#b42318",
    wash_top="#d2dbf0",
    wash_bottom="#d2dbf0",
    tint_a="#5b8ffb",
    tint_b="#b077f2",
    accent_ink="#ffffff",
    dark=False,
)

#: Windows' own dark surfaces are near-black rather than mid-grey, and its
#: accent lightens rather than darkens -- a dark theme that simply inverts the
#: light one looks like a different operating system sitting on the desktop.
DARK = Palette(
    accent="#4cc2ff",
    ink="#f2f2f5",
    ink_soft="#b4b4c0",
    ink_faint="#8a8a99",
    page="#202024",
    band="#27272d",
    rule="#3a3a44",
    rail="#1a1a1e",
    secret="#c9a0ff",
    warn="#e8b339",
    good="#4ad07a",
    bad="#ff6b5e",
    wash_top="#0c0c12",
    wash_bottom="#0c0c12",
    tint_a="#3670bd",
    tint_b="#7349c2",
    # The dark accent is a pale blue, so white text on it is unreadable. This
    # is the one place the dark theme's foreground has to go darker, not
    # lighter, and getting it wrong makes the main button illegible.
    accent_ink="#06263a",
    dark=True,
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

TITLE_SIZE = 19
SUBTITLE_SIZE = 10
BODY_SIZE = 10
SMALL_SIZE = 9

WINDOW_WIDTH = 1000
WINDOW_HEIGHT = 700
RAIL_WIDTH = 208
PAD = 26

#: How round the frosted panels are. Windows 11 rounds its own surfaces at 8
#: and its windows at 8; going much past that stops reading as a panel and
#: starts reading as a lozenge.
CARD_RADIUS = 10
#: The gap between panels, which is where the wash shows through. Too small and
#: the panels merge into one sheet and the layering disappears.
CARD_GAP = 16


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

    "clam" underneath, in both themes. It used to be "vista" for the light one,
    on the reasoning that a widget drawn by Windows looks more at home than a
    widget drawn by ttk -- which was true while this window was a white page
    with a heading on it.

    It stopped being true once the window started painting its own surfaces.
    vista draws from Windows' own bitmaps: they cannot be recoloured, they
    ignore every background given to them, and an accent-filled button under
    vista is simply a grey button. So the light theme would have kept native
    chrome sitting on frosted panels it did not match, and lost the one control
    that tells somebody which button carries on. clam takes the colours it is
    handed, so both themes now look like the same program.
    """
    for name in ("clam", "default"):
        try:
            style.theme_use(name)
            break
        except Exception:  # noqa: BLE001 -- no such theme on this build
            continue

    # The frames that sit inside the frosted panels take the panel's own
    # colour, so the frame is invisible against it and the card reads as one
    # surface. palette.page/band/rail stay as the flat fallback for anything
    # not on the canvas.
    surfaces = surfaces_for(palette)
    style.configure("Page.TFrame", background=surfaces.page)
    style.configure("Band.TFrame", background=surfaces.band)
    style.configure("Rail.TFrame", background=surfaces.rail)

    style.configure("Title.TLabel", background=surfaces.page, foreground=palette.ink,
                    font=(family, TITLE_SIZE))
    style.configure("Subtitle.TLabel", background=surfaces.page, foreground=palette.ink_soft,
                    font=(family, SUBTITLE_SIZE))
    style.configure("Body.TLabel", background=surfaces.page, foreground=palette.ink,
                    font=(family, BODY_SIZE))
    style.configure("Hint.TLabel", background=surfaces.page, foreground=palette.ink_soft,
                    font=(family, SMALL_SIZE))
    style.configure("Warn.TLabel", background=surfaces.page, foreground=palette.warn,
                    font=(family, SMALL_SIZE))
    style.configure("Bad.TLabel", background=surfaces.page, foreground=palette.bad,
                    font=(family, SMALL_SIZE))
    style.configure("Good.TLabel", background=surfaces.page, foreground=palette.good,
                    font=(family, BODY_SIZE))
    style.configure("BandHint.TLabel", background=surfaces.band, foreground=palette.ink_soft,
                    font=(family, SMALL_SIZE))

    # The step the user is on gets a filled row rather than only a colour, so
    # where they are survives being printed, photographed, or looked at by
    # somebody who does not separate blue from grey. The other two rows keep
    # the panel's own colour so only one thing on the rail is ever lit.
    style.configure(
        "RailOn.TLabel",
        background=glass.blend(surfaces.rail, palette.accent, 0.16),
        foreground=palette.accent, font=(family, BODY_SIZE, "bold"),
        padding=(10, 7),
    )
    style.configure("RailDone.TLabel", background=surfaces.rail, foreground=palette.ink_soft,
                    font=(family, BODY_SIZE), padding=(10, 7))
    style.configure("RailOff.TLabel", background=surfaces.rail, foreground=palette.ink_faint,
                    font=(family, BODY_SIZE), padding=(10, 7))
    style.configure("RailTitle.TLabel", background=surfaces.rail, foreground=palette.ink,
                    font=(family, BODY_SIZE + 3, "bold"))
    style.configure("RailNote.TLabel", background=surfaces.rail, foreground=palette.ink_faint,
                    font=(family, SMALL_SIZE))

    quiet = glass.blend(surfaces.band, palette.ink, 0.05)
    style.configure(
        "Wizard.TButton",
        font=(family, BODY_SIZE),
        padding=(18, 9),
        background=quiet,
        foreground=palette.ink,
        bordercolor=palette.rule,
        lightcolor=quiet,
        darkcolor=quiet,
        borderwidth=1,
        relief="flat",
        focuscolor=palette.accent,
    )
    style.map(
        "Wizard.TButton",
        background=[
            ("disabled", surfaces.band),
            ("pressed", glass.blend(quiet, palette.ink, 0.12)),
            ("active", glass.blend(quiet, palette.ink, 0.06)),
        ],
        foreground=[("disabled", palette.ink_faint)],
        bordercolor=[("active", palette.accent)],
    )
    # The page's own answer, filled with the accent so it is found without
    # being read. Only clam honours these; under vista the button keeps the
    # Windows look, which is no worse than what it had.
    style.configure(
        "Accent.TButton",
        font=(family, BODY_SIZE, "bold"),
        padding=(22, 9),
        background=palette.accent,
        foreground=palette.accent_ink,
        bordercolor=palette.accent,
        lightcolor=palette.accent,
        darkcolor=palette.accent,
        borderwidth=1,
        focuscolor=palette.accent_ink,
        relief="flat",
    )
    style.map(
        "Accent.TButton",
        background=[
            ("disabled", glass.blend(surfaces.band, palette.accent, 0.28)),
            ("pressed", glass.blend(palette.accent, "#000000", 0.18)),
            ("active", glass.blend(palette.accent, "#ffffff", 0.14)),
        ],
        foreground=[("disabled", palette.ink_faint)],
    )
    # Tick boxes and radios. clam draws its indicator as a bevelled box with a
    # white fill, which is the most dated thing in the window and, on the dark
    # theme, reads backwards: the unticked boxes are the bright ones.
    #
    # The option names matter. clam's indicator element takes
    # indicatorbackground (the box) and indicatorforeground (the tick or dot);
    # there is no "indicatorcolor", and ttk accepts an option a theme has never
    # heard of without a word, so a style written against the wrong name simply
    # does nothing and leaves the default in place.
    def indicator(name: str, background: str, size: int) -> None:
        empty = glass.blend(background, palette.ink, 0.07)
        style.configure(
            name, background=background, foreground=palette.ink,
            font=(family, size), padding=(2, 6), focuscolor=palette.accent,
            borderwidth=0, relief="flat",
            indicatorbackground=empty,
            indicatorforeground=palette.accent_ink,
            indicatorsize=12,
            indicatormargin=(0, 0, 8, 0),
            upperbordercolor=palette.rule,
            lowerbordercolor=palette.rule,
        )
        style.map(
            name,
            indicatorbackground=[
                # "!selected" first: ttk takes the first matching spec, and a
                # bare ("selected", ...) would also match a disabled-and-ticked
                # box and paint it as though it were live.
                ("disabled", background),
                ("selected", palette.accent),
                ("active", glass.blend(empty, palette.accent, 0.25)),
            ],
            upperbordercolor=[("selected", palette.accent),
                              ("active", palette.accent)],
            lowerbordercolor=[("selected", palette.accent),
                              ("active", palette.accent)],
            # clam greys the label as well as the box under the pointer unless
            # told otherwise, which reads as the row disabling itself.
            foreground=[("disabled", palette.ink_faint), ("active", palette.ink)],
            background=[("active", background)],
        )

    indicator("Wizard.TCheckbutton", surfaces.page, BODY_SIZE)
    indicator("Band.TCheckbutton", surfaces.band, SMALL_SIZE)
    indicator("Wizard.TRadiobutton", surfaces.page, BODY_SIZE + 1)

    # The list of what will be backed up, which is the busiest thing on screen.
    # Borderless, on the panel's own colour, with generous rows: a boxed grid
    # with bevelled headings reads as a database front end, and this is meant
    # to read as a list somebody can skim.
    rows = glass.blend(surfaces.page, palette.ink, 0.03)
    style.configure(
        "Wizard.Treeview",
        background=surfaces.page,
        fieldbackground=surfaces.page,
        foreground=palette.ink,
        font=(family, BODY_SIZE),
        rowheight=30,
        borderwidth=0,
        relief="flat",
    )
    style.layout("Wizard.Treeview", [
        ("Wizard.Treeview.treearea", {"sticky": "nswe"}),
    ])
    style.configure(
        "Wizard.Treeview.Heading",
        background=rows,
        foreground=palette.ink_soft,
        font=(family, SMALL_SIZE, "bold"),
        relief="flat",
        borderwidth=0,
        padding=(8, 7),
    )
    style.map("Wizard.Treeview.Heading",
              background=[("active", glass.blend(rows, palette.accent, 0.10))])

    # Separators, scrollbars and the progress bar: the leftovers that give a
    # window away when everything else has been styled.
    style.configure("TSeparator", background=palette.rule)
    style.configure(
        "Wizard.Vertical.TScrollbar",
        background=glass.blend(surfaces.page, palette.ink, 0.18),
        troughcolor=surfaces.page, bordercolor=surfaces.page,
        arrowcolor=palette.ink_soft, borderwidth=0, relief="flat", arrowsize=12,
    )
    style.map("Wizard.Vertical.TScrollbar",
              background=[("active", glass.blend(surfaces.page, palette.ink, 0.30))])
    style.configure(
        "Wizard.Horizontal.TProgressbar",
        background=palette.accent,
        troughcolor=glass.blend(surfaces.page, palette.ink, 0.08),
        bordercolor=surfaces.page, lightcolor=palette.accent,
        darkcolor=palette.accent, borderwidth=0, thickness=6,
    )
    style.configure(
        "Wizard.TEntry",
        fieldbackground=glass.blend(surfaces.page, palette.ink, 0.04),
        foreground=palette.ink, bordercolor=palette.rule,
        lightcolor=palette.rule, darkcolor=palette.rule,
        insertcolor=palette.ink, borderwidth=1, relief="flat", padding=(8, 7),
    )
    style.map("Wizard.TEntry",
              bordercolor=[("focus", palette.accent)],
              lightcolor=[("focus", palette.accent)],
              darkcolor=[("focus", palette.accent)])
    # Without this the selection keeps clam's default blue, which on the dark
    # page is the one thing louder than the accent.
    style.map(
        "Wizard.Treeview",
        background=[("selected", palette.accent)],
        foreground=[("selected", palette.accent_ink)],
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


# --- frosted surfaces -------------------------------------------------------
@dataclass(frozen=True, slots=True)
class Bloom:
    """One soft colour blob on the wash, positioned as fractions of the window.

    Kept here rather than in the painter because two things need it: the canvas
    that draws it, and the panel colours, which have to know what colour is
    behind them to be tinted by it.
    """

    x: float        # across the window, 0 left to 1 right
    y: float        # down it
    radius: float   # as a fraction of the window's longer side
    tint: str       # which palette tint: "a" or "b"
    strength: float = 0.85

    def colour(self, palette: "Palette") -> str:
        return palette.tint_a if self.tint == "a" else palette.tint_b


#: Both mostly off-screen, so what shows is the soft middle of each rather than
#: a disc with an edge somewhere on the page. One warm-blue from the top left,
#: behind the rail; one violet from the bottom right, behind the buttons.
BLOOMS: tuple[Bloom, ...] = (
    Bloom(0.10, 0.02, 0.62, "a"),
    Bloom(0.90, 0.94, 0.58, "b"),
    # A third, wide and weak, across the middle. Without it an even field with
    # two corner blobs reads as two blobs on a flat grey; with it the whole
    # field moves slightly and the corners stop looking stuck on.
    Bloom(0.55, 0.30, 0.85, "a", 0.22),
)

#: Each panel's centre, as fractions of the window, and how opaque its frosting
#: is. The centre is what decides the tint it picks up from the blooms behind
#: it, which is the whole reason the three panels are different colours rather
#: than three copies of the same grey.
#:
#: Fixed fractions rather than the panel's real position, because these become
#: ttk style colours and a style is global: recomputing them would mean
#: re-styling every widget in the window on every resize. They are worked out
#: for the window's default size and stay put, which the eye cannot catch
#: because the blooms are enormous and their colour barely changes over the
#: distance a window gets dragged.
#:
#: The footer is the least translucent. It holds the buttons, it is the one
#: part of the window that must never be ambiguous, and a heavier panel there
#: gives the window a base to sit on instead of fading out at the bottom.
SURFACE_STOPS: dict[str, tuple[float, float, float]] = {
    "rail": (0.115, 0.45, 0.66),
    "page": (0.610, 0.45, 0.82),
    "band": (0.500, 0.94, 0.78),
}


@dataclass(frozen=True, slots=True)
class Surfaces:
    """The three frosted panel colours, composited from the palette.

    Worked out once, here, because two different things need the identical
    value: the canvas that paints the panel, and the ttk style of the frame
    that sits inside it. If those disagree by a single step the frame shows up
    as a rectangle inside the card -- the exact artefact that makes a window
    look homemade.
    """

    rail: str
    page: str
    band: str


def backdrop_at(palette: Palette, x: float, y: float) -> str:
    """The wash colour at a point, blooms included. Fractions, not pixels.

    What a frosted panel centred there would have behind it, and therefore
    what it is tinted by.
    """
    colour = glass.blend(palette.wash_top, palette.wash_bottom, max(0.0, min(1.0, y)))
    # Distances are worked out on the default window, because the blooms are
    # sized against the longer side and a square measure would squash them.
    longer = max(WINDOW_WIDTH, WINDOW_HEIGHT)
    for bloom in BLOOMS:
        across = (x - bloom.x) * WINDOW_WIDTH
        down = (y - bloom.y) * WINDOW_HEIGHT
        distance = math.hypot(across, down)
        alpha = glass.bloom_alpha(distance, bloom.radius * longer, bloom.strength)
        if alpha:
            colour = glass.blend(colour, bloom.colour(palette), alpha)
    return colour


def surfaces_for(palette: Palette) -> Surfaces:
    """Composite the frosted panels for a palette.

    Each panel is frosted over whatever the wash is doing behind it, blooms
    and all -- which is what a real frosted pane does, since blurring a soft
    colour field leaves the same soft colour, only flatter. It is why the rail
    comes out faintly blue and the button band faintly violet instead of all
    three being the same grey.
    """
    def panel(name: str) -> str:
        x, y, strength = SURFACE_STOPS[name]
        return glass.veil(backdrop_at(palette, x, y), palette.dark, strength)

    return Surfaces(rail=panel("rail"), page=panel("page"), band=panel("band"))


# --- rounded buttons --------------------------------------------------------
#: How round a button is. Matched to the panels it sits on, because two
#: different radii in one window read as a mistake rather than as a choice.
BUTTON_RADIUS = 8

#: ttk stretches the middle of a nine-patched image and leaves the corners
#: alone, so the artwork only has to be large enough to hold two corners and a
#: seam. Anything bigger is pixels nobody sees.
_BUTTON_ART = BUTTON_RADIUS * 2 + 4

#: The generated images, kept alive for as long as the program runs. A Tk photo
#: image is garbage collected like any other object, and a style element whose
#: image has been collected draws nothing at all -- a button-shaped hole, which
#: is a memorable way to find this out.
_button_images: dict[str, object] = {}


def round_buttons(style, tk, palette: Palette) -> bool:
    """Give the buttons real rounded corners. True when it took.

    A ttk button is drawn from border elements which are rectangles by
    construction: there is no option that rounds them, in clam or any other
    built-in theme. The supported way round it is to draw the button yourself
    and hand ttk the picture, which it nine-patches -- corners kept, middle
    stretched -- so one small image serves a button of any width.

    The corners are not transparent, because a Tk photo image has no alpha.
    They are painted the colour of the panel the button sits on, which this
    program computed before it drew anything, so the result is the same as
    transparency would have been.

    Fails soft and says so: a Tk too old for ``element_create``, or one that
    refuses the layout, leaves square buttons behind rather than no buttons.
    """
    surfaces = surfaces_for(palette)
    quiet = glass.blend(surfaces.band, palette.ink, 0.05)
    faces = {
        "accent": (palette.accent, surfaces.band),
        "accentActive": (glass.blend(palette.accent, "#ffffff", 0.14), surfaces.band),
        "accentPressed": (glass.blend(palette.accent, "#000000", 0.18), surfaces.band),
        "accentOff": (glass.blend(surfaces.band, palette.accent, 0.28), surfaces.band),
        "quiet": (quiet, surfaces.band),
        "quietActive": (glass.blend(quiet, palette.ink, 0.06), surfaces.band),
        "quietPressed": (glass.blend(quiet, palette.ink, 0.12), surfaces.band),
        # A face, even disabled: exactly the panel colour leaves a button-shaped
        # hole and the row stops reading as three buttons.
        "quietOff": (glass.blend(surfaces.band, palette.ink, 0.02), surfaces.band),
    }
    try:
        for name, (fill, behind) in faces.items():
            image = tk.PhotoImage(width=_BUTTON_ART, height=_BUTTON_ART)
            image.put(
                glass.photo_data(
                    glass.rounded_pixels(_BUTTON_ART, _BUTTON_ART, BUTTON_RADIUS, fill, behind)
                )
            )
            _button_images[name] = image

        for style_name, prefix in (("Accent.TButton", "accent"), ("Wizard.TButton", "quiet")):
            element = f"{prefix}.roundedbutton"
            style.element_create(
                element, "image", _button_images[prefix],
                ("disabled", _button_images[f"{prefix}Off"]),
                ("pressed", _button_images[f"{prefix}Pressed"]),
                ("active", _button_images[f"{prefix}Active"]),
                # The nine-patch: the outer BUTTON_RADIUS pixels on each side
                # are the corners and are not stretched. Without this the
                # corners are scaled with the rest and the curve turns into a
                # smear as the button gets wider.
                border=BUTTON_RADIUS, sticky="nsew", padding=0,
            )
            style.layout(style_name, [
                (element, {"sticky": "nsew", "children": [
                    ("Button.padding", {"sticky": "nsew", "children": [
                        ("Button.label", {"sticky": "nswe"}),
                    ]}),
                ]}),
            ])
    except Exception as exc:  # noqa: BLE001 -- square buttons beat no buttons
        log.info("rounded buttons are not available here: %s", exc)
        return False
    return True
