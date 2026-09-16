"""The taskbar, the desktop layout and the Start menu."""

from __future__ import annotations

import base64
from pathlib import Path

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.models import Category, Kind
from winmigrate.platform_win import Environment
from winmigrate.scan import shell

BUILD_KEY = r"HKLM\SOFTWARE\Microsoft\Windows NT\CurrentVersion"
TASKBAND = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Taskband"
BAG = r"HKCU\Software\Microsoft\Windows\Shell\Bags\1\Desktop"


def env_with(root: Path, registry: dict | None = None) -> Environment:
    return Environment.fixture(root, registry or {})


# --- reading it -------------------------------------------------------------
def test_the_taskbar_needs_both_halves_to_mean_anything(tmp_path: Path):
    """Shortcut files under User Pinned say what the pins are; the Taskband blob
    says which of them are pinned and in what order. Carry one without the other
    and the taskbar does not change."""
    pinned = tmp_path / "AppData/Roaming/Microsoft/Internet Explorer/Quick Launch/User Pinned"
    (pinned / "TaskBar").mkdir(parents=True)
    (pinned / "TaskBar" / "Word.lnk").write_bytes(b"L\x00\x00\x00")
    env = env_with(tmp_path, {TASKBAND: {"Favorites": b"\x00\xff binary", "Unrelated": b"x"}})

    items, _ = shell.scan_shell(env)
    by_id = {item.id: item for item in items}

    assert by_id["shell:pinned_shortcuts"].kind is Kind.TREE
    assert by_id["shell:pinned_shortcuts"].category is Category.SHELL
    # The blob goes through JSON, so it travels base64-encoded and marked.
    carried = by_id["shell:taskbar"].record["values"]
    assert carried["Favorites"] == {"base64": base64.b64encode(b"\x00\xff binary").decode()}
    # And only the values the table names: the rest of that key is state
    # Explorer rewrites for itself.
    assert "Unrelated" not in carried


def test_desktop_icon_positions_travel_as_the_bytes_they_are(tmp_path: Path):
    env = env_with(tmp_path, {BAG: {"ItemPos1920x1080(1)": b"positions", "Mode": 1}})

    items, _ = shell.scan_shell(env)
    (item,) = [i for i in items if i.id == "shell:desktop_layout"]

    assert list(item.record["values"]) == ["ItemPos1920x1080(1)"]
    assert any("per screen size" in note.message for note in item.notes)


def test_the_start_menu_travels_with_the_windows_it_came_from(tmp_path: Path):
    """Its format changes between Windows releases, so the build number is the
    thing that makes it safe to put back."""
    layout = tmp_path / "AppData/Local" / Path(*shell.START_LAYOUT)
    layout.parent.mkdir(parents=True)
    layout.write_bytes(b"start layout")
    env = env_with(tmp_path, {BUILD_KEY: {"CurrentBuild": "22631"}})

    items, _ = shell.scan_shell(env)
    (item,) = [i for i in items if i.id == "shell:start_menu"]

    assert item.kind is Kind.FILE
    assert item.record["windows_build"] == "22631"


def test_a_profile_with_none_of_it_offers_nothing(tmp_path: Path):
    items, _ = shell.scan_shell(env_with(tmp_path))
    assert items == []


def test_a_blob_that_is_not_a_blob_does_not_come_back_as_one():
    assert shell.decode({"base64": "bm90aGluZw=="}) == b"nothing"
    assert shell.decode({"base64": "not base64 at all!"}) is None
    assert shell.decode({"nothing": 1}) is None
    assert shell.decode(None) is None
    assert shell.decode(7) == 7


# --- putting it back --------------------------------------------------------
def test_the_taskbar_is_written_back_byte_for_byte(tmp_path: Path):
    env = env_with(tmp_path)
    blob = base64.b64encode(b"\x01\x02pinned").decode()

    results = apply_mod.apply_shell_layout(
        {"values": {"Favorites": {"base64": blob}}}, None, None, env
    )

    assert env.registry[TASKBAND]["Favorites"] == b"\x01\x02pinned"
    assert results[0].outcome is Outcome.APPLIED


def test_a_record_naming_a_value_the_table_never_asked_for_is_refused(tmp_path: Path):
    """The record comes out of a bundle, which is a file from another machine."""
    env = env_with(tmp_path)

    apply_mod.apply_shell_layout(
        {"values": {
            "Favorites": {"base64": base64.b64encode(b"ok").decode()},
            "SomethingElse": {"base64": base64.b64encode(b"no").decode()},
        }},
        {"values": {"NotAPosition": {"base64": base64.b64encode(b"no").decode()}}},
        None,
        env,
    )

    assert list(env.registry[TASKBAND]) == ["Favorites"]
    assert BAG not in env.registry


def test_a_start_menu_from_another_windows_release_is_set_aside(tmp_path: Path):
    """A Start menu that has to rebuild itself is a nuisance. One
    half-transplanted from another Windows version is worse, and the person it
    happens to has no way to know why their computer looks broken."""
    restored = tmp_path / "start2.bin"
    restored.write_bytes(b"from the old release")
    env = env_with(tmp_path, {BUILD_KEY: {"CurrentBuild": "26100"}})

    (result,) = apply_mod.apply_shell_layout(
        None, None,
        {"windows_build": "22631", "restored_path": str(restored)},
        env,
    )

    assert result.outcome is Outcome.SKIPPED
    assert "22631" in result.detail and "26100" in result.detail
    assert not restored.exists()
    assert (tmp_path / "start2.bin.from-other-windows").read_bytes() == b"from the old release"


def test_a_start_menu_from_the_same_release_is_left_where_it_landed(tmp_path: Path):
    restored = tmp_path / "start2.bin"
    restored.write_bytes(b"layout")
    env = env_with(tmp_path, {BUILD_KEY: {"CurrentBuild": "22631"}})

    results = apply_mod.apply_shell_layout(
        None, None,
        {"windows_build": "22631", "restored_path": str(restored)},
        env,
    )

    (kept,) = [r for r in results if r.name == "your Start menu"]
    assert kept.outcome is Outcome.APPLIED
    assert restored.read_bytes() == b"layout"
    # And Explorer is restarted, because a Start menu nobody can see yet is the
    # same as one that did not come back.
    assert "Explorer" in [r.name for r in results]


def test_explorer_is_only_restarted_when_something_was_actually_written(tmp_path: Path):
    """A migration that finishes with the old taskbar still on screen reads as
    one that did not work -- but restarting Explorer for nothing is a flicker
    with no reason behind it."""
    env = env_with(tmp_path)

    nothing = apply_mod.apply_shell_layout({"values": {}}, None, None, env)
    assert [r.name for r in nothing] == ["your taskbar"]

    something = apply_mod.apply_shell_layout(
        {"values": {"Favorites": {"base64": base64.b64encode(b"x").decode()}}},
        None, None, env,
    )
    assert "Explorer" in [r.name for r in something]


def test_explorer_is_not_killed_on_the_machine_running_the_tests(tmp_path: Path):
    """The worst of the reach-past: a test that got this far on a Windows build
    machine would taskkill that machine's desktop."""
    env = env_with(tmp_path)
    env.is_windows = True  # as a test exercising a Windows path would

    results = apply_mod.apply_shell_layout(
        {"values": {"Favorites": {"base64": base64.b64encode(b"x").decode()}}},
        None, None, env,
    )

    (explorer,) = [r for r in results if r.name == "Explorer"]
    assert explorer.outcome is Outcome.SKIPPED
    assert explorer.detail == "not this machine"


# --- never leaving somebody without a desktop -------------------------------
class FakeWindows:
    """A machine where Explorer can be killed, and may or may not come back.

    Starting it goes through ``start`` rather than ``__call__``, because a
    program meant to outlive this one is spawned rather than run -- which is
    the difference between a restore that finishes and one that hangs for ever
    reading a pipe the shell is holding open.
    """

    def __init__(self, comes_back_after: int | None = 1):
        self.comes_back_after = comes_back_after
        self.calls: list[list[str]] = []
        self.starts = 0
        self.running = True

    def __call__(self, argv, timeout=None):
        from winmigrate.util.process import CommandResult

        self.calls.append(list(argv))
        if argv[0] == "taskkill":
            self.running = False
        if argv[0] == "tasklist":
            out = "explorer.exe   1234 Console   1   50,000 K" if self.running else ""
            return CommandResult(command=list(argv), returncode=0, stdout=out)
        return CommandResult(command=list(argv), returncode=0, stdout="")

    def start(self, argv):
        self.calls.append(list(argv))
        self.starts += 1
        if self.comes_back_after is not None and self.starts >= self.comes_back_after:
            self.running = True
        return None


def live_env(tmp_path: Path) -> Environment:
    """An environment that says it is a real Windows machine."""
    return Environment(profile_root=tmp_path, registry=None, is_windows=True)


def test_the_restart_waits_for_the_desktop_rather_than_walking_away(tmp_path: Path):
    """Killing Explorer is routine. Killing it and not looking back is not: the
    taskbar, the Start menu and the desktop gone, at the end of a migration, on
    a machine whose owner has every reason to think the program that just
    finished did it."""
    windows = FakeWindows(comes_back_after=1)

    result = apply_mod._restart_explorer(
        live_env(tmp_path), windows, pause=lambda _s: None, starter=windows.start
    )

    assert result.outcome is Outcome.APPLIED
    assert ["taskkill", "/f", "/im", "explorer.exe"] in windows.calls
    assert any(call[0] == "tasklist" for call in windows.calls)
    # Started, never run: waiting for the desktop to exit is waiting for the
    # user to log out.
    assert ["explorer.exe"] in windows.calls


def test_a_desktop_that_will_not_come_back_is_reported_as_the_failure_it_is(
    tmp_path: Path,
):
    """Not as a job done. The person is looking at a blank screen and needs the
    one instruction that fixes it, not a tick."""
    windows = FakeWindows(comes_back_after=None)

    result = apply_mod._restart_explorer(
        live_env(tmp_path), windows, pause=lambda _s: None, starter=windows.start
    )

    assert result.outcome is Outcome.FAILED
    assert "Ctrl+Shift+Esc" in result.detail and "explorer.exe" in result.detail
    # It kept trying rather than giving up after one go.
    assert windows.starts >= 2


def test_a_desktop_already_back_on_its_own_is_not_started_twice(tmp_path: Path):
    """Windows brings it back by itself in most configurations."""
    windows = FakeWindows()
    windows.running = True

    def already_back(argv, timeout=None):
        from winmigrate.util.process import CommandResult

        windows.calls.append(list(argv))
        if argv[0] == "tasklist":
            return CommandResult(command=list(argv), returncode=0, stdout="explorer.exe 1")
        return CommandResult(command=list(argv), returncode=0, stdout="")

    started: list = []
    result = apply_mod._restart_explorer(
        live_env(tmp_path), already_back, pause=lambda _s: None,
        starter=lambda argv: started.append(argv),
    )

    assert result.outcome is Outcome.APPLIED
    assert started == []
