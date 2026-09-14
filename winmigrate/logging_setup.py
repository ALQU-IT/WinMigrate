"""Logging: a real log file plus a quiet console.

The log is part of the product -- a migration you cannot audit afterwards is not
trustworthy. Two rules it must never break:

* secret material never reaches it (only ids, categories and sizes do), and
* it is written somewhere the user can find without being told: beside their
  chosen output on the command line, and beside the program itself for the
  window, which is the drive they started it from.
"""

from __future__ import annotations

import logging
import os
import platform
import sys
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from .util import paths as pathutil

UTC = timezone.utc

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
    if console is None and getattr(sys, "stderr", None) is None:
        # A --windowed build has no stderr at all. A StreamHandler over None
        # turns every log call into a handler error, so hand back a sink.
        return logging.NullHandler()
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


def writable_log_path(
    candidates: Iterable[os.PathLike[str] | str], name: str | None = None
) -> Path | None:
    """The first of ``candidates`` that will actually take a log file.

    Asking whether a directory is writable and then writing to it is two
    answers to one question, and on Windows they disagree often enough to
    matter: a USB stick mounted read-only, a folder under Program Files that
    quietly redirects, a network share that has gone away. So this writes --
    creating the file is the test -- and moves on to the next candidate when it
    cannot. Returns None only if none of them worked, which is survivable: the
    run goes on without a log rather than refusing to start.
    """
    stamped = name or f"winmigrate-{datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
    for candidate in candidates:
        path = Path(os.fspath(candidate)) / stamped
        try:
            pathutil.ensure_directory(path.parent)
            with open(pathutil.extended(path), "a", encoding="utf-8"):
                pass
        except OSError:
            continue
        return path
    return None


def _elevation_state() -> str:
    try:
        from . import vss  # noqa: PLC0415 -- the same check the capture makes

        if not vss.is_windows():
            return "not Windows"
        return "administrator" if vss.is_elevated() else "standard user"
    except Exception:  # noqa: BLE001 -- a banner must never stop a run
        return "unknown"


def log_start_banner(purpose: str, extra: Mapping[str, object] | None = None) -> None:
    """Write down where and when this run started, before it does anything.

    The first question about a run that went wrong -- or one that merely looked
    stuck -- is which copy of the program it was, started from where, by whom,
    and with what rights. None of that is reconstructable afterwards from a log
    that begins at the first warning, so it is written at the top of every one.

    Nothing here is secret: paths, names and flags only. The passphrase never
    reaches a command line (see :mod:`winmigrate.gui.elevate`), so ``sys.argv``
    is safe to record, and it is the single most useful line when a user says
    "I just ran it and nothing happened".
    """
    from . import __version__  # noqa: PLC0415 -- avoids an import cycle at module load

    now = datetime.now().astimezone()
    utc = now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    fields: dict[str, object] = {
        "version": __version__,
        "started": f"{now.isoformat(timespec='seconds')} (UTC {utc})",
        "purpose": purpose,
        "program": sys.executable,
        "started from": _program_directory(),
        "working directory": os.getcwd(),
        "command line": " ".join(sys.argv),
        "frozen build": "yes" if getattr(sys, "frozen", False) else "no",
        "rights": _elevation_state(),
        "user": _current_user(),
        "computer": platform.node(),
        "system": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "python": platform.python_version(),
        "process": os.getpid(),
    }
    fields.update(extra or {})
    banner = logging.getLogger("winmigrate.run")
    banner.info("--- WinMigrate run ---")
    width = max(len(key) for key in fields)
    for key, value in fields.items():
        banner.info("%s : %s", key.ljust(width), value)


def _program_directory() -> str:
    """The folder the running program lives in -- the .exe's, when frozen."""
    if getattr(sys, "frozen", False):
        return str(Path(sys.executable).resolve().parent)
    return str(Path(__file__).resolve().parent.parent)


def _current_user() -> str:
    try:
        import getpass  # noqa: PLC0415

        return getpass.getuser()
    except Exception:  # noqa: BLE001 -- no account name is not a failure
        return os.environ.get("USERNAME") or "unknown"
