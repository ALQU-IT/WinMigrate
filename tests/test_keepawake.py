"""Holding off sleep while a long job runs.

A capture of a real profile runs for an hour or more; a laptop on default
settings sleeps at thirty minutes. The result is a bundle that stops partway
through with nothing on screen in the morning to explain it -- not a slow
backup, a missing one.
"""

from __future__ import annotations

import sys

from winmigrate import keepawake


class FakeKernel:
    """Stands in for kernel32, recording the flags it is handed."""

    def __init__(self, refuse_away_mode: bool = False, refuse_everything: bool = False):
        self.calls: list[int] = []
        self.refuse_away_mode = refuse_away_mode
        self.refuse_everything = refuse_everything

    def SetThreadExecutionState(self, flags: int) -> int:  # noqa: N802 -- Win32 name
        self.calls.append(flags)
        if self.refuse_everything:
            return 0
        if self.refuse_away_mode and flags & keepawake.ES_AWAYMODE_REQUIRED:
            return 0
        return flags


def install(monkeypatch, kernel: FakeKernel) -> None:
    import ctypes
    import types

    monkeypatch.setattr(sys, "platform", "win32")
    fake_ctypes = types.SimpleNamespace(windll=types.SimpleNamespace(kernel32=kernel))
    monkeypatch.setitem(sys.modules, "ctypes", fake_ctypes)
    assert ctypes  # the real one is restored by monkeypatch


def test_the_system_is_held_awake_but_the_screen_is_allowed_to_sleep(monkeypatch):
    """Someone starting a two-hour capture and walking away should come back to
    a dark screen and a finished bundle, not a monitor that burned all evening.
    """
    kernel = FakeKernel()
    install(monkeypatch, kernel)

    with keepawake.KeepAwake("capture") as awake:
        assert awake.held is True

    requested = kernel.calls[0]
    assert requested & keepawake.ES_SYSTEM_REQUIRED
    assert requested & keepawake.ES_CONTINUOUS
    assert not requested & keepawake.ES_DISPLAY_REQUIRED


def test_away_mode_is_asked_for_and_its_refusal_is_not_a_failure(monkeypatch):
    """Away mode is the better answer and is refused by plenty of hardware.
    A refusal must fall back to the ordinary request rather than giving up on
    staying awake altogether."""
    kernel = FakeKernel(refuse_away_mode=True)
    install(monkeypatch, kernel)

    with keepawake.KeepAwake() as awake:
        assert awake.held is True
        assert awake.away_mode is False

    assert kernel.calls[0] & keepawake.ES_AWAYMODE_REQUIRED  # asked
    assert not kernel.calls[1] & keepawake.ES_AWAYMODE_REQUIRED  # then without


def test_the_request_is_undone_even_when_the_job_fails(monkeypatch):
    """Leaving a machine unable to sleep because a backup crashed would be a
    rude thing to do to someone's laptop."""
    kernel = FakeKernel()
    install(monkeypatch, kernel)

    try:
        with keepawake.KeepAwake():
            raise RuntimeError("the capture failed")
    except RuntimeError:
        pass

    assert kernel.calls[-1] == keepawake.ES_CONTINUOUS  # cleared, nothing held


def test_a_refusal_to_stay_awake_never_fails_the_job(monkeypatch):
    """A backup must not fall over because it could not adjust a power setting."""
    kernel = FakeKernel(refuse_everything=True)
    install(monkeypatch, kernel)

    with keepawake.KeepAwake() as awake:
        assert awake.held is False
    # Nothing to undo, so nothing was cleared.
    assert keepawake.ES_CONTINUOUS not in kernel.calls[2:]


def test_an_exploding_ctypes_never_fails_the_job(monkeypatch):
    import types

    monkeypatch.setattr(sys, "platform", "win32")

    class Exploding:
        def __getattr__(self, name):
            raise OSError("no kernel32 here")

    monkeypatch.setitem(sys.modules, "ctypes", types.SimpleNamespace(windll=Exploding()))
    with keepawake.KeepAwake() as awake:
        assert awake.held is False


def test_it_does_nothing_at_all_off_windows(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with keepawake.KeepAwake() as awake:
        assert awake.held is False


def test_both_long_jobs_hold_it():
    """The two things that run for an hour are the two that need it."""
    import inspect

    from winmigrate import capture, restore

    assert "KeepAwake" in inspect.getsource(capture.capture)
    assert "KeepAwake" in inspect.getsource(restore.restore)
