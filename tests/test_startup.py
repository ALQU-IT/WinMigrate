"""What starts when you log in: the Startup folder, and the Run key."""

from __future__ import annotations

from pathlib import Path

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
