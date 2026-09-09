"""Running external tools: decoding, and treating absence as information."""

from __future__ import annotations

import pytest

from winmigrate.util import process


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Name  Id\n7-Zip".encode("utf-16"), "Name  Id\n7-Zip"),
        ("Grüße".encode("cp1252"), "Grüße"),
        ("Grüße".encode("utf-8"), "Grüße"),
        ("plain".encode("utf-8-sig"), "plain"),
        (b"", ""),
        (None, ""),
    ],
)
def test_tool_output_is_decoded_however_windows_produced_it(raw, expected):
    """A mis-decode turns a parseable table into replacement characters."""
    assert process.decode(raw) == expected


def test_a_missing_tool_is_reported_rather_than_raised():
    result = process.run(["definitely-not-a-real-binary-xyz"])
    assert not result.ok
    assert result.unavailable
    assert "not found" in result.error


def test_a_command_that_runs_reports_its_output():
    result = process.run(["echo", "hello"])
    assert result.ok
    assert result.stdout.strip() == "hello"
    assert result.summary() == "ok"


def test_a_failing_command_summarises_its_first_error_line():
    result = process.run(["python3", "-c", "import sys; sys.stderr.write('boom\\n'); sys.exit(3)"])
    assert not result.ok
    assert not result.unavailable
    assert "exit 3" in result.summary()
    assert "boom" in result.summary()


def test_a_timeout_is_reported_as_an_error_not_a_hang():
    result = process.run(["python3", "-c", "import time; time.sleep(5)"], timeout=1)
    assert not result.ok
    assert "timed out" in result.error
