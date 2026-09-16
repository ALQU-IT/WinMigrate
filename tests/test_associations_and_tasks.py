"""Two things Windows deliberately will not let a program change.

Both resolve as reports rather than silent writes, which is why they sit
together: the boundary is the same one, and it is the right boundary.
"""

from __future__ import annotations

from pathlib import Path

from winmigrate.models import Category, Kind, RestoreStrategy
from winmigrate.platform_win import Environment
from winmigrate.scan import associations, tasks

FILE_EXTS = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Explorer"


def choice(suffix: str, prog_id: str) -> dict:
    key = (
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\FileExts"
        f"\\{suffix}\\UserChoice"
    )
    return {key: {"ProgId": prog_id}}


# --- which program opens which file -----------------------------------------
def test_the_everyday_choices_are_written_down(tmp_path: Path):
    """"Why does my PDF open in Edge now?" is one of the most common things
    somebody asks about a new computer, and one of the hardest to fix: the
    answer is several screens into Settings, once per file type, and you have to
    know the file type to look for."""
    registry = {**choice(".pdf", "AcroExch.Document.DC"), **choice(".mp3", "VLC.mp3")}
    env = Environment.fixture(tmp_path, registry)

    (item,), (followup,) = associations.scan_associations(env)

    assert item.category is Category.FILE_ASSOCIATIONS
    assert item.kind is Kind.REPORT
    assert item.record["associations"] == {
        ".pdf": "AcroExch.Document.DC", ".mp3": "VLC.mp3",
    }
    assert ".pdf opens with AcroExch.Document.DC" in followup.steps


def test_nothing_tries_to_set_them():
    """Windows protects a default-app choice with a hash over the file type, the
    user's SID and a timestamp. A program that writes the choice without the
    hash is ignored; one that forges it is doing exactly what the protection
    exists to stop."""
    import winmigrate.apply as apply_mod

    assert not any(
        name.startswith("apply_") and "association" in name for name in dir(apply_mod)
    )
    assert "UserChoice" not in Path("winmigrate/apply.py").read_text(encoding="utf-8")


def test_a_guided_strategy_is_what_says_the_tool_is_handing_off(tmp_path: Path):
    env = Environment.fixture(tmp_path, choice(".pdf", "AcroExch.Document.DC"))
    (item,), _ = associations.scan_associations(env)
    assert item.restore.strategy is RestoreStrategy.GUIDED


def test_the_hundreds_of_types_nobody_opens_by_hand_are_left_out(tmp_path: Path):
    """A profile accumulates hundreds of these, most for file types Windows only
    talks to itself about. A list of hundreds is a list nobody reads."""
    registry = {
        **choice(".pdf", "AcroExch.Document.DC"),
        **choice(".dwfx", "Windows.XPSReachViewer"),
        **choice(".jpg", "AppX43hnxtbyyps62jhe9sqpdzxn1790zetc"),
    }
    env = Environment.fixture(tmp_path, registry)

    (item,), _ = associations.scan_associations(env)

    # An everyday type, yes; an obscure one, no; and "whatever Windows ships"
    # says nothing worth repeating back.
    assert list(item.record["associations"]) == [".pdf"]


def test_a_profile_that_has_chosen_nothing_produces_nothing(tmp_path: Path):
    items, followups = associations.scan_associations(Environment.fixture(tmp_path, {}))
    assert items == [] and followups == []


# --- scheduled tasks --------------------------------------------------------
SCHTASKS_OUTPUT = '''"\\Microsoft\\Windows\\Defrag\\ScheduledDefrag","12/05/2026 03:00:00","Ready"
"\\Microsoft\\Windows\\UpdateOrchestrator\\Reboot","N/A","Disabled"
"\\Nightly Backup","16/09/2026 02:00:00","Ready"
"\\Photo sync","N/A","Disabled"
'''


def test_only_the_tasks_somebody_made_themselves_are_listed():
    """Windows runs hundreds of its own. Listing them would bury the two or
    three that are actually somebody's."""
    found = tasks.parse_tasks(SCHTASKS_OUTPUT)

    assert [task["name"] for task in found] == ["\\Nightly Backup", "\\Photo sync"]
    assert found[0]["status"] == "Ready"


def test_parsing_survives_empty_and_unexpected_output():
    assert tasks.parse_tasks("") == []
    assert tasks.parse_tasks("not,csv,really\n") == []


def test_tasks_are_reported_and_never_re_created(tmp_path: Path, monkeypatch):
    """A scheduled task is a command the machine runs on its own, unattended --
    a persistence mechanism as much as a convenience, and the one thing a backup
    tool should not add quietly from a file it was handed."""
    env = Environment.fixture(tmp_path, {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(tasks, "list_tasks", lambda: (
        [{"name": "\\Nightly Backup", "next_run": "", "status": "Ready"}], None
    ))

    (item,), (followup,) = tasks.scan_tasks(env)

    assert item.kind is Kind.REPORT
    assert item.restore.strategy is RestoreStrategy.GUIDED
    assert item.archive_path is None
    assert "\\Nightly Backup" in followup.steps

    import winmigrate.apply as apply_mod
    assert not hasattr(apply_mod, "apply_scheduled_tasks")


def test_a_machine_where_schtasks_will_not_run_says_nothing(tmp_path: Path, monkeypatch):
    """Not every machine allows it, and a migration is not the place to make an
    issue of that."""
    env = Environment.fixture(tmp_path, {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(tasks, "list_tasks", lambda: ([], "access denied"))

    assert tasks.scan_tasks(env) == ([], [])
