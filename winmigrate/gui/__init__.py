"""The desktop window.

Importing this package must not require tkinter: the frozen build, the tests and
the command line all import :mod:`winmigrate.gui.selection` and
:mod:`winmigrate.gui.defaults`, which are pure Python. Only :func:`run` reaches
for tkinter, and it says something useful when it is not there.
"""

from __future__ import annotations


def run() -> int:
    """Open the window. Returns a process exit code."""
    from .app import run as _run  # noqa: PLC0415 -- keeps tkinter out of import time

    return _run()


__all__ = ["run"]
