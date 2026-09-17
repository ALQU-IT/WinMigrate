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


#: Commands that need a console to be worth running. A windowed build has
#: none: its output goes nowhere, so "reinstall --apps" would sit there for
#: twenty minutes showing the user nothing at all.
CONSOLE_BUILD = "winmigrate-cli.exe"


def _window_arguments(argv: list[str]):
    """What this window was asked to open as, or a message saying it cannot.

    The windowed build used to ignore its arguments entirely, which broke two
    things quietly.

    Running "WinMigrate.exe reinstall <folder> --apps" -- the command the
    restore report prints -- opened the wizard on its first page instead, which
    is an offer to back this machine up. Somebody who came to reinstall their
    software and was handed a backup wizard has been answered with the wrong
    question, and the dangerous part is that it looks like a working program.

    And elevation relaunches this executable with the first page's choices on
    the command line, so that the elevated window comes back looking exactly
    like the one that disappeared. Ignoring them meant it came back on page one
    offering a backup, to somebody halfway through a restore.
    """
    from winmigrate.cli import build_parser

    if not argv:
        return {}, ""
    if argv[0] != "gui":
        return None, (
            f"This is the WinMigrate window, which has nowhere to print to.\n\n"
            f"To run '{argv[0]}' from a command line, use {CONSOLE_BUILD} in the "
            f"same folder:\n\n    {CONSOLE_BUILD} " + " ".join(argv)
        )
    try:
        args = build_parser().parse_args(argv)
    except SystemExit:
        # argparse prints usage to a console this build has not got.
        return None, (
            "WinMigrate did not understand those options.\n\n"
            f"Run {CONSOLE_BUILD} --help in the same folder to see them."
        )
    return {
        "mode": args.mode,
        "restore_as_admin": args.restore_as_admin,
        "profile_root": str(args.profile_root) if args.profile_root else "",
        "files_only": args.files_only,
        "include_wifi": args.include_wifi,
        "include_software": args.include_software,
        "include_notepad": args.include_notepad,
        "compression": args.compression,
        "elevation_attempted": args.elevation_attempted,
        "log_file": str(args.log_file) if args.log_file else "",
    }, ""


def main(argv: list[str] | None = None, opener=None) -> int:
    """Open the window for these arguments, or explain why it will not.

    A function rather than a block under ``__main__`` so that the wiring itself
    can be tested. It could not be before, and what it was wired to was
    ``run()`` with no arguments at all -- every option this file now reads was
    being parsed by nobody.
    """
    import sys  # noqa: PLC0415

    if argv is None:
        argv = sys.argv[1:]
    if opener is None:  # pragma: no cover -- the live path
        from winmigrate.gui import run as opener  # noqa: PLC0415

    options, complaint = _window_arguments(argv)
    if options is None:
        _report(complaint)
        return 2
    return opener(options)


if __name__ == "__main__":
    freeze_support()
    try:
        raise SystemExit(main())
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
