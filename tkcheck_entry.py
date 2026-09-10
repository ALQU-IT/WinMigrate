"""Proves tkinter survived freezing.

--windowed turns an import error into a message box nobody sees on a CI runner,
so the check runs as a console build of the same imports and exits non-zero if
anything the window needs is missing.
"""

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    import tkinter
    import tkinter.filedialog  # noqa: F401
    import tkinter.messagebox  # noqa: F401
    import tkinter.ttk  # noqa: F401

    import importlib

    # Imported by name so the check is about the module loading, which is
    # what freezing breaks, rather than about a binding being used.
    importlib.import_module("winmigrate.gui.app")
    print(f"tkinter {tkinter.TkVersion} present; gui module imports")
