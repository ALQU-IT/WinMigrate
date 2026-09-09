"""Software inventory: registry enumeration, tool output parsing, and merging."""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate.platform_win import Environment
from winmigrate.scan import software
from winmigrate.util.process import CommandResult

UNINSTALL = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
WOW = r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"


def env_with(registry: dict) -> Environment:
    return Environment.fixture(Path("/tmp/profile"), registry)


def test_applications_are_read_from_all_three_uninstall_roots():
    env = env_with(
        {
            f"HKLM\\{UNINSTALL}\\Firefox": {"DisplayName": "Mozilla Firefox", "DisplayVersion": "128"},
            f"HKLM\\{WOW}\\7zip": {"DisplayName": "7-Zip 23.01 (x64)", "DisplayVersion": "23.01"},
            f"HKCU\\{UNINSTALL}\\Slack": {"DisplayName": "Slack", "DisplayVersion": "4.3"},
        }
    )
    entries = software.read_registry_entries(env)
    assert {entry.name for entry in entries} == {"Mozilla Firefox", "7-Zip 23.01 (x64)", "Slack"}
    by_name = {entry.name: entry for entry in entries}
    assert by_name["Slack"].scope == "user"
    assert by_name["Mozilla Firefox"].scope == "machine"
    assert by_name["7-Zip 23.01 (x64)"].architecture == "x86"


def test_windows_updates_and_system_components_are_not_applications():
    env = env_with(
        {
            f"HKLM\\{UNINSTALL}\\Real": {"DisplayName": "Real App"},
            f"HKLM\\{UNINSTALL}\\KB": {"DisplayName": "KB5001234"},
            f"HKLM\\{UNINSTALL}\\UpdFor": {"DisplayName": "Update for Microsoft Office"},
            f"HKLM\\{UNINSTALL}\\Sys": {"DisplayName": "Hidden", "SystemComponent": 1},
            f"HKLM\\{UNINSTALL}\\Rel": {"DisplayName": "Patch", "ReleaseType": "Security Update"},
            f"HKLM\\{UNINSTALL}\\Child": {"DisplayName": "Sub", "ParentKeyName": "Real"},
            f"HKLM\\{UNINSTALL}\\Nameless": {"DisplayVersion": "1.0"},
        }
    )
    assert [entry.name for entry in software.read_registry_entries(env)] == ["Real App"]


def test_a_winget_export_is_flattened_to_package_pairs():
    export = {
        "Sources": [
            {
                "Packages": [
                    {"PackageIdentifier": "Mozilla.Firefox", "Version": "128.0"},
                    {"PackageIdentifier": "7zip.7zip", "Version": "23.01"},
                ]
            },
            {"Packages": [{"PackageIdentifier": "Microsoft.VisualStudioCode", "Version": "1.9"}]},
        ]
    }
    assert software.packages_from_export(export) == [
        ("Mozilla.Firefox", "128.0"),
        ("7zip.7zip", "23.01"),
        ("Microsoft.VisualStudioCode", "1.9"),
    ]


def test_an_absent_or_empty_export_yields_nothing_rather_than_failing():
    assert software.packages_from_export(None) == []
    assert software.packages_from_export({}) == []
    assert software.packages_from_export({"Sources": None}) == []


def test_powershell_json_is_parsed_for_both_one_and_many_packages():
    """ConvertTo-Json emits a bare object for a single result -- a classic trap."""
    single = json.dumps(
        {"Name": "Contoso.App", "PackageFamilyName": "Contoso.App_abc", "Version": "1.0"}
    )
    many = json.dumps(
        [
            {"Name": "Contoso.App", "PackageFamilyName": "Contoso.App_abc", "Version": "1.0"},
            {"Name": "Fabrikam.Tool", "PackageFamilyName": "Fabrikam.Tool_x", "Version": "2.0"},
        ]
    )
    assert [entry.name for entry in software.parse_appx_json(single)] == ["Contoso.App"]
    assert len(software.parse_appx_json(many)) == 2


def test_windows_component_packages_are_filtered_out_of_the_appx_list():
    text = json.dumps(
        [
            {"Name": "Microsoft.Windows.Photos", "Version": "1"},
            {"Name": "Microsoft.VCLibs.140.00", "Version": "1"},
            {"Name": "SpotifyAB.SpotifyMusic", "Version": "1"},
        ]
    )
    assert [entry.name for entry in software.parse_appx_json(text)] == ["SpotifyAB.SpotifyMusic"]


def test_a_certificate_publisher_is_reduced_to_its_common_name():
    text = json.dumps(
        [{"Name": "Some.App", "Publisher": "CN=Contoso Ltd, O=Contoso, C=US", "Version": "1"}]
    )
    assert software.parse_appx_json(text)[0].publisher == "Contoso Ltd"


def test_unparseable_tool_output_yields_nothing_rather_than_raising():
    assert software.parse_appx_json("not json at all") == []
    assert software.parse_appx_json("") == []


def test_package_ids_are_matched_to_installed_names():
    packages = [
        ("Mozilla.Firefox", "128"),
        ("7zip.7zip", "23.01"),
        ("Microsoft.VisualStudioCode", "1.9"),
    ]
    entry = software.SoftwareEntry(name="Mozilla Firefox (x64 en-US)", publisher="Mozilla")
    assert software.match_winget_id(entry, packages) == "Mozilla.Firefox"

    seven = software.SoftwareEntry(name="7-Zip 23.01 (x64)", publisher="Igor Pavlov")
    assert software.match_winget_id(seven, packages) == "7zip.7zip"

    code = software.SoftwareEntry(name="Microsoft Visual Studio Code", publisher="Microsoft")
    assert software.match_winget_id(code, packages) == "Microsoft.VisualStudioCode"


def test_an_unmatched_application_gets_no_package_id():
    entry = software.SoftwareEntry(name="Bespoke Internal Tool", publisher="ACME")
    assert software.match_winget_id(entry, [("Mozilla.Firefox", "128")]) is None


def test_very_short_product_tokens_do_not_match_by_accident():
    entry = software.SoftwareEntry(name="Diagnostics Utility", publisher="ACME")
    assert software.match_winget_id(entry, [("Some.Ag", "1")]) is None


def test_merging_deduplicates_and_labels_what_winget_can_reinstall():
    registry = [
        software.SoftwareEntry(name="Mozilla Firefox", version="128", sources=["registry"]),
        software.SoftwareEntry(name="Bespoke Tool", version="1.0", sources=["registry"]),
    ]
    appx = [
        software.SoftwareEntry(name="Mozilla Firefox", version="128", sources=["appx"]),
        software.SoftwareEntry(name="SpotifyAB.SpotifyMusic", version="1", sources=["appx"]),
    ]
    merged = software.merge(registry, appx, [("Mozilla.Firefox", "128")])
    names = [entry.name for entry in merged]
    assert names.count("Mozilla Firefox") == 1

    firefox = next(entry for entry in merged if entry.name == "Mozilla Firefox")
    assert set(firefox.sources) == {"registry", "appx", "winget"}
    assert firefox.winget_id == "Mozilla.Firefox"

    bespoke = next(entry for entry in merged if entry.name == "Bespoke Tool")
    assert bespoke.winget_id is None and not bespoke.reinstallable


def test_one_package_id_is_not_claimed_by_two_applications():
    entries = [
        software.SoftwareEntry(name="Mozilla Firefox", sources=["registry"]),
        software.SoftwareEntry(name="Mozilla Firefox ESR", sources=["registry"]),
    ]
    merged = software.merge(entries, [], [("Mozilla.Firefox", "128")])
    assert sum(1 for entry in merged if entry.winget_id == "Mozilla.Firefox") == 1


def test_the_export_file_is_read_when_winget_writes_one(tmp_path: Path, monkeypatch):
    payload = {"Sources": [{"Packages": [{"PackageIdentifier": "A.B", "Version": "1"}]}]}

    def fake_runner(command, timeout=None, input_text=None):
        target = Path(command[command.index("-o") + 1])
        target.write_text(json.dumps(payload), encoding="utf-8")
        return CommandResult(command, 0)

    export, error = software.run_winget_export(runner=fake_runner)
    assert error is None
    assert software.packages_from_export(export) == [("A.B", "1")]


def test_a_missing_winget_is_reported_not_raised():
    def fake_runner(command, timeout=None, input_text=None):
        return CommandResult(command, None, error="winget not found on this machine")

    export, error = software.run_winget_export(runner=fake_runner)
    assert export is None
    assert "not found" in error


def test_winget_exiting_non_zero_still_uses_the_file_it_wrote(tmp_path: Path):
    """winget exits non-zero when some installed packages are in no source."""

    def fake_runner(command, timeout=None, input_text=None):
        target = Path(command[command.index("-o") + 1])
        target.write_text(json.dumps({"Sources": [{"Packages": []}]}), encoding="utf-8")
        return CommandResult(command, 1, stderr="some packages were not found")

    export, error = software.run_winget_export(runner=fake_runner)
    assert export is not None and error is None
