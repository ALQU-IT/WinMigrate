"""PyInstaller entry point for the console build."""

from multiprocessing import freeze_support

if __name__ == "__main__":
    freeze_support()
    from winmigrate.cli import main

    raise SystemExit(main())
