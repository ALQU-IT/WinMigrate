"""Frosted-glass surfaces, computed rather than composited.

Glassmorphism is a translucent panel over a blurred, colourful background. The
browser version leans on ``backdrop-filter``: the panel says "sample what is
behind me, blur it, tint it", and the compositor does the work every frame.

Tkinter has no compositor. There is no per-widget alpha, no blur, no
``backdrop-filter``, and a ttk widget paints one opaque colour edge to edge.
Asking it for real glass gets nothing.

But the blur is only there because a web page cannot know what is behind the
panel. Here we draw the background ourselves, so we do know -- exactly, per
pixel, before anything is on screen. So the compositing is done in advance, in
arithmetic: take the colour the background will be at that point, blend the
panel's tint over it at the panel's opacity, and paint the single flat colour
that a real compositor would have produced. The result is not an approximation
of the blend; it *is* the blend, worked out early.

What that buys, and what it costs:

* A panel over a smooth wash is exact, because a blurred smooth wash is the
  same smooth wash. That is the whole visual language -- soft colour fields
  under frosted cards -- so the illusion holds where it matters.
* It cannot float over something busy. A card dragged over text would need the
  text blurred underneath it, and nothing here can blur. So nothing moves over
  anything detailed, which the layout respects.

Everything in this module is arithmetic on colours and coordinates, with no
tkinter import anywhere, so the look can be checked without a display.
"""

from __future__ import annotations

import math

Rgb = tuple[int, int, int]


# --- colour ----------------------------------------------------------------
def parse(colour: str) -> Rgb:
    """``#rrggbb`` (or ``#rgb``) to its components.

    Tk accepts named colours too, but every colour in this program comes from
    the palette as a hex string, so anything else here is a bug worth raising
    rather than quietly rendering black.
    """
    text = colour.strip().lstrip("#")
    if len(text) == 3:
        text = "".join(character * 2 for character in text)
    if len(text) != 6:
        raise ValueError(f"not a hex colour: {colour!r}")
    return int(text[0:2], 16), int(text[2:4], 16), int(text[4:6], 16)


def to_hex(rgb: Rgb) -> str:
    """Components back to ``#rrggbb``, clamped so arithmetic cannot escape."""
    return "#" + "".join(f"{max(0, min(255, round(value))):02x}" for value in rgb)


def blend(under: str, over: str, alpha: float) -> str:
    """Paint ``over`` on ``under`` at ``alpha`` and return the result.

    The one operation the toolkit will not do. ``alpha`` 0 is ``under``
    untouched, 1 is ``over`` covering it, and the clamp means a caller doing
    its own arithmetic cannot produce a colour Tk will reject.
    """
    alpha = max(0.0, min(1.0, alpha))
    beneath, above = parse(under), parse(over)
    return to_hex(
        tuple(a + (b - a) * alpha for a, b in zip(beneath, above, strict=True))
    )


def veil(base: str, dark: bool, strength: float = 0.62) -> str:
    """The frosted film itself: white on a light theme, light on a dark one.

    A dark glass panel is not black. Black over a dark background is a hole,
    and the panel disappears; what makes a dark frosted surface read as a
    surface is that it is *lighter* than what surrounds it, as if lit from
    within. So both themes veil towards white, and the dark one simply uses
    much less of it.
    """
    return blend(base, "#ffffff", strength * (0.12 if dark else 1.0))


def edges(surface: str, dark: bool) -> tuple[str, str]:
    """The rim of a glass panel: ``(highlight, shade)``.

    The single most recognisable part of the look, and the cheapest. Real glass
    catches light along its top edge and sits in a faint shadow along its
    bottom, and a rounded rectangle with those two hairlines reads as a pane
    with thickness, where the same rectangle without them reads as a coloured
    box.
    """
    return (
        blend(surface, "#ffffff", 0.55 if dark else 0.85),
        blend(surface, "#000000", 0.22 if dark else 0.07),
    )


# --- the wash behind the glass ---------------------------------------------
def wash_bands(top: str, bottom: str, steps: int) -> list[str]:
    """A vertical gradient as a list of flat colours, top to bottom.

    Painted as a stack of rectangles, because Tk has no gradient. Enough steps
    and the banding falls below what an eye picks up on a screen; the caller
    chooses how many, since a tall window needs more than a short one.
    """
    if steps <= 0:
        return []
    if steps == 1:
        return [top]
    return [blend(top, bottom, index / (steps - 1)) for index in range(steps)]


def bloom_alpha(distance: float, radius: float, strength: float) -> float:
    """How much of a bloom's colour reaches a point ``distance`` from it.

    Shared by the two halves that must agree: the canvas painting the bloom,
    and the panel colours computed from the palette before there is a canvas.
    A panel is tinted by what is behind it, so if these two used different
    falloffs the panel would be a different colour from the light it is
    supposedly letting through, and the edge where they meet would show.

    Squared falloff, so the change is concentrated in the middle where there is
    no boundary to notice, rather than spread out to a visible disc edge.
    """
    if radius <= 0 or distance >= radius:
        return 0.0
    reach = 1.0 - max(0.0, distance) / radius
    return strength * reach * reach


def bloom_rings(base: str, tint: str, rings: int, strength: float = 0.85) -> list[str]:
    """One soft colour blob, outermost ring first, fading into ``base``.

    Drawn as nested ovals rather than a real radial gradient, which Tk also has
    not got. The falloff is squared instead of linear because a linear fade
    leaves a visible disc edge: the eye finds the boundary of a flat-sided
    circle immediately, and the squared curve puts almost all the change in the
    middle where there is no edge to see.
    """
    if rings <= 0:
        return []
    colours = []
    for index in range(rings):
        # Ring 0 is the outermost, so its distance from the centre is the full
        # radius; expressed as a fraction so it can go through the same falloff
        # the panel tints use.
        distance = 1.0 - (index + 1) / rings
        colours.append(blend(base, tint, bloom_alpha(distance, 1.0, strength)))
    return colours


# --- the shape of a pane ---------------------------------------------------
def rounded_rectangle(
    x0: float, y0: float, x1: float, y1: float, radius: float, steps: int = 6
) -> list[float]:
    """A rounded rectangle as a flat ``[x, y, x, y, ...]`` polygon.

    For ``create_polygon``, which is the only way to get a filled rounded shape
    out of a Tk canvas. Corners are real quarter-circle arcs rather than
    ``smooth=True`` splines: a spline through the corner points pulls the
    straight edges in as well, so a card drawn that way bulges, and next to a
    square-edged widget the bulge is obvious.
    """
    x0, x1 = min(x0, x1), max(x0, x1)
    y0, y1 = min(y0, y1), max(y0, y1)
    # A radius cannot exceed half the shorter side, or the corners cross over
    # and the polygon folds through itself.
    radius = max(0.0, min(radius, (x1 - x0) / 2, (y1 - y0) / 2))
    if radius == 0:
        return [x0, y0, x1, y0, x1, y1, x0, y1]

    steps = max(1, steps)
    points: list[float] = []
    # Clockwise from the top-left corner, each corner swept a quarter turn.
    corners = (
        (x0 + radius, y0 + radius, 180, 270),
        (x1 - radius, y0 + radius, 270, 360),
        (x1 - radius, y1 - radius, 0, 90),
        (x0 + radius, y1 - radius, 90, 180),
    )
    for centre_x, centre_y, start, end in corners:
        for step in range(steps + 1):
            angle = math.radians(start + (end - start) * step / steps)
            points.append(centre_x + radius * math.cos(angle))
            points.append(centre_y + radius * math.sin(angle))
    return points


# --- the panels themselves --------------------------------------------------
class Backdrop:
    """Paints the wash, and the frosted panels that sit on it.

    Owns one canvas that fills the window. Everything the user reads lives in
    ordinary ttk frames embedded in that canvas, positioned over the panels
    this draws -- so the text is real text, selectable and styled by ttk, and
    only the surfaces underneath are drawn.

    The gaps between panels are the point. That is the only place the wash
    shows, and without it the panels have nothing to be panels *against*.
    """

    def __init__(self, canvas, palette, radius: int = 10):
        self.canvas = canvas
        self.palette = palette
        self.radius = radius
        self._painted: tuple[int, int] | None = None

    # -- colours ------------------------------------------------------------
    def wash_at(self, y: float, height: float) -> str:
        """The wash colour at a height, before any bloom is added.

        The blooms are deliberately left out: a panel is one flat colour, and
        taking its tint from a bloom that only covers part of it would make the
        panel disagree with itself at the edges.
        """
        if height <= 0:
            return self.palette.wash_top
        return blend(
            self.palette.wash_top,
            self.palette.wash_bottom,
            max(0.0, min(1.0, y / height)),
        )

    def surface(self, y: float, height: float, strength: float = 0.62) -> str:
        """The colour a frosted panel comes out at that height.

        The one number that has to be shared: the canvas paints the panel with
        it, and the ttk frame sitting inside the panel is given the same value
        as its background. If they disagree by even one step the frame shows as
        a rectangle inside the card, which is worse than no card at all.
        """
        return veil(self.wash_at(y, height), self.palette.dark, strength)

    # -- painting -----------------------------------------------------------
    def paint_wash(self, width: int, height: int, blooms=(), bands: int = 96) -> None:
        """The field, then the colour blooms over it.

        ``blooms`` are objects carrying ``x``, ``y`` and ``radius`` as
        fractions of the window, a ``strength``, and a ``colour(palette)``.
        They are passed in from the theme rather than written here, because the
        panel colours have to know what is behind them and both have to work it
        out from the same numbers.
        """
        if self.palette.wash_top == self.palette.wash_bottom:
            # The usual case; the reason for it is in _bloom below.
            self.canvas.create_rectangle(
                0, 0, width, height, fill=self.palette.wash_top,
                outline=self.palette.wash_top, tags="backdrop",
            )
        else:
            colours = wash_bands(self.palette.wash_top, self.palette.wash_bottom, bands)
            step = height / max(1, len(colours))
            for index, colour in enumerate(colours):
                # Each band is drawn one pixel past the next so rounding cannot
                # leave a hairline of bare canvas between them.
                self.canvas.create_rectangle(
                    0, index * step, width, index * step + step + 1,
                    fill=colour, outline=colour, tags="backdrop",
                )
        longer = max(width, height)
        for bloom in blooms:
            self._bloom(
                bloom.x * width, bloom.y * height, bloom.radius * longer,
                bloom.colour(self.palette), bloom.strength,
            )

    def _bloom(self, x: float, y: float, radius: float, tint: str,
               strength: float = 0.85, rings: int = 48) -> None:
        """One blob, as nested ovals drawn from the outside in.

        Each oval is filled with a flat colour, because a canvas has no alpha:
        a ring does not tint what is under it, it *replaces* it. So the colour
        has to be composited here, against whatever the ring is covering -- and
        that only works when what it covers is one known colour.

        Which is why the field behind these is even. Over a vertical gradient a
        ring would have to pick a single base for its whole area, so a ring
        reaching from a bloom in one corner across to the far end of the
        gradient would repaint that region in the wrong shade: a visible step
        along the oval's edge, in exactly the place the softness was meant to
        hide. The blooms carry the colour; the field stays flat.
        """
        base = self.wash_at(y, self.canvas_height())
        for index, colour in enumerate(bloom_rings(base, tint, rings, strength)):
            # Ring 0 is the outermost, drawn first, with the brighter inner
            # rings landing on top of it.
            size = radius * (1 - index / rings)
            self.canvas.create_oval(
                x - size, y - size, x + size, y + size,
                fill=colour, outline=colour, tags="backdrop",
            )

    def paint_panel(self, x0: float, y0: float, x1: float, y1: float, fill: str) -> str:
        """One frosted panel, in a colour decided in advance.

        ``fill`` is passed in rather than worked out from where the panel
        happens to sit, because the ttk frame that goes inside it needs the
        same colour and gets it from a ttk style. A style is global and
        changing one re-styles every widget using it, so recomputing these on
        every resize would mean rewriting the whole widget tree each time the
        window is dragged wider. They are fixed by the palette instead, and
        both sides read them from the same place.
        """
        highlight, shade = edges(fill, self.palette.dark)
        self.canvas.create_polygon(
            rounded_rectangle(x0, y0, x1, y1, self.radius),
            fill=fill, outline=shade, width=1, tags="backdrop",
        )
        # The lit top edge, inset by a pixel so it reads as the inner face of
        # the pane rather than as a second outline around it.
        self.canvas.create_line(
            x0 + self.radius, y0 + 1, x1 - self.radius, y0 + 1,
            fill=highlight, tags="backdrop",
        )
        return fill

    def canvas_height(self) -> int:
        try:
            return max(1, int(self.canvas.winfo_height()))
        except Exception:  # noqa: BLE001 -- not realised yet
            return 1

    def clear(self) -> None:
        self.canvas.delete("backdrop")


# --- rounded artwork for widgets Tk draws as rectangles ---------------------
def rounded_coverage(
    width: int, height: int, radius: float, samples: int = 4
) -> list[list[float]]:
    """How much of each pixel a rounded rectangle covers, row by row.

    ``0.0`` is outside, ``1.0`` inside, and the fractions in between are the
    corner pixels. Those fractions are the whole point: a corner stepped
    straight from filled to empty is a staircase, and at the sizes a button is
    drawn at the staircase is what the eye reads instead of a curve.

    A ttk button is drawn from border elements that are square by construction
    and cannot be told otherwise. The way round it is to hand ttk an image and
    let it nine-patch the thing -- which needs artwork, which needs this.

    Coverage is estimated by sampling each pixel on a ``samples`` x ``samples``
    grid, offset to the centres of its cells so the edges of the pixel are not
    counted twice.
    """
    if width <= 0 or height <= 0:
        return []
    radius = max(0.0, min(radius, width / 2, height / 2))
    samples = max(1, samples)
    step = 1.0 / samples
    offset = step / 2

    # The centres of the four corner arcs.
    corners = (
        (radius, radius), (width - radius, radius),
        (width - radius, height - radius), (radius, height - radius),
    )

    rows: list[list[float]] = []
    for pixel_y in range(height):
        row: list[float] = []
        for pixel_x in range(width):
            inside = 0
            for sub_y in range(samples):
                y = pixel_y + offset + sub_y * step
                for sub_x in range(samples):
                    x = pixel_x + offset + sub_x * step
                    if _within(x, y, width, height, radius, corners):
                        inside += 1
            row.append(inside / (samples * samples))
        rows.append(row)
    return rows


def _within(x, y, width, height, radius, corners) -> bool:
    """Is this point inside the rounded rectangle?

    Outside the corner squares the shape is an ordinary rectangle, so only a
    point in one of the four corner squares has to be measured against its arc.
    """
    if not (0 <= x <= width and 0 <= y <= height):
        return False
    if radius <= 0:
        return True
    left, right = x < radius, x > width - radius
    top, bottom = y < radius, y > height - radius
    if not ((left or right) and (top or bottom)):
        return True
    centre_x, centre_y = corners[0 if left and top else 1 if right and top else
                                 2 if right and bottom else 3]
    return math.hypot(x - centre_x, y - centre_y) <= radius


def rounded_pixels(
    width: int, height: int, radius: float, fill: str, behind: str, samples: int = 4
) -> list[list[str]]:
    """A rounded rectangle as flat pixels, already composited onto ``behind``.

    There is no alpha channel here either -- a Tk photo image is opaque -- so
    the corners are not transparent, they are painted the colour of whatever
    the button sits on. Which is knowable: it is the frosted panel underneath,
    and this program worked that colour out before it drew anything.
    """
    return [
        [blend(behind, fill, coverage) for coverage in row]
        for row in rounded_coverage(width, height, radius, samples)
    ]


def photo_data(pixels: list[list[str]]) -> str:
    """Pixels in the one format ``PhotoImage.put`` takes in a single call.

    Setting them one at a time is thousands of Tcl round trips per button; this
    is one.
    """
    return " ".join("{" + " ".join(row) + "}" for row in pixels)
