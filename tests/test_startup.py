"""What starts when you log in: the Startup folder, and the Run key."""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.models import Category, Kind
from winmigrate.platform_win import Environment
from winmigrate.scan import startup


def env_with(root: Path, registry: dict | None = None) -> Environment:
    return Environment.fixture(root, registry or {})


def a_startup_folder(root: Path) -> Path:
    folder = root / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup"
    folder.mkdir(parents=True)
    return folder


# --- reading it -------------------------------------------------------------
def test_the_startup_folder_travels_although_appdata_does_not(tmp_path: Path):
    """It lives under AppData\\Roaming\\Microsoft\\Windows\\Start Menu, which the
    file scan excludes wholesale -- so a restored machine logs in to a bare
    desktop and the things somebody expects to be running are not."""
    folder = a_startup_folder(tmp_path)
    (folder / "Backup Agent.lnk").write_bytes(b"L\x00\x00\x00shortcut")

    items, _ = startup.scan_startup(env_with(tmp_path))
    (item,) = [i for i in items if i.id == "settings:startup_folder"]

    assert item.kind is Kind.TREE
    assert item.category is Category.STARTUP
    # Profile-relative, so it lands in the new profile rather than the old one's
    # user name.
    assert item.archive_path == (
        "data/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    )


def test_an_empty_startup_folder_is_not_an_item(tmp_path: Path):
    a_startup_folder(tmp_path)
    items, _ = startup.scan_startup(env_with(tmp_path))
    assert [i.id for i in items] == []


def test_the_run_key_travels_and_runonce_does_not(tmp_path: Path):
    """RunOnce is a queue of things waiting to happen once on *that* machine --
    half-finished installers, pending reboots. Replaying somebody else's pending
    reboot on a new computer is not migration."""
    env = env_with(tmp_path, {
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run": {
            "OneDrive": r'"C:\Program Files\Microsoft OneDrive\OneDrive.exe" /background',
            "MouseTool": r"C:\Tools\mouse.exe",
        },
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\RunOnce": {
            "FinishSetup": r"C:\Windows\Temp\halfway.exe",
        },
    })

    items, _ = startup.scan_startup(env)
    (item,) = [i for i in items if i.id == "settings:startup_run"]

    assert set(item.record["entries"]) == {"OneDrive", "MouseTool"}
    assert "FinishSetup" not in item.record["entries"]


def test_the_program_is_found_in_a_command_line_either_way(tmp_path: Path):
    """Windows accepts a quoted path and an unquoted one, which is ambiguous by
    construction: the space could separate the program from its arguments or be
    part of the folder name."""
    assert startup.executable_of(r'"C:\Program Files\App\app.exe" --quiet') == (
        r"C:\Program Files\App\app.exe"
    )
    assert startup.executable_of(r"C:\Tools\mouse.exe -min") == r"C:\Tools\mouse.exe"
    assert startup.executable_of("") == ""

    # An unquoted path with a space in it is grown a word at a time until it
    # names something real, which is what Windows itself does.
    folder = tmp_path / "My Tools"
    folder.mkdir()
    program = folder / "helper.exe"
    program.write_bytes(b"MZ")
    assert startup.executable_of(f"{program} --now") == str(program)


# --- putting it back --------------------------------------------------------
def test_an_entry_whose_program_is_missing_is_not_restored_broken(tmp_path: Path):
    """A dead Run entry is an error box at every login, for ever, naming a path
    the user has never seen."""
    env = env_with(tmp_path)

    (result,) = apply_mod.apply_startup(
        {"entries": {"OldTool": r"C:\Gone\tool.exe"}}, env
    )

    assert result.outcome is Outcome.SKIPPED
    assert "not on this machine" in result.detail
    assert env.registry.get(r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run") is None


def test_each_entry_is_named_as_it_is_restored(tmp_path: Path):
    """A Run entry is a command Windows executes at every login. In a tool whose
    premise is showing its work, that is the one setting worth seeing restored
    one at a time rather than written quietly and counted."""
    program = tmp_path / "mouse.exe"
    program.write_bytes(b"MZ")
    env = env_with(tmp_path)

    results = apply_mod.apply_startup(
        {"entries": {"MouseTool": f'"{program}" -min', "Gone": r"C:\Gone\x.exe"}}, env
    )

    named = {r.name: r.outcome for r in results}
    assert named == {"MouseTool": Outcome.APPLIED, "Gone": Outcome.SKIPPED}
    run = env.registry[r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run"]
    assert run == {"MouseTool": f'"{program}" -min'}


def test_a_record_that_is_not_a_record_does_nothing(tmp_path: Path):
    assert apply_mod.apply_startup({}, env_with(tmp_path)) == []
    assert apply_mod.apply_startup({"entries": "nonsense"}, env_with(tmp_path)) == []


# --- the program is not here *yet* ------------------------------------------
@pytest.mark.parametrize(
    "command, expected",
    [
        # The one that was breaking: an unquoted path with a space in it, on a
        # machine where the program is not installed yet. Growing a word at a
        # time finds nothing, and "the first word" is C:\Program.
        (r"C:\Program Files\Notepad++\notepad++.exe",
         r"C:\Program Files\Notepad++\notepad++.exe"),
        (r"C:\Program Files\App\app.exe --quiet", r"C:\Program Files\App\app.exe"),
        (r'"C:\Program Files\App\app.exe" --quiet', r"C:\Program Files\App\app.exe"),
        # The first executable is the program; what follows is its argument.
        (r"C:\Windows\system32\cmd.exe /c C:\other\thing.exe",
         r"C:\Windows\system32\cmd.exe"),
        (r"rundll32.exe shell32.dll,Control_RunDLL", "rundll32.exe"),
        # Nothing that looks like a program: the old fallback is still the best
        # answer available.
        (r"C:\Tools\runner --flag", r"C:\Tools\runner"),
        ("", ""),
    ],
)
def test_the_program_is_found_even_when_it_is_not_installed_yet(command, expected):
    """Every Run entry is put back before any software is installed, so the
    "grow until it exists" reading finds nothing and falls through. Falling
    through to the first word turned "C:\\Program Files\\..." into
    "C:\\Program", which will never exist on any machine -- so the entry was
    reported as a program the user does not have, for ever."""
    assert startup.executable_of(command) == expected


def test_a_real_path_still_wins_over_the_shape_of_one(tmp_path):
    """When the program is here, its presence is the exact answer and beats
    guessing where the arguments start."""
    program = tmp_path / "my app.exe.thing"
    program.write_bytes(b"MZ")
    assert startup.executable_of(f"{program} --flag") == str(program)


def test_the_login_entries_are_asked_about_again_once_the_software_is_there(tmp_path):
    """The bug in one sentence: a restore writes the login entries before it
    installs anything, so every one of them is skipped as a program this
    machine does not have -- and nothing ever asks again.

    The entries were right. The question was early.
    """
    profile = tmp_path / "u"
    profile.mkdir()
    env = Environment.fixture(profile, {})
    program = tmp_path / "Notepad++" / "notepad++.exe"
    record = {"entries": {"Notepad++": str(program), "Gone": r"C:\nowhere\gone.exe"}}

    # Restore time: the installer has not run, so neither program is here.
    first = apply_mod.apply_startup(record, env)
    assert {r.name: r.outcome for r in first} == {
        "Notepad++": Outcome.SKIPPED, "Gone": Outcome.SKIPPED
    }
    # And it does not say so as though it were the final word.
    assert all("yet" in r.detail for r in first)

    # The install runs, and one of the two arrives.
    program.parent.mkdir(parents=True)
    program.write_bytes(b"MZ")

    fresh = apply_mod.retry_startup(record, first, env)

    assert [r.name for r in fresh] == ["Notepad++"]
    assert fresh[0].outcome is Outcome.APPLIED
    written = env.registry.get(f"HKCU\\{startup.RUN_KEY}", {})
    assert written == {"Notepad++": str(program)}, "the Run value was never written"


def test_the_second_look_leaves_alone_what_it_cannot_change(tmp_path):
    """A program that is still missing after the install is still missing.
    Returning it again would put a duplicate line in the report saying nothing
    new."""
    profile = tmp_path / "u"
    profile.mkdir()
    env = Environment.fixture(profile, {})
    record = {"entries": {"Gone": r"C:\nowhere\gone.exe"}}

    first = apply_mod.apply_startup(record, env)
    assert apply_mod.retry_startup(record, first, env) == []


def test_nothing_is_re_asked_when_nothing_was_skipped(tmp_path):
    """No install, no second look: retrying an entry that already applied would
    rewrite a registry value for no reason."""
    profile = tmp_path / "u"
    profile.mkdir()
    env = Environment.fixture(profile, {})
    assert apply_mod.retry_startup({"entries": {}}, [], env) == []


def test_the_later_answer_replaces_the_earlier_one_in_the_report():
    """Otherwise the report carries both "not on this machine" and "restored"
    for the same program, and the reader cannot tell which line came last."""
    from winmigrate.apply import Result

    applied = [
        Result("startup", "Notepad++", Outcome.SKIPPED, "not here yet"),
        Result("startup", "Gone", Outcome.SKIPPED, "not here yet"),
        Result("printer", "Notepad++", Outcome.APPLIED, "a different kind, left alone"),
    ]
    fresh = [Result("startup", "Notepad++", Outcome.APPLIED, "C:\\np\\notepad++.exe")]

    settled = apply_mod.supersede(applied, fresh)

    startup_rows = [(r.name, r.outcome) for r in settled if r.kind == "startup"]
    assert startup_rows == [("Gone", Outcome.SKIPPED), ("Notepad++", Outcome.APPLIED)]
    # A result of another kind that happens to share a name is not touched.
    assert any(r.kind == "printer" and r.outcome is Outcome.APPLIED for r in settled)
