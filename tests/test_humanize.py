"""Numbers as the person reading them expects to see them."""

from __future__ import annotations

import pytest

from winmigrate.util import humanize


@pytest.mark.parametrize(
    "n, expected",
    [
        (0, "0 B"),
        (1023, "1023 B"),
        (1024, "1.0 KB"),
        (1536, "1.5 KB"),
        (15 * 1024**3 + 200 * 1024**2, "15.2 GB"),
        (2 * 1024**4, "2.0 TB"),
    ],
)
def test_sizes_read_the_way_explorer_shows_them(n, expected):
    """Explorer counts in 1024s and calls the result KB, MB, GB. A folder it
    shows as "15.2 GB" shown here as "15.2 GiB" is correct and reads as a
    different number -- to exactly the person comparing the two to check that
    their photos came across."""
    assert humanize.bytes_(n) == expected


def test_the_largest_unit_does_not_run_out():
    assert humanize.bytes_(5 * 1024**6).endswith(" PB")
