"""The reinstall artifacts, and the boundary that keeps installing separate."""

from __future__ import annotations

import json
from pathlib import Path

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


def test_the_import_file_is_wingets_own_export_verbatim(tmp_path: Path):
    """Replaying winget's export is more reliable than rebuilding one."""
    artifacts = reinstall.write_artifacts(manifest_with(("software", SOFTWARE_RECORD)), tmp_path)
    written = json.loads(artifacts.winget_import.read_text())
    assert written == SOFTWARE_RECORD["winget_export"]


def test_applications_winget_cannot_handle_are_listed_not_dropped(tmp_path: Path):
    artifacts = reinstall.write_artifacts(manifest_with(("software", SOFTWARE_RECORD)), tmp_path)
    text = artifacts.manual_list.read_text()
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
    xml = artifacts.office_configuration.read_text()
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
    import_file.write_text("{}")
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
    setup.write_text("")
    configuration = tmp_path / "configuration.xml"
    configuration.write_text("<Configuration/>")
    reinstall.run_office_install(setup, configuration, runner=fake_runner)
    assert recorded["command"] == [str(setup), "/configure", str(configuration)]


def test_a_pipe_in_an_application_name_cannot_break_the_markdown_table(tmp_path: Path):
    record = dict(SOFTWARE_RECORD)
    record["applications"] = [{"name": "Weird | Name", "version": "1|2", "publisher": "A|B"}]
    artifacts = reinstall.write_artifacts(manifest_with(("software", record)), tmp_path)
    line = [
        row
        for row in artifacts.manual_list.read_text().splitlines()
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

    text = artifacts.manual_list.read_text()
    body, components = text.split("## Runtimes and drivers")
    assert "ACME Bespoke Suite" in body
    assert "Visual C++" not in body
    assert "Visual C++" in components
    assert "nothing to do here" in components


def test_the_manual_list_says_how_many_winget_will_handle(tmp_path: Path):
    """Without the other half of the number, "N by hand" reads as the whole job."""
    artifacts = reinstall.write_artifacts(manifest_with(("software", COMPONENT_RECORD)), tmp_path)
    text = artifacts.manual_list.read_text()
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
    text = artifacts.manual_list.read_text()
    body, games = text.split("## Games (return through their launcher)")
    assert "ACME Bespoke Suite" in body
    assert "Counter-Strike 2" not in body
    assert "### Steam (2)" in games
    assert "### Epic Games (1)" in games
    assert "Portal 2" in games
