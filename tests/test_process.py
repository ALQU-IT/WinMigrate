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


# --- streaming --------------------------------------------------------------
def _python(script: str) -> list[str]:
    import sys

    return [sys.executable, "-c", script]


def test_output_arrives_line_by_line_including_carriage_returns():
    """A console tool that shows progress rewrites one line with carriage
    returns. Waiting for a line feed that never comes shows nothing at all,
    which is a progress bar with nothing behind it."""
    lines: list[str] = []

    result = process.stream(
        _python("import sys; print('one'); sys.stdout.write('two\\rthree\\n')"),
        lines.append,
        timeout=30,
    )

    assert lines == ["one", "two", "three"]
    assert result.returncode == 0
    assert result.stdout.splitlines() == ["one", "two", "three"]


def test_stop_takes_effect_at_the_next_line_not_at_the_end():
    """A winget import runs for up to two hours. Stop that waits for it is not
    a stop."""
    seen: list[str] = []

    result = process.stream(
        _python("import time\nwhile True:\n    print('y', flush=True)\n    time.sleep(0.05)"),
        seen.append,
        timeout=60,
        cancelled=lambda: len(seen) >= 3,
    )

    assert result.error == "stopped"
    assert len(seen) < 20  # it really stopped rather than running to the timeout


def test_a_handler_that_raises_does_not_end_the_run():
    """The handler writes to a window. An install must not end because a label
    could not be updated."""
    def explode(line: str) -> None:
        raise RuntimeError("no window")

    result = process.stream(_python("print('hello')"), explode, timeout=30)

    assert result.returncode == 0
    assert "hello" in result.stdout


def test_a_program_that_never_ends_is_given_up_on():
    result = process.stream(
        _python("import time\nwhile True:\n    print('x', flush=True)\n    time.sleep(0.05)"),
        lambda line: None,
        timeout=1,
    )

    assert result.error and "timed out" in result.error


def test_a_missing_program_is_reported_rather_than_raised():
    result = process.stream(["winmigrate-not-a-real-program"], lambda line: None, timeout=5)

    assert result.unavailable


# --- starting something meant to outlive us ---------------------------------
def test_a_long_lived_program_is_started_rather_than_waited_for(tmp_path):
    """The bug this exists for looks like nothing at all in a log.

    run() captures output, so the child gets a pipe this process reads until
    end-of-file -- and end-of-file comes when every handle to the write end is
    closed, not when the child exits. Start a long-lived program through a
    launcher and the launcher exits while the program keeps the pipe, so the
    read never ends. Then the timeout fires, subprocess.run kills the child it
    started, and waits on that same pipe again with no timeout at all.

    Restoring a backup stopped there: taskkill returned, the launcher
    returned, Explorer held the pipe, and the log simply stopped.
    """
    import time

    marker = tmp_path / "started"
    # A launcher that exits at once, leaving a grandchild holding the handles.
    grandchild = (
        "import subprocess, sys; "
        f"subprocess.Popen([sys.executable, '-c', "
        f"\"import time; open(r'{marker}', 'w').close(); time.sleep(30)\"])"
    )

    began = time.monotonic()
    error = process.spawn(_python(grandchild))
    elapsed = time.monotonic() - began

    assert error is None
    # Back immediately, rather than in thirty seconds or never.
    assert elapsed < 5, f"spawn waited {elapsed:.1f}s"

    for _ in range(100):
        if marker.exists():
            break
        time.sleep(0.05)
    assert marker.exists(), "the program was never actually started"


def test_starting_something_that_is_not_there_is_reported_not_raised():
    error = process.spawn(["winmigrate-not-a-real-program"])
    assert error and "not found" in error


def test_stop_works_on_a_program_that_has_gone_quiet():
    """Reading inline blocks until something arrives, so Stop is not checked,
    the deadline is not checked, and a program that says nothing for an hour is
    an install page with a button that does nothing."""
    import time

    began = time.monotonic()
    result = process.stream(
        _python("import time; print('starting', flush=True); time.sleep(60)"),
        lambda line: None,
        timeout=120,
        cancelled=lambda: time.monotonic() - began > 1,
    )

    assert result.error == "stopped"
    assert time.monotonic() - began < 15


def test_the_deadline_is_honoured_by_a_silent_program():
    import time

    began = time.monotonic()
    result = process.stream(
        _python("import time; time.sleep(60)"), lambda line: None, timeout=1
    )

    assert result.error and "timed out" in result.error
    assert time.monotonic() - began < 15
