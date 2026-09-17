"""Making the window behave like a Windows program rather than a script.

Four things separate an application from something that was obviously written
in a hurry, and all four are invisible when they are right:

* **It is not blurry.** Most Windows laptops now ship at 125% or 150% display
  scaling, and a process that has not said it understands that gets its window
  drawn small and then bitmap-stretched by the compositor. Every edge goes
  soft, every letter goes fuzzy. Worse here than in most programs, because the
  people most likely to be running at 150% are the people who set it that way
  to be able to read the screen.
* **It has its own icon.** Without one, the title bar and the taskbar show
  Tk's feather, and on a frozen build Windows groups the window under whatever
  executable started it.
* **It appears where you are looking**, in the middle of the screen, not
  wherever the window manager felt like.
* **Its text is the size you asked Windows for.** A tool that migrates
  somebody's text-size setting and then ignores it in its own window has
  missed its own point.

Every function here fails soft. None of it is load-bearing: a machine where
none of it works gets a window that is merely plain, which is what it had
before.
"""

from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)

#: Grouping identity for the taskbar. Without it Windows groups the window
#: under python.exe, with python.exe's icon, however carefully the window's own
#: icon was set.
APP_ID = "ALQU-IT.WinMigrate"

#: SetProcessDpiAwarenessContext, best first. Per-monitor v2 is what makes a
#: window redraw sharply when it is dragged to a second screen with different
#: scaling, rather than being stretched until it is let go.
_PER_MONITOR_AWARE_V2 = -4
_PER_MONITOR_AWARE = 2

#: Windows reports scaling against this: 96 dpi is 100%, 144 is 150%.
BASE_DPI = 96

#: Tk expresses scaling in pixels per point, and a point is 1/72 inch.
POINTS_PER_INCH = 72


def is_windows() -> bool:
    return sys.platform == "win32"


def make_dpi_aware() -> bool:
    """Tell Windows this process draws its own pixels. True when it took.

    Must run before the first window exists: Windows decides how to treat a
    process the first time it shows one, and will not be told afterwards.
    """
    if not is_windows():
        return False
    try:  # pragma: no cover -- Windows-only
        import ctypes

        user32 = ctypes.windll.user32
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            for context in (_PER_MONITOR_AWARE_V2, _PER_MONITOR_AWARE):
                if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(context)):
                    return True
        # Windows 8.1 and early 10. Returns non-zero on failure, including when
        # the process is already aware, which is not a failure worth reporting.
        ctypes.windll.shcore.SetProcessDpiAwareness(_PER_MONITOR_AWARE)
        return True
    except Exception as exc:  # noqa: BLE001 -- a blurry window still works
        log.info("could not become DPI aware: %s", exc)
        return False


def screen_dpi(root) -> float:
    """The DPI Tk sees for this screen, or the 96 that means 100%."""
    try:
        # Pixels across one inch, asked of Tk rather than of Windows, so the
        # number matches whatever Tk will actually draw with.
        return float(root.winfo_fpixels("1i"))
    except Exception:  # noqa: BLE001
        return float(BASE_DPI)


def text_scale_factor(env=None) -> float:
    """The "make text bigger" setting, as a multiplier. 1.0 when unset.

    This is the same value :mod:`winmigrate.scan.personalization` carries
    across, read here for the window's own text. Windows stores it as a
    percentage, and clamps its own slider to 100-225.
    """
    try:
        if env is None:
            from ..platform_win import Environment  # noqa: PLC0415

            env = Environment.live()
        raw = env.read_registry_value(
            "HKCU", r"Software\Microsoft\Accessibility", "TextScaleFactor"
        )
        percent = int(str(raw))
    except Exception:  # noqa: BLE001 -- an unset value is the common case
        return 1.0
    if percent < 100 or percent > 225:
        return 1.0
    return percent / 100.0


def apply_scaling(root, env=None) -> float:
    """Set Tk's scaling from the screen and the user's text size. Returns it.

    Tk sizes fonts in points and converts with this number, so setting it once
    makes every font, padding and border in the window come out right -- rather
    than each size being multiplied at the point it is used and one being
    missed.
    """
    dpi = screen_dpi(root)
    scale = (dpi / POINTS_PER_INCH) * text_scale_factor(env)
    try:
        root.tk.call("tk", "scaling", scale)
    except Exception as exc:  # noqa: BLE001
        log.info("could not set Tk scaling: %s", exc)
    log.info("display: %.0f dpi, text scale %.2f, tk scaling %.3f",
             dpi, text_scale_factor(env), scale)
    return scale


def set_taskbar_identity(app_id: str = APP_ID) -> bool:
    """Give the process its own taskbar identity. True when it took."""
    if not is_windows():
        return False
    try:  # pragma: no cover -- Windows-only
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
        return True
    except Exception as exc:  # noqa: BLE001
        log.info("could not set the taskbar identity: %s", exc)
        return False


def centre(root, width: int, height: int) -> str:
    """Put the window in the middle of the screen it is opening on.

    Returns the geometry string, so this is testable without a screen.
    """
    try:
        screen_width = int(root.winfo_screenwidth())
        screen_height = int(root.winfo_screenheight())
    except Exception:  # noqa: BLE001
        screen_width = screen_height = 0
    if screen_width <= 0 or screen_height <= 0:
        return f"{width}x{height}"
    left = max(0, (screen_width - width) // 2)
    # Slightly above centre: a window placed exactly in the middle reads as low,
    # because the eye treats the centre of a screen as higher than it is.
    top = max(0, (screen_height - height) // 3)
    geometry = f"{width}x{height}+{left}+{top}"
    try:
        root.geometry(geometry)
    except Exception as exc:  # noqa: BLE001
        log.info("could not place the window: %s", exc)
    return geometry


def set_icon(root, image_factory=None) -> bool:
    """Give the window its own icon. True when it took.

    ``image_factory`` builds the PhotoImage; it is injected so this is
    exercised without a display.
    """
    try:
        if image_factory is None:  # pragma: no cover -- needs a live Tk
            import tkinter as tk  # noqa: PLC0415

            from .icon import png_bytes  # noqa: PLC0415

            def image_factory():
                import base64  # noqa: PLC0415

                return tk.PhotoImage(data=base64.b64encode(png_bytes(64)))

        root.iconphoto(True, image_factory())
        return True
    except Exception as exc:  # noqa: BLE001 -- a default icon still works
        log.info("could not set the window icon: %s", exc)
        return False


# --- the window's own material ---------------------------------------------
#: ``DwmSetWindowAttribute`` attributes, by their numbers in dwmapi.h.
#:
#: Each arrived in a different Windows build and an older one rejects the
#: attribute it has never heard of with an HRESULT rather than crashing, which
#: is why every one of these can simply be attempted. On Windows 10 the corner
#: and colour attributes do nothing and the dark title bar still works; on
#: Windows 11 all of them do.
DWMWA_USE_IMMERSIVE_DARK_MODE = 20     # Windows 10 1809+
DWMWA_BORDER_COLOR = 34                # Windows 11 22000+
DWMWA_CAPTION_COLOR = 35
DWMWA_TEXT_COLOR = 36
DWMWA_SYSTEMBACKDROP_TYPE = 38         # Windows 11 22621+
DWMWA_WINDOW_CORNER_PREFERENCE = 33

#: ``DWM_WINDOW_CORNER_PREFERENCE``. 2 is the rounded corner Windows 11 gives
#: its own windows; 0 lets the system decide, which for a Tk window means
#: square.
DWMWCP_ROUND = 2

#: ``DWM_SYSTEMBACKDROP_TYPE``. Mica samples the desktop wallpaper and blurs it
#: behind the window -- the genuine article, done by the compositor rather than
#: imitated. It is the only real blur available to this program.
DWMSBT_MAINWINDOW = 2       # Mica
DWMSBT_TRANSIENTWINDOW = 3  # Acrylic


def _window_handle(root) -> int | None:
    """The HWND for a Tk window, or None if it has not got one yet.

    ``winfo_id`` on Windows returns the handle of Tk's *child* window, not the
    top-level one that has a title bar and a frame -- and DWM attributes set on
    a child are accepted and then do nothing at all, which is the most annoying
    way for this to fail. ``GetParent`` walks up to the real one.
    """
    if not is_windows():
        return None
    try:
        import ctypes  # noqa: PLC0415

        root.update_idletasks()  # the handle does not exist until it is realised
        child = int(root.winfo_id())
        parent = int(ctypes.windll.user32.GetParent(child))
        return parent or child
    except Exception as exc:  # noqa: BLE001
        log.debug("no window handle available: %s", exc)
        return None


def _colorref(colour: str) -> int:
    """``#rrggbb`` as the 0x00bbggrr integer DWM wants.

    Byte order reversed against every other colour in this program, which is
    the one thing to get wrong here: passing an RGB integer straight through
    silently swaps red and blue, and a blue title bar comes out orange.
    """
    from .glass import parse  # noqa: PLC0415

    red, green, blue = parse(colour)
    return (blue << 16) | (green << 8) | red


def _set_attribute(handle: int, attribute: int, value: int) -> bool:
    """One ``DwmSetWindowAttribute`` call. True when Windows accepted it."""
    try:
        import ctypes  # noqa: PLC0415
        from ctypes import wintypes  # noqa: PLC0415

        dwmapi = ctypes.windll.dwmapi
        dwmapi.DwmSetWindowAttribute.argtypes = [
            wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD
        ]
        dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
        # Every attribute used here takes a 4-byte value: a BOOL, an enum, or a
        # COLORREF. Declaring the size wrongly is how this call corrupts the
        # stack rather than politely failing.
        stored = ctypes.c_int(value)
        result = dwmapi.DwmSetWindowAttribute(
            wintypes.HWND(handle),
            wintypes.DWORD(attribute),
            ctypes.byref(stored),
            wintypes.DWORD(ctypes.sizeof(stored)),
        )
        return result == 0
    except Exception as exc:  # noqa: BLE001 -- an old build, or no dwmapi
        log.debug("DWM attribute %d not accepted: %s", attribute, exc)
        return False


def apply_window_material(root, palette) -> list[str]:
    """Make the frame Windows draws match the window it is drawn around.

    Tk gives every window the default title bar, which on a dark theme is a
    white strip above a dark page -- the single loudest thing on screen, and
    the giveaway that a window was not written for this operating system. The
    frame is not ours to paint, but it is ours to ask about, and Windows 11
    answers all of these.

    Returns the names of what it managed to set, for the log and for a test to
    read. Every part fails soft: a Windows 10 machine gets the dark title bar
    and nothing else, and one older still gets what it had before.
    """
    handle = _window_handle(root)
    if handle is None:
        return []

    applied: list[str] = []
    attempts: tuple[tuple[str, int, int], ...] = (
        # First, because on Windows 10 it is the only one that lands and it is
        # the one that matters most.
        ("dark-title-bar", DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if palette.dark else 0),
        ("rounded-corners", DWMWA_WINDOW_CORNER_PREFERENCE, DWMWCP_ROUND),
        # The caption is painted the colour of the wash directly beneath it, so
        # the frame stops being a separate strip and the window reads as one
        # object. This is what does most of the work of looking modern.
        ("caption-colour", DWMWA_CAPTION_COLOR, _colorref(palette.wash_top)),
        ("caption-text", DWMWA_TEXT_COLOR, _colorref(palette.ink)),
        ("border-colour", DWMWA_BORDER_COLOR, _colorref(palette.rule)),
        # Real blur, from the compositor, sampling the desktop behind the
        # window. The only genuine glass in the program -- everything inside
        # the window is computed, because nothing in Tk can blur.
        ("mica", DWMWA_SYSTEMBACKDROP_TYPE, DWMSBT_MAINWINDOW),
    )
    for name, attribute, value in attempts:
        if _set_attribute(handle, attribute, value):
            applied.append(name)
    log.info("window material: %s", ", ".join(applied) if applied else "none available")
    return applied
