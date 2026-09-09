"""Exception hierarchy shared by every stage."""

from __future__ import annotations


class WinMigrateError(Exception):
    """Base class for all errors raised deliberately by WinMigrate."""


class PlatformError(WinMigrateError):
    """The tool is running somewhere it cannot do its job."""


class ScanError(WinMigrateError):
    """The scan stage could not produce a usable inventory."""


class ManifestError(WinMigrateError):
    """A manifest failed to build, serialize, or validate."""


class IntegrityError(WinMigrateError):
    """A hash or authentication tag did not match the recorded value."""


class ConfigError(WinMigrateError):
    """A configuration file or CLI option combination is not usable."""
