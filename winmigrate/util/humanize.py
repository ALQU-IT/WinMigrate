"""Human-readable formatting for sizes, counts and durations."""

from __future__ import annotations

_UNITS = ("B", "KiB", "MiB", "GiB", "TiB", "PiB")


def bytes_(n: int | float) -> str:
    """Format a byte count with binary units, e.g. ``1.4 GiB``."""
    value = float(n)
    if value < 1024:
        return f"{int(value)} B"
    for unit in _UNITS[1:]:
        value /= 1024.0
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:,.1f} {unit}"
    return f"{value:,.1f} {_UNITS[-1]}"


def count(n: int, singular: str, plural: str | None = None) -> str:
    """Pluralize ``n`` with thousands separators."""
    word = singular if n == 1 else (plural or singular + "s")
    return f"{n:,} {word}"


def duration(seconds: float) -> str:
    """Format a duration compactly, e.g. ``1m 04s``."""
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"
