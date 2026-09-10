"""PyInstaller entry point for the windowed build.

A frozen app needs a plain script to freeze, not a console_scripts entry point,
and multiprocessing needs freeze_support() before anything else or a frozen
Windows build can relaunch itself in a loop.

Everything is wrapped, because a --windowed build has no console: an exception
that escapes here produces either nothing at all or PyInstaller's own crash
dialog quoting a traceback, and neither tells someone what to do about it.
"""

from multiprocessing import freeze_support


def _report(message: str) -> None:
    """Last-resort message for a build with nowhere to print."""
    import sys

    if sys.platform == "win32":
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(None, message, "WinMigrate", 0x10)
            return
        except Exception:
            pass
    print(message)


if __name__ == "__main__":
    freeze_support()
    try:
        from winmigrate.gui import run

        raise SystemExit(run())
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 -- nothing may escape a windowed build
        import traceback

        _report(
            "WinMigrate could not start.\n\n"
            "If you are running this from inside a .zip, extract the whole folder "
            "somewhere first and run WinMigrate.exe from there — the program needs "
            "the files next to it.\n\n"
            f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc()}"
        )
        raise SystemExit(1) from exc
