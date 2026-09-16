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
