"""PyInstaller entry point for the windowed build.

A frozen app needs a plain script to freeze, not a console_scripts entry point,
and multiprocessing needs freeze_support() called before anything else or a
frozen Windows build can relaunch itself in a loop.
"""

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    from winmigrate.gui import run

    raise SystemExit(run())
