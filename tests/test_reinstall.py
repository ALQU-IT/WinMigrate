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
