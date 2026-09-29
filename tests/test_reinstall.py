"""The reinstall artifacts, and the boundary that keeps installing separate."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from winmigrate import reinstall
from winmigrate.util.process import CommandResult

SOFTWARE_RECORD = {
    "counts": {"total": 3, "reinstallable_with_winget": 2, "manual": 1},
    "applications": [
        {"name": "Mozilla Firefox", "version": "128", "winget_id": "Mozilla.Firefox"},
        {"name": "7-Zip", "version": "23.01", "winget_id": "7zip.7zip"},
        {"name": "Bespoke Tool", "version": "1.0", "publisher": "ACME"},
    ],
    "winget_export": {
        "Sources": [
            {
                "Packages": [
                    {"PackageIdentifier": "Mozilla.Firefox", "Version": "128"},
                    {"PackageIdentifier": "7zip.7zip", "Version": "23.01"},
                ]
            }
        ]
    },
}

OFFICE_RECORD = {
    "product_ids": ["O365ProPlusRetail"],
    "platform": "x64",
    "client_culture": "en-us",
    "channel": "Current",
    "activation_type": "subscription",
}


def manifest_with(*records) -> dict:
    items = []
    for category, record in records:
        items.append(
            {
                "id": f"{category}:x",
                "category": category,
                "kind": "record",
                "title": category,
                "action": "capture",
                "sensitivity": "normal",
                "record": record,
            }
        )
    return {"items": items}


def test_artifacts_are_written_for_software_and_office(tmp_path: Path):
    artifacts = reinstall.write_artifacts(
        manifest_with(("software", SOFTWARE_RECORD), ("office", OFFICE_RECORD)), tmp_path
    )
    assert artifacts.directory == tmp_path / reinstall.ARTIFACTS_DIRECTORY
    assert artifacts.winget_import.is_file()
    assert artifacts.office_configuration.is_file()
    assert artifacts.manual_list.is_file()
    assert artifacts.reinstallable_count == 2
    assert artifacts.manual_count == 1


def test_the_import_file_is_wingets_own_export_but_for_the_pinned_versions(tmp_path: Path):
    """Replaying winget's export is more reliable than rebuilding one, so the
    file stays winget's own output -- with exactly one thing taken out.

    The versions are removed because keeping them makes the file unusable
    within weeks: the repository drops old manifests, and an import that
    insists on the version the old machine had then installs nothing at all.
    Everything else is left exactly as winget wrote it, and this test is here
    to catch the next thing that quietly starts editing it."""
    artifacts = reinstall.write_artifacts(manifest_with(("software", SOFTWARE_RECORD)), tmp_path)
    written = json.loads(artifacts.winget_import.read_text(encoding="utf-8"))

    assert written == reinstall.without_versions(SOFTWARE_RECORD["winget_export"])
    # Said plainly, so the test does not simply agree with the function it is
    # checking: same packages, same order, no versions.
    original = SOFTWARE_RECORD["winget_export"]["Sources"][0]["Packages"]
    packages = written["Sources"][0]["Packages"]
    assert [p["PackageIdentifier"] for p in packages] == [
        p["PackageIdentifier"] for p in original
    ]
    assert all(set(p) == {"PackageIdentifier"} for p in packages)


def test_applications_winget_cannot_handle_are_listed_not_dropped(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("software", SOFTWARE_RECORD)), tmp_path)
    text = artifacts.manual_list.read_text(encoding="utf-8")
    assert "Bespoke Tool" in text
    assert "Mozilla Firefox" not in text
    # The list is a prompt to decide, not an instruction to reinstall everything.
    assert "were found on the old machine" in text


def test_writing_artifacts_installs_nothing(tmp_path: Path, monkeypatch):
    """The whole point of the split: restore prepares, it does not install."""
    called = []
    monkeypatch.setattr(
        reinstall.process, "run", lambda *a, **k: called.append(a) or CommandResult([], 0)
    )
    reinstall.write_artifacts(
        manifest_with(("software", SOFTWARE_RECORD), ("office", OFFICE_RECORD)), tmp_path
    )
    assert called == []


def test_a_manifest_without_software_writes_no_directory(tmp_path: Path):
    artifacts = reinstall.write_artifacts({"items": []}, tmp_path)
    assert not artifacts.anything_to_do
    assert not (tmp_path / reinstall.ARTIFACTS_DIRECTORY).exists()


def test_the_office_configuration_matches_the_captured_installation(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("office", OFFICE_RECORD)), tmp_path)
    xml = artifacts.office_configuration.read_text(encoding="utf-8")
    assert 'OfficeClientEdition="64"' in xml
    assert 'Channel="Current"' in xml
    assert 'ID="O365ProPlusRetail"' in xml
    assert 'ID="en-us"' in xml


def test_reactivation_steps_follow_the_captured_licence_type(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("office", OFFICE_RECORD)), tmp_path)
    artifacts.office.licences = []
    steps = reinstall.office_reactivation_steps(artifacts.office)
    assert steps
    assert reinstall.office_reactivation_steps(None) == []


def test_winget_import_is_invoked_with_flags_that_survive_one_bad_package(tmp_path: Path):
    recorded = {}

    def fake_runner(command, timeout=None, input_text=None):
        recorded["command"] = command
        return CommandResult(command, 0, stdout="done")

    import_file = tmp_path / "winget-import.json"
    import_file.write_text("{}", encoding="utf-8")
    result = reinstall.run_winget_import(import_file, runner=fake_runner)
    assert result.ok
    assert "--ignore-unavailable" in recorded["command"]
    assert "--accept-package-agreements" in recorded["command"]
    assert str(import_file) in recorded["command"]


def test_office_setup_is_invoked_with_the_configure_switch(tmp_path: Path):
    recorded = {}

    def fake_runner(command, timeout=None, input_text=None):
        recorded["command"] = command
        return CommandResult(command, 0)

    setup = tmp_path / "setup.exe"
    setup.write_text("", encoding="utf-8")
    configuration = tmp_path / "configuration.xml"
    configuration.write_text("<Configuration/>", encoding="utf-8")
    reinstall.run_office_install(setup, configuration, runner=fake_runner)
    assert recorded["command"] == [str(setup), "/configure", str(configuration)]


def test_a_pipe_in_an_application_name_cannot_break_the_markdown_table(tmp_path: Path):
    record = dict(SOFTWARE_RECORD)
    record["applications"] = [{"name": "Weird | Name", "version": "1|2", "publisher": "A|B"}]
    artifacts = reinstall.write_artifacts(manifest_with(("software", record)), tmp_path)
    line = [
        row
        for row in artifacts.manual_list.read_text(encoding="utf-8").splitlines()
        if "Weird" in row
    ][0]
    assert line.count("|") == 4 + 3  # four cell separators plus three escaped pipes


COMPONENT_RECORD = {
    "counts": {"total": 4, "reinstallable_with_winget": 1, "manual": 1, "components": 2},
    "applications": [
        {"name": "Mozilla Firefox", "version": "128", "winget_id": "Mozilla.Firefox"},
        {"name": "ACME Bespoke Suite", "version": "3.2", "publisher": "ACME"},
        {
            "name": "Microsoft Visual C++ 2015-2022 Redistributable (x64)",
            "version": "14.38",
            "component": True,
        },
        {"name": "Microsoft .NET Runtime - 8.0.11 (x64)", "version": "8.0", "component": True},
    ],
    "winget_export": {"Sources": [{"Packages": [{"PackageIdentifier": "Mozilla.Firefox"}]}]},
}


def test_runtimes_are_separated_from_things_that_need_a_person(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("software", COMPONENT_RECORD)), tmp_path)
    assert artifacts.manual_count == 1
    assert artifacts.component_count == 2

    text = artifacts.manual_list.read_text(encoding="utf-8")
    body, components = text.split("## Runtimes and drivers")
    assert "ACME Bespoke Suite" in body
    assert "Visual C++" not in body
    assert "Visual C++" in components
    assert "nothing to do here" in components


def test_the_manual_list_says_how_many_winget_will_handle(tmp_path: Path):
    """Without the other half of the number, "N by hand" reads as the whole job."""
    artifacts = reinstall.write_artifacts(manifest_with(("software", COMPONENT_RECORD)), tmp_path)
    text = artifacts.manual_list.read_text(encoding="utf-8")
    assert "winget can reinstall 1 application(s) on its own" in text
    assert "These 1 it has no package for." in text


def test_launcher_games_get_their_own_section_not_the_by_hand_list(tmp_path: Path):
    record = {
        "counts": {},
        "applications": [
            {"name": "ACME Bespoke Suite", "version": "3.2"},
            {"name": "Counter-Strike 2", "managed_by": "Steam"},
            {"name": "Portal 2", "managed_by": "Steam"},
            {"name": "Fortnite", "managed_by": "Epic Games"},
        ],
        "winget_export": {"Sources": []},
    }
    artifacts = reinstall.write_artifacts(manifest_with(("software", record)), tmp_path)
    assert artifacts.manual_count == 1
    assert artifacts.launcher_count == 3
    text = artifacts.manual_list.read_text(encoding="utf-8")
    body, games = text.split("## Games (return through their launcher)")
    assert "ACME Bespoke Suite" in body
    assert "Counter-Strike 2" not in body
    assert "### Steam (2)" in games
    assert "### Epic Games (1)" in games
    assert "Portal 2" in games


def test_a_malformed_manifest_does_not_cost_the_user_the_restore_report(tmp_path):
    """The reinstall inputs are a convenience written after every file is
    restored and verified. A record from a different version of this tool -- or
    one field that is a string where a list was expected -- used to raise
    AttributeError out of write_artifacts, past the OSError-only guard at the
    call site and past main()'s WinMigrateError handler, replacing the whole
    restore report with a traceback. The report is where the follow-up list
    lives, which is the point of a restore that stops at what needs a person.
    """
    cases = [
        {"items": "not a list"},
        {"items": ["not a dict"]},
        {"items": [{"category": "software", "record": {"applications": "not a list"}}]},
        {"items": [{"category": "software", "record": {"applications": ["not a dict"]}}]},
        {"items": [{"category": "office", "record": {"product_ids": [None, 5]}}]},
    ]
    for index, manifest in enumerate(cases):
        destination = tmp_path / f"case{index}"
        destination.mkdir()
        reinstall.write_artifacts(manifest, destination)  # must not raise


def test_a_manifest_entry_with_a_newline_cannot_break_the_manual_table(tmp_path):
    """A registry DisplayName is whatever the installer wrote there. A newline in
    one turned the rest of the markdown table into loose text."""
    manifest = {
        "items": [
            {
                "category": "software",
                "record": {
                    "applications": [
                        {"name": "Line1\nLine2 | x", "version": "1", "publisher": "P"},
                        {"name": "Ordinary App", "version": "2", "publisher": "Q"},
                    ]
                },
            }
        ]
    }
    artifacts = reinstall.write_artifacts(manifest, tmp_path)
    rows = [
        line for line in artifacts.manual_list.read_text(encoding="utf-8").splitlines() if line.startswith("|")
    ]
    assert len(rows) == 4  # header, rule, and one row per application
    assert "| Line1 Line2 \\| x | 1 | P |" in rows


# --- installing without ninety-seven windows --------------------------------
HELP_WITH_SILENT = """
Downloads and installs packages from a previously exported file.

usage: winget import [-i] <import-file> [<options>]

The following arguments are available:
  -i,--import-file          File describing the packages to install

The following options are available:
  --ignore-unavailable      Suppress errors for unavailable packages
  --ignore-versions         Ignore the versions in the import file
  --no-upgrade              Skip packages already installed
  --accept-package-agreements
  --accept-source-agreements
  --disable-interactivity   Disable interactive prompts
  --silent                  Request silent installation of packages
"""

HELP_WITHOUT_SILENT = HELP_WITH_SILENT.replace(
    "  --silent                  Request silent installation of packages\n", ""
)

HELP_WITHOUT_IGNORE_VERSIONS = HELP_WITH_SILENT.replace(
    "  --ignore-versions         Ignore the versions in the import file\n", ""
)


def helping(text: str, ok: bool = True):
    def runner(argv, timeout=None):
        assert argv[:2] == ["winget", "import"]
        return CommandResult(
            command=list(argv), returncode=0 if ok else 1, stdout=text if ok else ""
        )

    return runner


def test_the_installers_are_asked_to_be_quiet_when_winget_allows_it(tmp_path: Path):
    """--disable-interactivity silences winget's own prompts and nothing else:
    every installer it runs is then free to put a window on the screen, ask
    where to install, and offer a toolbar. Ninety-seven of those over an hour
    is not an unattended migration."""
    command = reinstall.import_command(tmp_path / "x.json", helping(HELP_WITH_SILENT))

    assert "--silent" in command
    assert "--disable-interactivity" in command


def test_a_winget_that_does_not_take_the_flag_is_not_given_it(tmp_path: Path):
    """Passing an option winget does not know is not a degraded install -- it
    is a usage error before the first package, so nothing installs at all."""
    command = reinstall.import_command(tmp_path / "x.json", helping(HELP_WITHOUT_SILENT))

    assert "--silent" not in command
    # And everything that made it work before is still there.
    assert command[:4] == ["winget", "import", "-i", str(tmp_path / "x.json")]


def test_a_winget_that_cannot_answer_gets_the_command_that_always_worked(tmp_path: Path):
    """Missing, broken, or a version that says nothing useful: the import still
    runs, exactly as it did before there was a flag to ask about."""
    command = reinstall.import_command(tmp_path / "x.json", helping("", ok=False))

    assert "--silent" not in command


def test_the_flag_is_looked_for_by_name_rather_than_by_prose():
    """Option names are not translated. Looking for the flag itself is what
    makes this work on a machine running Windows in any language."""
    assert reinstall.supports_silent(helping(HELP_WITH_SILENT)) is True
    assert reinstall.supports_silent(helping(HELP_WITHOUT_SILENT)) is False
    # Prose about silence is not the flag.
    assert reinstall.supports_silent(helping("installs packages silently")) is False


def test_the_import_does_not_insist_on_the_versions_that_were_installed(tmp_path: Path):
    """Without this the import installs nothing at all, and says so package by
    package in a way that looks like the packages are gone.

    winget export writes down the version each package was at; winget import
    then demands exactly that version. The community repository does not keep
    old manifests, so a few weeks later every version in the file is one nobody
    can install, and the whole import fails at once:

        No version found matching: 8.8.1
        Search failed for: Notepad++.Notepad++

    Somebody moving to a new machine is not trying to reproduce last year's
    build of Notepad++; they are trying to get Notepad++ back.
    """
    command = reinstall.import_command(tmp_path / "x.json", helping(HELP_WITH_SILENT))

    assert "--ignore-versions" in command


def test_a_winget_without_that_flag_is_not_given_it_either(tmp_path: Path):
    """Same reasoning as --silent: an unknown option is a usage error before
    the first package, so offering it to a winget that has never heard of it
    trades a partial install for no install."""
    command = reinstall.import_command(
        tmp_path / "x.json", helping(HELP_WITHOUT_IGNORE_VERSIONS)
    )

    assert "--ignore-versions" not in command
    # And the rest of the command is untouched by its absence.
    assert "--silent" in command
    assert "--ignore-unavailable" in command


def test_the_flags_are_worked_out_from_one_reading_of_the_help(tmp_path: Path):
    """winget is not quick to start. Asking it the same question once per flag
    puts a pause in front of the install for each one, and every extra flag
    would make it worse."""
    calls = []

    def counting(argv, timeout=None):
        calls.append(list(argv))
        return CommandResult(command=list(argv), returncode=0, stdout=HELP_WITH_SILENT)

    reinstall.import_command(tmp_path / "x.json", counting)
    assert len(calls) == 1


def test_a_winget_that_cannot_answer_still_gets_a_command_that_runs(tmp_path: Path):
    """Missing, broken, or too old to describe itself. The import must still be
    the command that worked before any of these flags existed."""
    command = reinstall.import_command(tmp_path / "x.json", helping("", ok=False))

    assert command == [
        "winget", "import", "-i", str(tmp_path / "x.json"),
        "--accept-source-agreements", "--accept-package-agreements",
        "--ignore-unavailable", "--disable-interactivity",
    ]


def test_what_came_with_windows_is_neither_counted_nor_written_down(tmp_path: Path):
    """The scan flags these and takes them out of the winget import. The report
    has to agree, or it promises 99 reinstalls for an import file holding 97 --
    and pads the by-hand list with Paint, which is already on the new machine."""
    record = {
        "applications": [
            {"name": "Mozilla Firefox", "winget_id": "Mozilla.Firefox"},
            {"name": "Microsoft Edge", "winget_id": "Microsoft.Edge",
             "shipped_with_windows": True},
            {"name": "Microsoft.Paint", "shipped_with_windows": True},
            {"name": "Bespoke Tool", "publisher": "ACME"},
        ],
        "winget_export": {"Sources": [{"Packages": [{"PackageIdentifier": "Mozilla.Firefox"}]}]},
    }
    artifacts = reinstall.write_artifacts(manifest_with(("software", record)), tmp_path)

    assert artifacts.reinstallable_count == 1
    assert artifacts.manual_count == 1
    text = artifacts.manual_list.read_text(encoding="utf-8")
    assert "Bespoke Tool" in text
    assert "Paint" not in text
    assert "Microsoft Edge" not in text


def test_the_import_file_carries_no_pinned_versions_either(tmp_path: Path):
    """The flag is the fix, and this is the fix said twice, because the
    failure is total and the file is also something the user runs by hand --
    the restore report prints the command next to it."""
    record = {
        "applications": [{"name": "Notepad++", "winget_id": "Notepad++.Notepad++"}],
        "winget_export": {
            "Sources": [
                {
                    "SourceDetails": {"Name": "winget"},
                    "Packages": [
                        {"PackageIdentifier": "Notepad++.Notepad++", "Version": "8.8.1"},
                        # The versions that were never installable: an entry
                        # winget matched through Add/Remove Programs carries
                        # whatever the installer wrote there.
                        {"PackageIdentifier": "Blizzard.BattleNet", "Version": "Unknown"},
                        {"PackageIdentifier": "Ubisoft.Connect", "Version": "< 173.1.0"},
                    ],
                }
            ]
        },
    }
    artifacts = reinstall.write_artifacts(manifest_with(("software", record)), tmp_path)

    written = json.loads(artifacts.winget_import.read_text(encoding="utf-8"))
    packages = written["Sources"][0]["Packages"]
    assert [p["PackageIdentifier"] for p in packages] == [
        "Notepad++.Notepad++", "Blizzard.BattleNet", "Ubisoft.Connect"
    ]
    assert not any("Version" in package for package in packages)
    # Everything else winget wrote is still there, or it is not winget's export.
    assert written["Sources"][0]["SourceDetails"] == {"Name": "winget"}


def test_stripping_the_versions_does_not_empty_the_manifest_it_read_from():
    """The manifest is read again for the report. Editing it in place would
    show up three screens later as an unrelated blank."""
    export = {"Sources": [{"Packages": [{"PackageIdentifier": "a", "Version": "1.0"}]}]}

    stripped = reinstall.without_versions(export)

    assert stripped["Sources"][0]["Packages"][0] == {"PackageIdentifier": "a"}
    assert export["Sources"][0]["Packages"][0]["Version"] == "1.0"


@pytest.mark.parametrize(
    "export",
    [None, {}, "nonsense", {"Sources": "nonsense"}, {"Sources": [None]},
     {"Sources": [{"Packages": None}]}, {"Sources": [{"Packages": ["not a dict"]}]}],
)
def test_a_shape_winget_did_not_write_costs_the_report_nothing(export):
    """The export comes from whatever winget is on the machine, and the
    manifest may have come from a different version of this tool. One odd
    field here must not cost the user the restore report, which is where the
    follow-up list lives."""
    reinstall.without_versions(export)


def test_a_declined_flag_is_logged_with_what_it_costs(caplog):
    """A log line saying an option was refused reads like a fault. Most of the
    time it is not one -- winget still installs the same packages without
    asking the same questions -- and somebody reading the log after a migration
    deserves to know which kind of line they are looking at."""
    import logging

    with caplog.at_level(logging.INFO, logger="winmigrate.reinstall"):
        reinstall.accepted_flags(helping("  --ignore-unavailable\n"))

    text = "\n".join(record.getMessage() for record in caplog.records)
    assert "--silent" in text
    # Not just "does not accept": what follows from it.
    assert "disable-interactivity" in text
    assert "will not" in text and "ask questions" in text


def test_every_optional_flag_can_say_what_its_absence_costs():
    """A flag added to the list without a note logs a refusal and no reason,
    which is the line that started this."""
    for flag in reinstall.OPTIONAL_FLAGS:
        assert flag in reinstall.FLAG_CONSEQUENCES, flag


# --- the login programs, for an install run from the command line ----------
def test_the_login_programs_wait_beside_the_install_files(tmp_path: Path):
    """The window asks again about login programs after its own install. The
    command-line install is a separate process, later, with no manifest -- so
    the entries have to be left where it will look."""
    manifest = manifest_with(("software", SOFTWARE_RECORD))
    manifest["items"].append({
        "id": "settings:startup_run", "category": "startup", "kind": "record",
        "record": {"entries": {"Notepad++": r"C:\Program Files\Notepad++\notepad++.exe"}},
    })

    artifacts = reinstall.write_artifacts(manifest, tmp_path)

    written = json.loads(
        (artifacts.directory / reinstall.STARTUP_FILE).read_text(encoding="utf-8")
    )
    assert written == {"entries": {"Notepad++": r"C:\Program Files\Notepad++\notepad++.exe"}}


def test_no_login_programs_means_no_file(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("software", SOFTWARE_RECORD)), tmp_path)
    assert not (artifacts.directory / reinstall.STARTUP_FILE).exists()


def test_the_command_line_install_puts_the_login_programs_back_afterwards(
    tmp_path: Path, monkeypatch
):
    """The same second question the window asks, for the path you take when
    you install from a command line -- which is the path that was taken."""
    from winmigrate import apply as apply_mod
    from winmigrate.cli import main

    directory = tmp_path / reinstall.ARTIFACTS_DIRECTORY
    directory.mkdir()
    (directory / reinstall.WINGET_IMPORT_FILE).write_text(
        json.dumps({"Sources": [{"Packages": [{"PackageIdentifier": "Notepad++.Notepad++"}]}]}),
        encoding="utf-8",
    )
    record = {"entries": {"Notepad++": r"C:\Program Files\Notepad++\notepad++.exe"}}
    (directory / reinstall.STARTUP_FILE).write_text(json.dumps(record), encoding="utf-8")

    order: list[str] = []
    monkeypatch.setattr(
        reinstall, "install_packages",
        lambda wanted, **k: order.append("install") or [],
    )
    monkeypatch.setattr(
        apply_mod, "apply_startup",
        lambda rec, env=None, only=None: order.append(("startup", rec)) or [],
    )

    assert main(["reinstall", str(directory), "--apps", "--yes"]) == 0
    # After the install, not before: before it, there is nothing to start.
    assert order == ["install", ("startup", record)]


# --- one package at a time ---------------------------------------------------
LOCATION_REQUIRED = 0x8A15005F


def _export(tmp_path: Path, sources: list[tuple[str | None, list[str]]]) -> Path:
    path = tmp_path / reinstall.WINGET_IMPORT_FILE
    path.write_text(
        json.dumps({"Sources": [
            ({"SourceDetails": {"Name": name}} if name else {})
            | {"Packages": [{"PackageIdentifier": p} for p in packages]}
            for name, packages in sources
        ]}),
        encoding="utf-8",
    )
    return path


def _runner(codes: dict[str, int], calls: list[list[str]]):
    def run(command, timeout=0):
        calls.append(list(command))
        identifier = command[command.index("--id") + 1]
        return CommandResult(command, codes.get(identifier, 0), "")
    return run


def test_one_package_that_cannot_install_does_not_cost_the_others():
    """What happened on a real migration: one package out of ninety wanted an
    install location, and winget import stopped fourteen seconds in with
    nothing installed. Each package is its own install now."""
    wanted = [reinstall.Package(p) for p in ("A.A", "B.B", "C.C")]
    calls: list[list[str]] = []
    results = reinstall.install_packages(
        wanted, _runner({"A.A": 0x8A150104}, calls), flags=()
    )

    assert [r.outcome for r in results] == ["failed", "installed", "installed"]
    assert "does not have" in results[0].detail
    assert len(calls) == 3


def test_a_package_that_wants_a_location_is_given_one():
    wanted = [reinstall.Package("Some.Game")]
    calls: list[list[str]] = []

    def run(command, timeout=0):
        calls.append(list(command))
        code = 0 if "--location" in command else LOCATION_REQUIRED
        return CommandResult(command, code, "")

    results = reinstall.install_packages(
        wanted, run, flags=(), environ={"ProgramFiles": r"C:\Program Files"}
    )

    assert len(calls) == 2
    assert calls[1][calls[1].index("--location") + 1] == r"C:\Program Files\Game"
    assert results[0].outcome == "installed"
    # Said where it went, since nobody chose it.
    assert r"C:\Program Files\Game" in results[0].detail


def test_what_is_already_here_is_not_a_failure():
    for code in (0x8A150061, 0x8A15002B, 0x8A15010D):
        result = reinstall.classify(reinstall.Package("A.A"), CommandResult([], code, ""))
        assert result.outcome == "already" and result.present


def test_a_negative_exit_code_is_read_as_the_same_hresult():
    """Depending on how it was reached, the same code can arrive signed."""
    signed = LOCATION_REQUIRED - (1 << 32)
    result = reinstall.classify(reinstall.Package("A.A"), CommandResult([], signed, ""))
    assert result.code == LOCATION_REQUIRED


def test_a_package_winget_no_longer_has_is_said_to_be_by_hand():
    result = reinstall.classify(reinstall.Package("A.A"), CommandResult([], 0x8A150014, ""))
    assert result.outcome == "not_found" and "by-hand" in result.detail


def test_no_winget_is_asked_once_not_ninety_times():
    calls: list[list[str]] = []

    def run(command, timeout=0):
        calls.append(command)
        return CommandResult(command, None, error="winget not found on this machine")

    results = reinstall.install_packages(
        [reinstall.Package("A.A"), reinstall.Package("B.B")], run, flags=()
    )
    assert len(calls) == 1 and results[0].outcome == "no_winget"


def test_stop_is_checked_between_packages():
    calls: list[list[str]] = []
    stop = {"now": False}

    def run(command, timeout=0):
        calls.append(command)
        stop["now"] = True
        return CommandResult(command, 0, "")

    results = reinstall.install_packages(
        [reinstall.Package("A.A"), reinstall.Package("B.B")], run, flags=(),
        cancelled=lambda: stop["now"],
    )
    assert len(calls) == 1 and len(results) == 1


def test_each_install_asks_for_todays_version_from_the_right_catalogue(tmp_path: Path):
    path = _export(tmp_path, [("winget", ["Git.Git"]), ("msstore", ["9NBLGGH4NNS1"])])
    wanted = reinstall.packages(path)
    assert wanted == [reinstall.Package("Git.Git", "winget"),
                      reinstall.Package("9NBLGGH4NNS1", "msstore")]

    command = reinstall.install_command(wanted[1], ("--silent",))
    assert command[:2] == ["winget", "install"]
    assert command[command.index("--source") + 1] == "msstore"
    assert "--exact" in command and "--silent" in command
    assert "--disable-interactivity" in command
    assert "--accept-package-agreements" in command
    assert "--version" not in command


def test_a_package_listed_twice_is_installed_once(tmp_path: Path):
    path = _export(tmp_path, [(None, ["A.A", "A.A"])])
    assert reinstall.package_identifiers(path) == ["A.A"]


def test_results_survive_the_trip_through_the_file(tmp_path: Path):
    """The window reads this while the helper is still writing it, so a line
    caught half-written is skipped rather than fatal."""
    path = tmp_path / reinstall.RESULTS_FILE
    first = reinstall.PackageResult("A.A", "installed", "", 0)
    second = reinstall.PackageResult("B.B", "failed", "the disk is full", 0x8A150105)
    reinstall.append_result(path, first)
    reinstall.append_result(path, second)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write('{"id": "C.C", "outc')

    assert reinstall.read_results(path) == [first, second]
    assert reinstall.read_results(tmp_path / "missing") == []


def test_the_helper_is_this_program_from_a_checkout(tmp_path: Path, monkeypatch):
    import sys

    monkeypatch.setattr(sys, "frozen", False, raising=False)
    import_file = tmp_path / reinstall.WINGET_IMPORT_FILE
    results = tmp_path / reinstall.RESULTS_FILE
    executable, arguments = reinstall.helper_command(import_file, results)

    assert executable == sys.executable
    assert arguments[:3] == ["-m", "winmigrate", "reinstall"]
    assert arguments[3] == str(tmp_path)
    assert {"--apps", "--yes"} <= set(arguments)
    assert arguments[arguments.index("--results") + 1] == str(results)


def test_the_helper_is_the_console_build_when_frozen(tmp_path: Path, monkeypatch):
    import sys

    program = tmp_path / "program"
    program.mkdir()
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program / "WinMigrate.exe"))
    folder = tmp_path / "reinstall"
    folder.mkdir()
    import_file = folder / reinstall.WINGET_IMPORT_FILE

    assert reinstall.helper_command(import_file, folder / "r.jsonl") is None

    (program / reinstall.CONSOLE_BUILD).write_bytes(b"MZ")
    executable, arguments = reinstall.helper_command(import_file, folder / "r.jsonl")
    assert Path(executable).name == reinstall.CONSOLE_BUILD
    assert arguments[0] == "reinstall"


def test_a_helper_on_a_network_share_is_copied_somewhere_local(tmp_path: Path, monkeypatch):
    """The elevated session is another logon, often another account, and a
    share this user opened with their own password is closed to it."""
    import sys

    program = tmp_path / "program"
    program.mkdir()
    (program / reinstall.CONSOLE_BUILD).write_bytes(b"MZ")
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", str(program / "WinMigrate.exe"))
    real_resolve = Path.resolve
    monkeypatch.setattr(
        Path, "resolve",
        lambda self, strict=False: Path("\\\\nas\\share\\" + self.name)
        if self.name == reinstall.CONSOLE_BUILD else real_resolve(self, strict),
    )
    folder = tmp_path / "reinstall"
    folder.mkdir()

    executable, _ = reinstall.helper_command(folder / "x.json", folder / "r.jsonl")

    assert executable == str(folder / reinstall.CONSOLE_BUILD)
    assert (folder / reinstall.CONSOLE_BUILD).read_bytes() == b"MZ"


def test_the_window_helper_reports_each_package_and_leaves_login_programs_alone(
    tmp_path: Path, monkeypatch
):
    """The helper runs as whoever typed the administrator password; login
    programs written from it would land in that account, not this one."""
    from winmigrate import apply as apply_mod
    from winmigrate.cli import main

    directory = tmp_path / reinstall.ARTIFACTS_DIRECTORY
    directory.mkdir()
    _export(directory, [(None, ["A.A", "B.B"])])
    (directory / reinstall.STARTUP_FILE).write_text(
        json.dumps({"entries": {"A": "a.exe"}}), encoding="utf-8"
    )
    results = directory / reinstall.RESULTS_FILE
    results.write_text('{"id": "Old.Run", "outcome": "installed"}\n', encoding="utf-8")

    monkeypatch.setattr(reinstall, "install_flags", lambda runner=None: ())
    monkeypatch.setattr(
        reinstall.process, "run",
        lambda command, timeout=0: CommandResult(
            command, 0x8A150105 if "B.B" in command else 0, ""
        ),
    )
    started: list[str] = []
    monkeypatch.setattr(
        apply_mod, "apply_startup", lambda *a, **k: started.append("yes") or []
    )

    assert main(["reinstall", str(directory), "--apps", "--yes",
                 "--results", str(results)]) == 0

    written = reinstall.read_results(results)
    assert [(r.identifier, r.outcome) for r in written] == [
        ("A.A", "installed"), ("B.B", "failed")
    ]
    assert started == []


def test_the_run_summary_knows_whether_anything_arrived():
    already = reinstall.InstallRun(1, [reinstall.PackageResult("A.A", "already")])
    assert not already.installed_any
    fresh = reinstall.InstallRun(1, [reinstall.PackageResult("A.A", "installed")])
    assert fresh.installed_any and fresh.counts == {"installed": 1}
    assert reinstall.InstallRun(1, [reinstall.PackageResult("A.A", "no_winget")]).unavailable


def test_bonjour_is_not_installed_on_its_own(tmp_path: Path):
    """The old machine had it because iTunes brought it. Installed alone, it
    earned a "module blocked from the Local Security Authority" dialog at
    every start of the new machine, and nothing in exchange."""
    path = _export(tmp_path, [(None, ["Apple.Bonjour", "Apple.iTunes", "Git.Git"])])
    assert reinstall.package_identifiers(path) == ["Apple.iTunes", "Git.Git"]
