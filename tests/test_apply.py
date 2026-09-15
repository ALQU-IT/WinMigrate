"""Re-applying the settings that do not need a person.

The consent boundary in this tool is about *identity* -- account sign-ins,
licence activation, a password store that wants Windows Hello. Four things had
drifted onto the follow-up list that need no identity at all and are one command
each. Re-adding eleven printers by hand is not consent, it is tedium.
"""

from __future__ import annotations

from pathlib import Path

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.platform_win import Environment
from winmigrate.util.process import CommandResult


def recording_runner(fail_on: str = "", explode_on: str = ""):
    """A process runner that records commands and can be told to fail."""
    calls: list[list[str]] = []

    def run(command, timeout=120, input_text=None):
        calls.append(command)
        joined = " ".join(command)
        if explode_on and explode_on in joined:
            raise OSError("the command could not be started")
        if fail_on and fail_on in joined:
            return CommandResult(command=command, returncode=1, stdout="", stderr="it refused")
        return CommandResult(command=command, returncode=0, stdout="ok", stderr="")

    run.calls = calls
    return run


# --- Wi-Fi -----------------------------------------------------------------
def test_wifi_profiles_are_imported_for_this_user_only(tmp_path: Path):
    """The profiles came out of one account and belong back in one account.
    Importing them machine-wide would hand every user of the new computer the
    network passwords from the old one."""
    folder = tmp_path / "WinMigrate-WiFi"
    folder.mkdir()
    for name in ("HomeNet", "OfficeNet"):
        (folder / f"{name}.xml").write_text("<x/>", encoding="utf-8")

    runner = recording_runner()
    results = apply_mod.apply_wifi(folder, runner)

    assert [r.outcome for r in results] == [Outcome.APPLIED, Outcome.APPLIED]
    for command in runner.calls:
        assert "user=current" in command
        assert "user=all" not in " ".join(command)


def test_one_network_that_refuses_does_not_stop_the_others(tmp_path: Path):
    folder = tmp_path / "WinMigrate-WiFi"
    folder.mkdir()
    for name in ("Good", "Bad", "AlsoGood"):
        (folder / f"{name}.xml").write_text("<x/>", encoding="utf-8")

    results = {r.name: r for r in apply_mod.apply_wifi(folder, recording_runner(fail_on="Bad"))}
    assert results["Bad"].outcome is Outcome.FAILED
    assert results["Good"].outcome is Outcome.APPLIED
    assert results["AlsoGood"].outcome is Outcome.APPLIED


def test_a_command_that_cannot_even_start_is_a_result_not_an_exception(tmp_path: Path):
    """A restore that has already written every file must not fall over on a
    missing netsh."""
    folder = tmp_path / "WinMigrate-WiFi"
    folder.mkdir()
    (folder / "Net.xml").write_text("<x/>", encoding="utf-8")
    results = apply_mod.apply_wifi(folder, recording_runner(explode_on="netsh"))
    assert results[0].outcome is Outcome.FAILED


def test_no_wifi_folder_means_no_results(tmp_path: Path):
    assert apply_mod.apply_wifi(tmp_path / "nothing") == []


# --- printers --------------------------------------------------------------
def test_network_printers_are_added_and_local_ones_are_left_for_a_person():
    """A locally attached printer is a driver and a USB cable, neither of which
    a backup can produce."""
    record = {"connections": ["\\\\srv\\Laser", "HP LaserJet on USB001"], "default": None}
    results = {r.name: r for r in apply_mod.apply_printers(record, recording_runner())}

    assert results["\\\\srv\\Laser"].outcome is Outcome.APPLIED
    local = results["HP LaserJet on USB001"]
    assert local.outcome is Outcome.SKIPPED
    assert "driver" in local.detail


def test_the_default_printer_is_only_set_if_it_was_added():
    """Setting a default that failed to install would leave the machine pointing
    at a printer that is not there."""
    record = {"connections": ["\\\\srv\\Broken"], "default": "\\\\srv\\Broken"}
    results = apply_mod.apply_printers(record, recording_runner(fail_on="Broken"))
    assert not any("default" in r.name for r in results)

    record = {"connections": ["\\\\srv\\Laser"], "default": "\\\\srv\\Laser"}
    results = apply_mod.apply_printers(record, recording_runner())
    assert any("default" in r.name and r.outcome is Outcome.APPLIED for r in results)


# --- mapped drives ---------------------------------------------------------
def test_drives_are_mapped_persistently_and_without_credentials():
    """No password was captured and none is supplied. A share that needs one
    prompts the user the first time they open it, which is the operating system
    asking for an identity -- the line this tool does not cross."""
    runner = recording_runner()
    results = apply_mod.apply_mapped_drives({"drives": {"Z": "\\\\srv\\share"}}, runner)

    assert results[0].outcome is Outcome.APPLIED
    command = " ".join(runner.calls[0])
    assert "/persistent:yes" in command
    assert "/user" not in command and "password" not in command.lower()


def test_a_drive_letter_without_a_colon_still_works():
    runner = recording_runner()
    apply_mod.apply_mapped_drives({"drives": {"Z": "\\\\srv\\share"}}, runner)
    assert "Z:" in runner.calls[0]


def test_anything_that_is_not_a_unc_path_is_not_mapped():
    runner = recording_runner()
    results = apply_mod.apply_mapped_drives({"drives": {"C": "C:\\local"}}, runner)
    assert results == [] and runner.calls == []


# --- environment variables -------------------------------------------------
def test_the_users_own_variables_are_written():
    env = Environment.fixture(Path("/tmp/p"), {})
    record = {"variables": {"EDITOR": "code", "JAVA_HOME": "C:\\jdk"}}
    results = {r.name: r for r in apply_mod.apply_environment(record, env)}

    assert results["EDITOR"].outcome is Outcome.APPLIED
    assert env.registry["HKCU\\Environment"] == {"EDITOR": "code", "JAVA_HOME": "C:\\jdk"}


def test_path_is_never_overwritten_only_added_to(tmp_path: Path):
    """PATH reads like a user setting and is really two things: where software
    happened to live on the old disk, and entries somebody added on purpose.
    Replacing this machine's copy breaks every tool that is not in the same
    place; leaving it out means re-adding the deliberate ones from memory. So
    this machine's entries stay, in order and first, and the old ones join them
    where the folder actually exists here."""
    travelled = tmp_path / "tools"
    travelled.mkdir()
    env = Environment.fixture(
        tmp_path, {"HKCU\\Environment": {"Path": r"C:\mine\bin"}}, environ={"PATH": ""}
    )
    record = {"variables": {"PATH": f"C:\\old\\gone;{travelled}", "EDITOR": "code"}}

    results = apply_mod.apply_environment(record, env)
    by_name = {r.name: r for r in results}

    assert env.registry["HKCU\\Environment"]["Path"] == f"C:\\mine\\bin;{travelled}"
    assert by_name["PATH"].outcome is Outcome.APPLIED
    assert str(travelled) in by_name["PATH"].detail
    # And the one that could not come is named, because that is the part nobody
    # can recover from memory.
    dropped = [r for r in results if r.name == "PATH entry"]
    assert len(dropped) == 1
    assert dropped[0].outcome is Outcome.SKIPPED
    assert r"C:\old\gone" in dropped[0].detail
    assert by_name["EDITOR"].outcome is Outcome.APPLIED


def test_an_entry_this_machine_already_has_is_not_added_twice(tmp_path: Path):
    """Once from its own registry, and once from the machine half of PATH that
    the user's own copy does not contain."""
    mine = tmp_path / "mine"
    mine.mkdir()
    system = tmp_path / "system32"
    system.mkdir()
    env = Environment.fixture(
        tmp_path,
        {"HKCU\\Environment": {"Path": str(mine)}},
        environ={"PATH": f"{system};{mine}"},
    )

    apply_mod.apply_environment({"variables": {"Path": f"{mine};{system}"}}, env)

    assert env.registry["HKCU\\Environment"]["Path"] == str(mine)


def test_a_path_with_nothing_worth_adding_is_left_exactly_as_it_was(tmp_path: Path):
    """Writing the same value back is still a write, and a restore that says it
    applied something it did not change is the noise this is trying to remove."""
    env = Environment.fixture(
        tmp_path, {"HKCU\\Environment": {"Path": r"C:\mine\bin"}}, environ={"PATH": ""}
    )

    (result,) = [
        r for r in apply_mod.apply_environment({"variables": {"Path": r"C:\gone"}}, env)
        if r.name == "Path"
    ]

    assert result.outcome is Outcome.SKIPPED
    assert env.registry["HKCU\\Environment"]["Path"] == r"C:\mine\bin"


def test_every_machine_owned_variable_is_refused():
    env = Environment.fixture(Path("/tmp/p"), {})
    record = {"variables": {name.upper(): "x" for name in apply_mod.MACHINE_OWNED_VARS}}
    results = apply_mod.apply_environment(record, env)

    assert all(r.outcome is Outcome.SKIPPED for r in results)
    assert env.registry.get("HKCU\\Environment", {}) == {}


def test_only_the_current_user_is_written_to():
    """HKCU, never HKLM: nothing here can affect another account on the machine."""
    env = Environment.fixture(Path("/tmp/p"), {})
    apply_mod.apply_environment({"variables": {"EDITOR": "code"}}, env)
    assert list(env.registry) == ["HKCU\\Environment"]


def test_a_record_with_no_variables_does_nothing():
    env = Environment.fixture(Path("/tmp/p"), {})
    assert apply_mod.apply_environment({}, env) == []
    assert apply_mod.apply_environment({"variables": "nonsense"}, env) == []


# --- through a real restore -------------------------------------------------
def test_a_restore_applies_them_and_can_be_told_not_to(tmp_path: Path, monkeypatch):
    """The settings ride in the manifest, so this goes through a real capture
    and restore rather than calling the appliers directly."""
    from winmigrate import capture as capture_mod
    from winmigrate import restore as restore_mod
    from winmigrate.capture import CaptureOptions
    from winmigrate.config import ScanConfig
    from winmigrate.restore import RestoreOptions
    from winmigrate.scan import run_scan

    profile = tmp_path / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "Documents" / "a.txt").write_text("doc", encoding="utf-8")
    registry = {
        "HKCU\\Environment": {"EDITOR": "code"},
        "HKCU\\Network\\Z": {"RemotePath": "\\\\srv\\share"},
    }
    env = Environment.fixture(profile, registry)
    config = ScanConfig(profile_root=profile, include_software=False)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=bundle, passphrase="pw", use_vss=False),
        config,
        env,
    )

    seen: list[str] = []
    monkeypatch.setattr(
        apply_mod, "apply_environment",
        lambda record, env=None: seen.append("env") or [],
    )
    monkeypatch.setattr(
        apply_mod, "apply_mapped_drives",
        lambda record, runner=None: seen.append("drives") or [],
    )

    restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase="pw", destination=tmp_path / "on")
    )
    assert set(seen) == {"env", "drives"}

    seen.clear()
    restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase="pw", destination=tmp_path / "off",
            apply_settings=False,
        )
    )
    assert seen == []


def test_a_dry_run_changes_nothing_about_the_machine(tmp_path: Path, monkeypatch):
    """A practice run that re-mapped drives and rewrote environment variables
    would not be a practice run."""
    from winmigrate import capture as capture_mod
    from winmigrate import restore as restore_mod
    from winmigrate.capture import CaptureOptions
    from winmigrate.config import ScanConfig
    from winmigrate.restore import RestoreOptions
    from winmigrate.scan import run_scan

    profile = tmp_path / "alice"
    (profile / "Documents").mkdir(parents=True)
    (profile / "Documents" / "a.txt").write_text("doc", encoding="utf-8")
    env = Environment.fixture(profile, {"HKCU\\Environment": {"EDITOR": "code"}})
    config = ScanConfig(profile_root=profile, include_software=False)
    bundle = tmp_path / "b.dat"
    capture_mod.capture(
        run_scan(config, env),
        CaptureOptions(output=bundle, passphrase="pw", use_vss=False),
        config,
        env,
    )

    touched: list[str] = []
    monkeypatch.setattr(
        apply_mod, "apply_environment", lambda record, env=None: touched.append("env") or []
    )
    restore_mod.restore(
        RestoreOptions(
            bundle=bundle, passphrase="pw", destination=tmp_path / "dry", dry_run=True
        )
    )
    assert touched == []


# --- what the tools actually receive ----------------------------------------
def test_the_wifi_filename_is_not_wrapped_in_quotes(tmp_path):
    """filename=<path>, not filename="<path>". The quotes are what you type at a
    prompt, where the shell strips them again; here the argument list goes to
    CreateProcess, which escapes them, and netsh looks for a file whose name
    starts with a quote mark. Every network went out of the export side
    correctly -- it builds folder=<path> with no quotes -- and none of them
    could come back."""
    import subprocess

    folder = tmp_path / "WinMigrate-WiFi"
    folder.mkdir()
    (folder / "Office.xml").write_text("<x/>", encoding="utf-8")
    seen: list[list[str]] = []

    apply_mod.apply_wifi(folder, runner=lambda command, timeout=0: seen.append(command) or _ok())

    assert seen and seen[0][:4] == ["netsh", "wlan", "add", "profile"]
    argument = seen[0][4]
    assert argument == f"filename={folder / 'Office.xml'}"
    assert '"' not in argument
    # And what Windows would really build from that list carries no escaping.
    assert '\\"' not in subprocess.list2cmdline(seen[0])


def test_a_printer_name_that_could_be_reinterpreted_is_refused(tmp_path):
    """The record comes out of a bundle, which may have been written on another
    machine, and printui parses what it is handed itself."""
    seen: list[list[str]] = []
    record = {
        "connections": [
            r"\\server\good",
            '\\\\server\\bad" /q /if /b "x',
            "\\\\server\\line\nbreak",
        ]
    }

    results = apply_mod.apply_printers(
        record, runner=lambda command, timeout=0: seen.append(command) or _ok()
    )

    assert [command[4] for command in seen] == [r"\\server\good"]
    skipped = [r for r in results if r.outcome is apply_mod.Outcome.SKIPPED]
    assert len(skipped) == 2
    assert all("safely" in r.detail for r in skipped)


def test_only_a_drive_letter_is_treated_as_a_drive():
    """net use takes switches in the same position as the drive."""
    seen: list[list[str]] = []
    record = {"drives": {"X": r"\\srv\share", "/delete": r"\\srv\other", "": r"\\srv\third"}}

    results = apply_mod.apply_mapped_drives(
        record, runner=lambda command, timeout=0: seen.append(command) or _ok()
    )

    assert [command[2] for command in seen] == ["X:"]
    assert sorted(r.name for r in results if r.outcome is apply_mod.Outcome.SKIPPED) == [
        "",
        "/delete",
    ]


def _ok():
    from winmigrate.util.process import CommandResult

    return CommandResult(["x"], 0)
