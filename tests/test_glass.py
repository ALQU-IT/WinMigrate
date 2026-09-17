"""The frosted-glass look, checked without a display.

Everything visual here is arithmetic on colours and coordinates, which is the
whole reason it was written as arithmetic: a look that can only be judged by
looking at it is a look that silently rots.
"""

from __future__ import annotations

import math

import pytest

from winmigrate.gui import glass, theme


# --- colour ----------------------------------------------------------------
@pytest.mark.parametrize(
    "text, expected",
    [("#ffffff", (255, 255, 255)), ("#000000", (0, 0, 0)),
     ("0f6cbd", (15, 108, 189)), ("#abc", (170, 187, 204))],
)
def test_colours_are_read_the_way_the_palette_writes_them(text, expected):
    assert glass.parse(text) == expected


@pytest.mark.parametrize("bad", ["", "#", "#12345", "rebeccapurple", "#gggggg"])
def test_something_that_is_not_a_colour_is_refused_rather_than_rendered_black(bad):
    """Every colour in this program comes from the palette as hex. Anything
    else arriving here is a bug, and a bug that paints a widget black is one
    nobody finds until it is in front of somebody."""
    with pytest.raises(ValueError):
        glass.parse(bad)


def test_blending_is_the_compositing_the_toolkit_will_not_do():
    assert glass.blend("#000000", "#ffffff", 0.0) == "#000000"
    assert glass.blend("#000000", "#ffffff", 1.0) == "#ffffff"
    assert glass.blend("#000000", "#ffffff", 0.5) == "#808080"


def test_a_blend_outside_its_range_is_clamped_not_extrapolated():
    """Callers do their own arithmetic to work out an alpha, and arithmetic
    overshoots. A colour past either end is one Tk refuses, which takes the
    window down over a rounding error."""
    assert glass.blend("#000000", "#ffffff", -3.0) == "#000000"
    assert glass.blend("#000000", "#ffffff", 9.0) == "#ffffff"


def test_a_dark_panel_is_lighter_than_what_is_around_it_not_darker():
    """Black over a dark field is a hole, and the panel disappears. What makes
    a dark frosted surface read as a surface is being lit from within, so both
    themes veil towards white and the dark one merely uses far less of it."""
    def brightness(colour: str) -> int:
        return sum(glass.parse(colour)) // 3

    for palette in (theme.LIGHT, theme.DARK):
        field = palette.wash_top
        surfaces = theme.surfaces_for(palette)
        for panel in (surfaces.rail, surfaces.page, surfaces.band):
            assert brightness(panel) > brightness(field), palette


def test_a_pane_has_a_lit_top_edge_and_a_shaded_bottom():
    """The cheapest half of the whole look: the same rectangle without the two
    hairlines reads as a coloured box rather than a pane with thickness."""
    def brightness(colour: str) -> int:
        return sum(glass.parse(colour)) // 3

    for dark in (False, True):
        highlight, shade = glass.edges("#808080", dark)
        assert brightness(highlight) > brightness("#808080") > brightness(shade)


# --- the wash ---------------------------------------------------------------
def test_a_gradient_starts_and_ends_where_it_was_told_to():
    bands = glass.wash_bands("#000000", "#ffffff", 5)
    assert len(bands) == 5
    assert bands[0] == "#000000" and bands[-1] == "#ffffff"
    # Monotonic, or the fade visibly doubles back on itself.
    values = [glass.parse(band)[0] for band in bands]
    assert values == sorted(values)


@pytest.mark.parametrize("steps", [0, -1])
def test_a_gradient_of_no_steps_is_empty_rather_than_a_division_by_zero(steps):
    assert glass.wash_bands("#000000", "#ffffff", steps) == []


def test_a_gradient_of_one_step_does_not_divide_by_zero():
    assert glass.wash_bands("#123456", "#ffffff", 1) == ["#123456"]


def test_a_bloom_fades_out_and_stops_at_its_own_edge():
    assert glass.bloom_alpha(0, 100, 0.8) == pytest.approx(0.8)
    assert glass.bloom_alpha(100, 100, 0.8) == 0.0
    assert glass.bloom_alpha(200, 100, 0.8) == 0.0
    # Squared falloff: half way out keeps a quarter, not a half. A linear fade
    # leaves a visible disc edge, because the eye finds the boundary of a
    # flat-sided circle immediately.
    assert glass.bloom_alpha(50, 100, 0.8) == pytest.approx(0.2)


def test_a_bloom_of_no_radius_contributes_nothing_rather_than_dividing_by_zero():
    assert glass.bloom_alpha(0, 0, 0.8) == 0.0


def test_the_painter_and_the_panel_colours_fade_the_same_way():
    """The two halves that must agree: the canvas drawing a bloom, and the
    panel colour computed from the palette for the surface sitting over it. A
    panel tinted by a different falloff from the light it is letting through
    shows a seam at its own edge."""
    rings = glass.bloom_rings("#000000", "#ffffff", 4, strength=1.0)
    # Ring 0 is the outermost and so the faintest.
    assert glass.parse(rings[0])[0] < glass.parse(rings[-1])[0]
    for index, colour in enumerate(rings):
        distance = 1.0 - (index + 1) / 4
        expected = glass.blend("#000000", "#ffffff", glass.bloom_alpha(distance, 1.0, 1.0))
        assert colour == expected


# --- the shape --------------------------------------------------------------
def test_a_rounded_rectangle_stays_inside_the_box_it_was_given():
    points = glass.rounded_rectangle(10, 20, 110, 80, 12)
    xs, ys = points[0::2], points[1::2]
    assert min(xs) >= 10 - 1e-9 and max(xs) <= 110 + 1e-9
    assert min(ys) >= 20 - 1e-9 and max(ys) <= 80 + 1e-9


def test_a_radius_too_big_for_the_box_is_capped_rather_than_folding_it_inside_out():
    """Half the shorter side is the limit. Past it the corner arcs cross over
    and the polygon passes through itself, which Tk draws as a bow tie."""
    points = glass.rounded_rectangle(0, 0, 40, 20, 500)
    xs, ys = points[0::2], points[1::2]
    assert min(xs) >= -1e-9 and max(xs) <= 40 + 1e-9
    assert min(ys) >= -1e-9 and max(ys) <= 20 + 1e-9
    # Capped at 10, so the shape is a stadium: its widest point is the full box.
    assert max(ys) - min(ys) == pytest.approx(20)


def test_a_radius_of_nothing_is_the_plain_rectangle():
    assert glass.rounded_rectangle(0, 0, 10, 5, 0) == [0, 0, 10, 0, 10, 5, 0, 5]


def test_coordinates_given_backwards_still_describe_the_same_box():
    """A layout that subtracts its way to a negative width should come out as a
    thin panel, not as a polygon wound the other way that Tk fills oddly."""
    assert glass.rounded_rectangle(110, 80, 10, 20, 12) == glass.rounded_rectangle(
        10, 20, 110, 80, 12
    )


def test_the_corners_are_arcs_rather_than_a_spline_pulling_the_sides_in():
    """smooth=True through the corner points bows the straight edges as well,
    and next to a square-edged widget the bulge is obvious. Every corner point
    must sit on its own quarter-circle."""
    radius = 20
    points = glass.rounded_rectangle(0, 0, 200, 100, radius, steps=8)
    corners = [(radius, radius), (200 - radius, radius),
               (200 - radius, 100 - radius), (radius, 100 - radius)]
    for x, y in zip(points[0::2], points[1::2], strict=True):
        assert any(
            math.hypot(x - cx, y - cy) == pytest.approx(radius, abs=1e-6)
            for cx, cy in corners
        ), (x, y)


# --- the layout the window builds on the canvas -----------------------------
def test_the_three_panels_never_overlap_and_always_leave_the_wash_showing(
    monkeypatch, tmp_path
):
    """The gaps are the design. Panels packed edge to edge would cover every
    pixel of the wash, and the frosted look would be three grey rectangles."""
    from guistub import open_window

    wizard, _ = open_window(monkeypatch, {"profile_root": str(tmp_path)})
    canvas = wizard.backdrop_canvas

    class Size:
        width, height = 1000, 700

    wizard._relayout(Size())

    boxes = {
        name: (
            canvas.windows[item]["x"], canvas.windows[item]["y"],
            canvas.windows[item]["width"], canvas.windows[item]["height"],
        )
        for name, item in wizard._panels.items()
    }
    for name, (x, y, width, height) in boxes.items():
        assert width > 0 and height > 0, name
        assert x >= 0 and y >= 0, name
        assert x + width <= 1000 and y + height <= 700, name

    def overlap(a, b):
        ax, ay, aw, ah = a
        bx, by, bw, bh = b
        return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah

    assert not overlap(boxes["rail"], boxes["page"])
    assert not overlap(boxes["rail"], boxes["band"])
    assert not overlap(boxes["page"], boxes["band"])
    # And the rail is on the left of the page, not somewhere behind it.
    assert boxes["rail"][0] + boxes["rail"][2] <= boxes["page"][0]


def test_a_window_squeezed_small_still_lays_out_rather_than_inverting(monkeypatch, tmp_path):
    """Every panel's size is one subtraction away from a negative number. A
    negative width is not an exception in Tk, it is a widget that vanishes."""
    from guistub import open_window

    wizard, _ = open_window(monkeypatch, {"profile_root": str(tmp_path)})

    for width, height in ((320, 240), (200, 150), (2560, 1440)):
        size = type("Size", (), {"width": width, "height": height})()
        wizard._relayout(size)
        for name, item in wizard._panels.items():
            entry = wizard.backdrop_canvas.windows[item]
            assert entry["width"] >= 1 and entry["height"] >= 1, (name, width, height)


def test_nothing_is_painted_before_the_window_has_a_size(monkeypatch, tmp_path):
    """<Configure> fires once with a 1x1 geometry before the window is laid
    out. Painting then means dividing a wash into bands of no height."""
    from guistub import open_window

    wizard, _ = open_window(monkeypatch, {"profile_root": str(tmp_path)})
    wizard.backdrop_canvas.items.clear()
    wizard._relayout(type("Size", (), {"width": 1, "height": 1})())
    assert wizard.backdrop_canvas.items == []


def test_repainting_replaces_the_backdrop_instead_of_stacking_copies(monkeypatch, tmp_path):
    """Every resize repaints. Without clearing first, a window dragged for a
    few seconds accumulates thousands of canvas items and the whole thing
    slows to a crawl."""
    from guistub import open_window

    wizard, _ = open_window(monkeypatch, {"profile_root": str(tmp_path)})
    size = type("Size", (), {"width": 1000, "height": 700})()

    wizard._relayout(size)
    once = len(wizard.backdrop_canvas.items)
    for _ in range(5):
        wizard._relayout(size)
    assert len(wizard.backdrop_canvas.items) == once
    assert once > 0


def test_the_panels_are_painted_in_the_colour_their_widgets_are_styled_with(
    monkeypatch, tmp_path
):
    """The one number that has to be shared. If the canvas paints a panel in a
    colour the ttk frame inside it does not have, the frame shows as a
    rectangle within the card -- the exact artefact that makes a window look
    homemade."""
    from guistub import open_window

    wizard, _ = open_window(monkeypatch, {"profile_root": str(tmp_path)})
    wizard._relayout(type("Size", (), {"width": 1000, "height": 700})())

    painted = {
        item[2]["fill"]
        for item in wizard.backdrop_canvas.items
        if item[0] == "polygon"
    }
    surfaces = theme.surfaces_for(wizard.palette)
    assert painted == {surfaces.rail, surfaces.page, surfaces.band}

    styles = wizard.style.styles
    assert styles["Rail.TFrame"]["background"] == surfaces.rail
    assert styles["Page.TFrame"]["background"] == surfaces.page
    assert styles["Band.TFrame"]["background"] == surfaces.band


# --- artwork for widgets Tk insists on drawing as rectangles ----------------
def test_a_rounded_corner_is_shaded_rather_than_stepped():
    """A corner that goes straight from filled to empty is a staircase, and at
    the size a button is drawn the staircase is what the eye reads instead of a
    curve. The fractional pixels are the curve."""
    coverage = glass.rounded_coverage(20, 20, 6)

    assert coverage[0][0] == 0.0          # the very corner is outside
    assert coverage[10][10] == 1.0        # the middle is inside
    partial = [c for row in coverage for c in row if 0.0 < c < 1.0]
    assert partial, "no partly covered pixels: the corners are hard steps"


def test_the_straight_edges_stay_straight():
    """Only the four corner squares are curved. A shape that pulled its sides
    in as well would read as a lozenge, and next to a square-edged widget the
    difference is obvious."""
    coverage = glass.rounded_coverage(30, 30, 8)
    middle = len(coverage) // 2
    assert coverage[middle][0] == 1.0     # left edge, half way down
    assert coverage[0][middle] == 1.0     # top edge, half way across


def test_a_radius_bigger_than_the_button_is_capped():
    coverage = glass.rounded_coverage(10, 10, 999)
    assert coverage[5][5] == 1.0
    assert coverage[0][0] == 0.0


@pytest.mark.parametrize("width, height", [(0, 10), (10, 0), (-1, -1)])
def test_artwork_with_no_size_is_empty_rather_than_an_error(width, height):
    assert glass.rounded_coverage(width, height, 4) == []


def test_a_radius_of_nothing_covers_every_pixel():
    coverage = glass.rounded_coverage(6, 6, 0)
    assert all(value == 1.0 for row in coverage for value in row)


def test_the_corners_are_painted_the_colour_behind_them_not_left_transparent():
    """A Tk photo image has no alpha channel. The corners cannot be see-through,
    so they are painted the colour of the panel the button sits on -- which
    this program worked out before it drew anything."""
    pixels = glass.rounded_pixels(16, 16, 5, "#0f6cbd", "#ffffff")

    assert pixels[0][0] == "#ffffff"      # outside: the panel shows
    assert pixels[8][8] == "#0f6cbd"      # inside: the button
    # And somewhere along each corner arc there are pixels that are neither,
    # which is the antialiasing.
    blended = [
        value for row in pixels for value in row
        if value not in ("#ffffff", "#0f6cbd")
    ]
    assert blended, "the corners step straight from panel to button"


def test_pixels_go_to_tk_in_one_call():
    """Setting them one at a time is thousands of Tcl round trips per button."""
    data = glass.photo_data([["#000000", "#ffffff"], ["#ff0000", "#00ff00"]])
    assert data == "{#000000 #ffffff} {#ff0000 #00ff00}"


def test_the_buttons_are_given_rounded_artwork_and_a_layout_that_uses_it(monkeypatch):
    """A ttk button is drawn from border elements which are rectangles by
    construction: no option rounds them, in clam or any other built-in theme.
    The supported way round it is to hand ttk a picture and let it nine-patch
    the thing -- so both halves have to be there, the element and the layout
    pointing at it."""
    import guistub

    guistub.install(monkeypatch)
    import tkinter as tk

    style = guistub.Style()
    assert theme.round_buttons(style, tk, theme.DARK) is True

    for name, prefix in (("Accent.TButton", "accent"), ("Wizard.TButton", "quiet")):
        element = f"{prefix}.roundedbutton"
        assert element in style.elements
        kind, args, kwargs = style.elements[element]
        assert kind == "image"
        # The nine-patch. Without it the corners stretch with the rest and the
        # curve smears as the button gets wider.
        assert kwargs["border"] == theme.BUTTON_RADIUS
        assert kwargs["sticky"] == "nsew"
        # Every state the button can be in has its own picture, or it snaps
        # back to a square one when pressed.
        assert {spec[0] for spec in args[1:]} == {"disabled", "pressed", "active"}
        assert style.layouts[name][0][0] == element


def test_a_tk_that_refuses_the_artwork_keeps_square_buttons_rather_than_none(monkeypatch):
    """Fails soft, like everything else that reaches for a platform feature."""
    import guistub

    guistub.install(monkeypatch)
    import tkinter as tk

    class Refusing(guistub.Style):
        def element_create(self, *args, **kwargs):
            raise tk.TclError("no image element here")

    assert theme.round_buttons(Refusing(), tk, theme.LIGHT) is False


def test_the_artwork_is_kept_alive_after_it_is_handed_over(monkeypatch):
    """A Tk photo image is garbage collected like any other object, and a style
    element whose image has been collected draws nothing -- a button-shaped
    hole, which is a memorable way to find this out."""
    import guistub

    guistub.install(monkeypatch)
    import tkinter as tk

    theme.round_buttons(guistub.Style(), tk, theme.DARK)
    assert theme._button_images, "nothing is holding a reference to the images"
    for image in theme._button_images.values():
        assert image.data, "an image was created and never filled in"
