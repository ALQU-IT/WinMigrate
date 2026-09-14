"""The log a run leaves behind: where it goes, what it opens with, what it omits.

This exists because of a real half hour. A capture of a 15 GiB profile sat at
"5.0 MiB of 15.2 GiB" while Windows made a shadow copy, the window said nothing
about that, and there was no log to look at afterwards -- so the only available
reading was that the tool had hung. Two things follow from that: a run says in
writing where and when it started, and the waiting it does before the first byte
is named rather than left blank.
"""

from __future__ import annotations

import logging
from pathlib import Path

from winmigrate import logging_setup


def read_log(path: Path) -> str:
    # Never read_text() without an encoding: on a German Windows the default is
    # cp1252 and a log holding one path with an umlaut in it stops being
    # readable at all. tests/test_portability.py guards the product code.
    return path.read_text(encoding="utf-8")


def test_a_directory_that_will_not_take_a_file_is_skipped_for_the_next(tmp_path: Path):
    """A read-only USB stick, a folder that redirects, a share that has gone
    away: asking whether a directory is writable and then writing to it are two
    answers to one question. Writing is the only test that agrees with itself."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    fallback = tmp_path / "second"

    path = logging_setup.writable_log_path([blocked / "inside", fallback])

    assert path is not None
    assert path.parent == fallback
    assert path.exists()


def test_no_writable_candidate_at_all_is_survivable(tmp_path: Path):
    """Without a log the run still has to start. A backup tool that refuses to
    open because it cannot write its own log has its priorities backwards."""
    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")

    assert logging_setup.writable_log_path([blocked / "one", blocked / "two"]) is None


def test_the_log_opens_by_saying_where_and_when_the_run_started(tmp_path: Path):
    """The first question about a run that went wrong -- or one that merely
    looked stuck -- is which copy of the program it was, started from where, by
    whom, and with what rights. None of that is reconstructable afterwards."""
    path = tmp_path / "run.log"
    logging_setup.configure(path)
    try:
        logging_setup.log_start_banner("window", {"elevation attempted": "no"})
    finally:
        logging.getLogger().handlers.clear()

    text = read_log(path)
    for field in ("version", "started", "purpose", "started from", "command line", "rights"):
        assert field in text, field
    assert "window" in text
    assert "elevation attempted : no" in text


def test_nothing_marked_secret_reaches_the_log(tmp_path: Path):
    """The standing rule of the project: secret material goes into the encrypted
    bundle and nowhere else. The filter is the backstop for a call site that
    forgets, so it is checked on the file handler, not only in principle."""
    path = tmp_path / "run.log"
    logging_setup.configure(path)
    try:
        logging.getLogger("winmigrate.test").warning(
            "passphrase is %s", "hunter2", extra={"secret": True}
        )
        logging.getLogger("winmigrate.test").warning("ordinary line")
    finally:
        logging.getLogger().handlers.clear()

    text = read_log(path)
    assert "hunter2" not in text
    assert "ordinary line" in text


def test_the_window_logs_beside_the_program_it_was_started_from(monkeypatch, tmp_path: Path):
    """Run from a USB stick, the log belongs on the USB stick: the drive the
    user is holding, next to the backup. Not in a temp folder they would have to
    be told how to find."""
    from winmigrate.gui import runlog

    stick = tmp_path / "E_drive"
    stick.mkdir()
    monkeypatch.setattr(runlog.defaults, "program_directory", lambda: stick)

    try:
        path = runlog.begin("window", {"elevation attempted": "no"})
    finally:
        logging.getLogger().handlers.clear()

    assert path is not None and path.parent == stick
    assert path.name.startswith("winmigrate-") and path.suffix == ".log"
    assert "purpose" in read_log(path)


def test_the_log_falls_back_when_the_program_sits_somewhere_unwritable(
    monkeypatch, tmp_path: Path
):
    """Started from inside a .zip, or a folder that redirects: the fallback is
    the local application data folder, and then the temporary folder."""
    from winmigrate.gui import runlog

    blocked = tmp_path / "afile"
    blocked.write_text("not a directory", encoding="utf-8")
    local = tmp_path / "LocalAppData"
    monkeypatch.setattr(runlog.defaults, "program_directory", lambda: blocked / "inside")
    monkeypatch.setenv("LOCALAPPDATA", str(local))

    try:
        path = runlog.begin()
    finally:
        logging.getLogger().handlers.clear()

    assert path is not None
    assert path.parent == local / "WinMigrate"


def test_the_shadow_copy_wait_is_announced_before_it_starts(monkeypatch, tmp_path: Path):
    """The minutes Windows spends making a snapshot come before the first byte
    is written, so the bar reads zero throughout and used to say nothing at all.
    The notice has to be emitted *before* the call that blocks, or it arrives
    after the wait it was meant to explain."""
    from winmigrate import capture as capture_mod
    from winmigrate.capture import CaptureOptions, CaptureReport

    events: list[str] = []

    class FakeShadow:
        def remove(self):
            pass

    monkeypatch.setattr(capture_mod.vss, "is_windows", lambda: True)
    monkeypatch.setattr(capture_mod.vss, "volume_of", lambda path: "C:")
    monkeypatch.setattr(
        capture_mod.vss, "create", lambda volume: (events.append("create"), FakeShadow())[1]
    )
    monkeypatch.setattr(capture_mod.vss, "usable_for", lambda shadow, probe: True)

    class FakeSource:
        profile_path = Path("C:/Users/alessio")

    class FakeScan:
        source = FakeSource()

    report = CaptureReport(
        bundle_path=tmp_path / "b.dat", manifest_path=tmp_path / "b.manifest.json"
    )
    options = CaptureOptions(output=tmp_path / "b.dat", passphrase="pw", use_vss=True)

    def progress(title: str, size: int) -> None:
        events.append(f"progress:{title}:{size}")

    shadow = capture_mod._open_shadow_copy(FakeScan(), options, report, progress)

    assert shadow is not None and report.used_shadow_copy
    assert events[0].startswith("progress:")
    assert "shadow copy" in events[0].lower()
    # ASCII: the same line goes to a console whose code page is often cp1252.
    assert events[0].isascii()
    # The notice must not move the bar: nothing has been captured yet.
    assert events[0].endswith(":0")
    assert events[1] == "create"


def test_old_logs_are_tidied_but_only_this_program_s_own(tmp_path: Path):
    """A run leaves a log every time, including the launch that only asked for
    administrator rights and closed. What it must never tidy away is a file that
    is merely named similarly and belongs to someone else."""
    from winmigrate.gui import runlog

    for day in range(1, 26):
        (tmp_path / f"winmigrate-202609{day:02d}-120000.log").write_text("x", encoding="utf-8")
    mine = tmp_path / "winmigrate-notes.log"
    mine.write_text("mine", encoding="utf-8")
    bundle = tmp_path / "desktop-alessio-20260901-120000.dat"
    bundle.write_text("bundle", encoding="utf-8")

    removed = runlog.prune(tmp_path, keep=20)

    left = sorted(path.name for path in tmp_path.iterdir())
    assert len(removed) == 5
    assert "winmigrate-20260901-120000.log" not in left  # oldest went
    assert "winmigrate-20260925-120000.log" in left      # newest stayed
    assert mine.name in left and bundle.name in left
