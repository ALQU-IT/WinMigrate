"""Logging: a real log file plus a quiet console.

The log is part of the product -- a migration you cannot audit afterwards is not
trustworthy. Two rules it must never break:

* secret material never reaches it (only ids, categories and sizes do), and
* it is written next to the user's chosen output, not somewhere hidden.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime
from pathlib import Path

from .util import paths as pathutil

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%dT%H:%M:%S"


class SecretRedactingFilter(logging.Filter):
    """Belt-and-braces: drop records explicitly marked as carrying secrets.

    Call sites should never log secret material in the first place; this exists
    so that a mistake fails closed. Mark a record with ``extra={"secret": True}``
    and it is dropped from every handler.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        return not getattr(record, "secret", False)


def _console_handler(console: object | None) -> logging.Handler:
    """A handler that cooperates with a live rich display, when there is one."""
    if console is not None:
        try:
            from rich.logging import RichHandler  # noqa: PLC0415

            return RichHandler(
                console=console,
                show_time=False,
                show_path=False,
                # Log text is not markup: a Windows path or an application name
                # containing brackets must not be parsed as a rich tag.
                markup=False,
                rich_tracebacks=False,
            )
        except ImportError:  # pragma: no cover -- rich is a hard dependency
            pass
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    return handler


def default_log_path(output_dir: os.PathLike[str] | str | None = None) -> Path:
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    directory = Path(os.fspath(output_dir)) if output_dir else Path.cwd()
    return directory / f"winmigrate-{stamp}.log"


def configure(
    log_file: os.PathLike[str] | str | None = None,
    verbose: bool = False,
    quiet: bool = False,
    console: object | None = None,
) -> Path | None:
    """Configure root logging. Returns the log file path, if one is in use.

    ``console`` is the rich Console the command prints through. A plain
    StreamHandler writes straight to stderr, which during a capture means
    writing *through* the live progress bar: the warning and the bar end up
    interleaved on one line and neither is readable. Handing logging the same
    Console lets rich place the message above the bar instead, which matters
    most exactly when it matters at all -- a warning during a long capture.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    redactor = SecretRedactingFilter()

    level = logging.ERROR if quiet else (logging.DEBUG if verbose else logging.WARNING)
    handler = _console_handler(console)
    handler.setLevel(level)
    handler.addFilter(redactor)
    root.addHandler(handler)

    if log_file is None:
        return None

    path = Path(os.fspath(log_file))
    pathutil.ensure_directory(path.parent)
    file_handler = logging.FileHandler(pathutil.extended(path), encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(LOG_FORMAT, DATE_FORMAT))
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)
    logging.getLogger(__name__).info("log started: %s", path)
    return path
