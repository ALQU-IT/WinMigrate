"""The things that separate an application from something written in a hurry.

All of them are invisible when they are right, which is why they are the ones
that never get added.
"""

from __future__ import annotations

from pathlib import Path

from winmigrate.gui import desktop, icon
from winmigrate.platform_win import Environment


class FakeRoot:
    """Enough of a Tk root to place, scale and decorate."""

    def __init__(self, dpi: float = 96.0, screen=(1920, 1080)):
        self._dpi = dpi
        self._screen = screen
        self.geometry_set = ""
        self.tk_calls: list = []
        self.icons: list = []

    def winfo_fpixels(self, _spec):
        return self._dpi

    def winfo_screenwidth(self):
        return self._screen[0]

    def winfo_screenheight(self):
        return self._screen[1]

    def geometry(self, value):
        self.geometry_set = value

    def iconphoto(self, default, image):
        self.icons.append((default, image))

    @property
    def tk(self):
        return self

    def call(self, *args):
        self.tk_calls.append(args)


# --- not blurry -------------------------------------------------------------
def test_tk_is_told_what_the_screen_actually_is():
    """Most Windows laptops ship at 125% or 150%. A process that has not said
    it understands that gets drawn small and bitmap-stretched: every edge soft,
    every letter fuzzy."""
    root = FakeRoot(dpi=144.0)  # 150%

    scale = desktop.apply_scaling(root, env=Environment.fixture(Path("/tmp/p"), {}))

    assert scale == 2.0  # 144 dpi / 72 points
    assert ("tk", "scaling", 2.0) in root.tk_calls


def test_the_window_honours_the_text_size_the_user_asked_windows_for():
    """A tool that migrates somebody's text-size setting and then ignores it in
    its own window has missed its own point."""
    env = Environment.fixture(
        Path("/tmp/p"), {r"HKCU\Software\Microsoft\Accessibility": {"TextScaleFactor": 150}}
    )

    assert desktop.text_scale_factor(env) == 1.5
    assert desktop.apply_scaling(FakeRoot(dpi=96.0), env) == 2.0


def test_an_unset_or_impossible_text_size_is_simply_one():
    """Windows clamps its own slider to 100-225. A value outside that did not
    come from the slider, and guessing what it meant is worse than ignoring it."""
    def with_value(value):
        return Environment.fixture(
            Path("/tmp/p"),
            {r"HKCU\Software\Microsoft\Accessibility": {"TextScaleFactor": value}},
        )

    assert desktop.text_scale_factor(Environment.fixture(Path("/tmp/p"), {})) == 1.0
    assert desktop.text_scale_factor(with_value(0)) == 1.0
    assert desktop.text_scale_factor(with_value(5000)) == 1.0
    assert desktop.text_scale_factor(with_value("nonsense")) == 1.0


def test_a_screen_tk_cannot_measure_does_not_stop_the_window():
    class Awkward(FakeRoot):
        def winfo_fpixels(self, _spec):
            raise RuntimeError("no display")

    assert desktop.screen_dpi(Awkward()) == 96.0


# --- where it opens ---------------------------------------------------------
def test_the_window_opens_where_somebody_is_looking():
    root = FakeRoot(screen=(1920, 1080))

    geometry = desktop.centre(root, 940, 660)

    # Horizontally centred; a little above the middle, because the eye reads
    # the centre of a screen as higher than it is.
    assert geometry == "940x660+490+140"
    assert root.geometry_set == geometry


def test_a_screen_smaller_than_the_window_is_not_placed_off_it():
    geometry = desktop.centre(FakeRoot(screen=(800, 600)), 940, 660)
    assert geometry.endswith("+0+0")


def test_a_root_that_cannot_say_how_big_the_screen_is_still_gets_a_size():
    class Blind(FakeRoot):
        def winfo_screenwidth(self):
            raise RuntimeError("no display")

    assert desktop.centre(Blind(), 940, 660) == "940x660"


# --- its own icon -----------------------------------------------------------
def test_the_window_gets_an_icon_of_its_own():
    """Without one the title bar and the taskbar show Tk's feather, and nothing
    says "thrown together" more quickly."""
    root = FakeRoot()

    assert desktop.set_icon(root, image_factory=lambda: "an image") is True
    assert root.icons == [(True, "an image")]


def test_an_icon_that_cannot_be_set_is_not_worth_an_exception():
    class Plain(FakeRoot):
        def iconphoto(self, default, image):
            raise RuntimeError("no window manager")

    assert desktop.set_icon(Plain(), image_factory=lambda: "x") is False


def test_the_icon_is_a_real_png_at_every_size():
    for size in (16, 32, 64):
        data = icon.png_bytes(size)
        assert data.startswith(b"\x89PNG\r\n\x1a\n")
        # IHDR carries the dimensions, and they have to be the ones asked for:
        # a 16-pixel icon must be drawn at 16 pixels, not photographed from a
        # bigger one.
        assert data[16:24] == size.to_bytes(4, "big") * 2


def test_the_icon_reads_as_a_mark_rather_than_a_smudge():
    """It has to work at sixteen pixels across. Corners clear, a solid body,
    and a light mark inside it that is actually there."""
    size = 16
    raw = icon.rgba(size)

    def pixel(column, row):
        offset = (row * size + column) * 4
        return tuple(raw[offset:offset + 4])

    assert pixel(0, 0)[3] == 0, "the corner should be transparent"
    assert pixel(size // 2, 1)[3] > 200, "the top edge should be solid"
    # The chevron: light pixels, inside, roughly where its point is.
    light = sum(
        1
        for row in range(size)
        for column in range(size)
        if pixel(column, row)[0] > 200 and pixel(column, row)[1] > 200
    )
    assert 15 < light < size * size // 3, f"{light} light pixels is not a mark"


def test_the_ico_for_the_frozen_build_holds_every_size():
    data = icon.ico_bytes((16, 32, 48))

    assert data[:4] == b"\x00\x00\x01\x00"      # reserved, type 1 (icon)
    assert data[4:6] == (3).to_bytes(2, "little")
    # Each directory entry points at a PNG that is really there.
    for index, size in enumerate((16, 32, 48)):
        entry = data[6 + index * 16: 6 + (index + 1) * 16]
        assert entry[0] == size
        offset = int.from_bytes(entry[12:16], "little")
        assert data[offset:offset + 8] == b"\x89PNG\r\n\x1a\n"


def test_none_of_it_is_load_bearing():
    """A machine where none of this works gets a window that is merely plain,
    which is what it had before."""
    assert desktop.make_dpi_aware() is False       # not Windows, here
    assert desktop.set_taskbar_identity() is False


# --- what the download looks like before it is even opened ------------------
def test_the_version_stamp_is_built_from_the_version_in_the_package():
    """Right-click Properties on an unsigned download showing nothing is the
    only thing separating it from an anonymous file."""
    import build_assets

    assert build_assets.version_tuple("1.2.3") == (1, 2, 3, 0)
    assert build_assets.version_tuple("0.1.0rc2") == (0, 1, 0, 0)
    assert build_assets.version_tuple("nonsense") == (0, 0, 0, 0)

    text = build_assets.version_info("2.5.1")
    assert "filevers=(2, 5, 1, 0)" in text
    assert "'WinMigrate'" in text and "'ALQU-IT'" in text
    assert "StringStruct('FileVersion', '2.5.1')" in text


def test_the_build_asks_for_the_icon_and_the_stamp():
    """A step that generates them and a build that never passes them is the
    kind of thing nobody notices until a release has shipped without them."""
    workflow = Path(".github/workflows/build-exe.yml").read_text(encoding="utf-8")

    assert "python build_assets.py" in workflow
    assert workflow.count("--icon winmigrate.ico") == 2
    assert workflow.count("--version-file version_info.txt") == 2
