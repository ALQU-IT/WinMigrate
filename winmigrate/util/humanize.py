"""Human-readable formatting for sizes, counts and durations."""

from __future__ import annotations

#: Powers of 1024, labelled the way Windows labels them. Explorer calls 1024
#: bytes a KB and shows a 15 GB folder as "15.2 GB"; the same folder shown here
#: as "15.2 GiB" is correct and reads as a different number, or as a typo, to
#: anybody comparing the two -- which is exactly what somebody checking that
#: their photos all came across will do.
_UNITS = ("B", "KB", "MB", "GB", "TB", "PB")


def bytes_(n: int | float) -> str:
    """Format a byte count the way Explorer does, e.g. ``1.4 GB`` for 1.4 x 2**30."""
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
