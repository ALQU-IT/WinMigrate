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


def test_path_is_never_overwritten():
    """PATH reads like a user setting and is really a list of places software
    was installed on the *old* computer. Replacing the new machine's copy breaks
    every tool that is not in the same place, and the breakage looks nothing
    like a backup restore."""
    env = Environment.fixture(Path("/tmp/p"), {})
    record = {"variables": {"PATH": "C:\\old\\bin", "EDITOR": "code"}}
    results = {r.name: r for r in apply_mod.apply_environment(record, env)}

    assert results["PATH"].outcome is Outcome.SKIPPED
    assert "PATH" not in env.registry.get("HKCU\\Environment", {})
    assert results["EDITOR"].outcome is Outcome.APPLIED


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
