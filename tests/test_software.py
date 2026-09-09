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
    merged, _stats = software.merge(registry, appx, [("Mozilla.Firefox", "128")])
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
    merged, _stats = software.merge(entries, [], [("Mozilla.Firefox", "128")])
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


# --- winget list parsing ---------------------------------------------------
WINGET_LIST_OUTPUT = """   -
   \\
Name                                     Id                              Version       Available Source
--------------------------------------------------------------------------------------------------------
7-Zip 24.09 (x64)                        7zip.7zip                       24.09                   winget
Microsoft Edge                           Microsoft.Edge                  131.0.2903.86           winget
ACME Bespoke Suite                       ARP\\Machine\\X64\\{GUID-1234}      3.2
Microsoft Visual C++ 2015-2022 Redist…   Microsoft.VCRedist.2015+.x64    14.38.33130             winget
Spotify                                  Spotify.Spotify                 1.2.3                   msstore
"""


def test_winget_list_columns_are_sliced_from_the_header():
    rows = software.parse_winget_list(WINGET_LIST_OUTPUT)
    assert [row.name for row in rows][:2] == ["7-Zip 24.09 (x64)", "Microsoft Edge"]
    assert rows[0].identifier == "7zip.7zip"
    assert rows[0].version == "24.09"
    assert rows[0].source == "winget"


def test_a_synthetic_arp_id_means_winget_has_no_package():
    """winget lists everything; entries it cannot reinstall get a generated id."""
    rows = {row.name: row for row in software.parse_winget_list(WINGET_LIST_OUTPUT)}
    assert rows["ACME Bespoke Suite"].has_package is False
    assert rows["7-Zip 24.09 (x64)"].has_package is True
    assert rows["Spotify"].has_package is True


def test_output_without_a_header_yields_nothing_rather_than_garbage():
    assert software.parse_winget_list("some error text\nno table here") == []
    assert software.parse_winget_list("") == []


def test_winget_list_answers_are_preferred_over_name_matching():
    entries = [
        software.SoftwareEntry(name="7-Zip 24.09 (x64)", sources=["registry"]),
        software.SoftwareEntry(name="ACME Bespoke Suite", sources=["registry"]),
    ]
    software.apply_winget_listings(entries, software.parse_winget_list(WINGET_LIST_OUTPUT))
    assert entries[0].winget_id == "7zip.7zip"
    assert entries[1].winget_id is None
    # And the "no" is winget's own answer, not a failure to guess.
    assert entries[1].winget_knows_no_package is True


def test_a_truncated_name_is_matched_on_its_prefix():
    entries = [
        software.SoftwareEntry(
            name="Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33130",
            sources=["registry"],
        )
    ]
    software.apply_winget_listings(entries, software.parse_winget_list(WINGET_LIST_OUTPUT))
    assert entries[0].winget_id == "Microsoft.VCRedist.2015+.x64"


def test_merge_does_not_second_guess_winget_with_name_matching():
    entries = [software.SoftwareEntry(name="ACME Bespoke Suite", sources=["registry"])]
    merged, _stats = software.merge(
        entries, [], [("Acme.BespokeSuite", "3.2")], software.parse_winget_list(WINGET_LIST_OUTPUT)
    )
    assert merged[0].winget_id is None


# --- id candidates ---------------------------------------------------------
def test_multi_part_ids_do_not_match_on_their_last_component_alone():
    """The bug behind a 17% match rate on a real machine.

    Taking only the last component matches Microsoft.VCRedist.2015+.x64 on
    "x64" and Microsoft.VisualStudio.2022.Community on "community".
    """
    candidates = software.id_candidates("Microsoft.VisualStudio.2022.Community")
    assert "visualstudio2022community" in candidates
    assert candidates[0] == "microsoftvisualstudio2022community"
    assert all(len(candidate) >= software.MIN_CANDIDATE_LENGTH for candidate in candidates)

    entry = software.SoftwareEntry(name="Visual Studio Community 2022", publisher="Microsoft")
    assert (
        software.match_winget_id(entry, [("Microsoft.VisualStudio.2022.Community", "1")])
        == "Microsoft.VisualStudio.2022.Community"
    )


def test_a_short_id_tail_does_not_produce_accidental_matches():
    entry = software.SoftwareEntry(name="Some Diagnostics Utility x64", publisher="ACME")
    assert software.match_winget_id(entry, [("Some.Ag", "1")]) is None
    # ...and an id whose tail is just an architecture cannot match on that.
    assert software.match_winget_id(entry, [("Other.Thing.x64", "1")]) is None


# --- components ------------------------------------------------------------
def test_runtimes_and_drivers_are_classified_as_components():
    for name in [
        "Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33130",
        "Microsoft .NET Runtime - 8.0.11 (x64)",
        "Microsoft Edge WebView2 Runtime",
        "Windows Software Development Kit",
        "Intel(R) Chipset Device Software driver",
    ]:
        assert software.SoftwareEntry(name=name).is_component, name


def test_real_applications_are_not_classified_as_components():
    for name in ["Mozilla Firefox", "7-Zip 23.01 (x64)", "ACME Bespoke Suite", "Slack"]:
        assert not software.SoftwareEntry(name=name).is_component, name


def test_components_are_counted_apart_from_things_to_reinstall_by_hand():
    """249 chores becomes an honest number once runtimes are separated out."""
    inventory = software.SoftwareInventory(
        entries=[
            software.SoftwareEntry(name="Mozilla Firefox", winget_id="Mozilla.Firefox"),
            software.SoftwareEntry(name="ACME Bespoke Suite"),
            software.SoftwareEntry(name="Microsoft Visual C++ 2015-2022 Redistributable (x64)"),
            software.SoftwareEntry(name="Microsoft .NET Runtime - 8.0.11 (x64)"),
        ]
    )
    assert [entry.name for entry in inventory.manual] == ["ACME Bespoke Suite"]
    assert len(inventory.components) == 2
    counts = inventory.to_json()["counts"]
    assert counts == {
        "total": 4,
        "reinstallable_with_winget": 1,
        "manual": 1,
        "components": 2,
        "winget_packages_in_export": 0,
    }


# --- robustness of winget list parsing -------------------------------------
GERMAN_LIST_OUTPUT = """Name                                     Kennung                         Version   Verfügbar Quelle
-------------------------------------------------------------------------------------------------
7-Zip 24.09 (x64)                        7zip.7zip                       24.09               winget
ACME Eigenbau                            ARP\\Machine\\X64\\{GUID}          3.2
"""


def test_a_localised_header_is_parsed_by_position_not_by_word():
    """A German winget prints Kennung, not Id.

    Matching the literal "Id" is how this silently fell back to guessing on a
    real machine; the rule of dashes and the fixed column order are what hold
    across languages.
    """
    rows = software.parse_winget_list(GERMAN_LIST_OUTPUT)
    assert [row.name for row in rows] == ["7-Zip 24.09 (x64)", "ACME Eigenbau"]
    assert rows[0].identifier == "7zip.7zip"
    assert rows[0].has_package is True
    assert rows[1].has_package is False


def test_terminal_escape_sequences_do_not_shift_the_columns():
    text = (
        "\x1b[?25l\x1b[32mName\x1b[0m                Id                  Version\n"
        "------------------------------------------------------\n"
        "Mozilla Firefox     Mozilla.Firefox     128.0\n"
    )
    rows = software.parse_winget_list(text)
    assert rows[0].name == "Mozilla Firefox"
    assert rows[0].identifier == "Mozilla.Firefox"


def test_columns_separated_by_a_single_space_are_still_separate():
    """winget uses one space when a column is exactly as wide as its heading."""
    text = (
        "Name        Id          Version Available Source\n"
        "-----------------------------------------------\n"
        "7-Zip       7zip.7zip   24.09             winget\n"
    )
    rows = software.parse_winget_list(text)
    assert rows[0].source == "winget"
    assert rows[0].version == "24.09"


def test_a_box_drawing_rule_is_recognised_as_the_header_underline():
    text = (
        "Name             Id               Version\n"
        "──────────────────────────────\n"
        "Mozilla Firefox  Mozilla.Firefox  128.0\n"
    )
    assert software.parse_winget_list(text)[0].identifier == "Mozilla.Firefox"


def test_error_text_without_a_table_still_yields_nothing():
    assert software.parse_winget_list("Failed when searching source; results may be missing") == []


# --- join diagnostics ------------------------------------------------------
def test_the_join_rate_is_recorded_so_it_can_be_checked_on_a_real_machine():
    """Whether the automatic/manual split is trustworthy is a number, not a hope.

    It cannot be verified from a fixture -- only on a machine with real software
    on it -- so the counts travel in the record.
    """
    entries = [
        software.SoftwareEntry(name="7-Zip 24.09 (x64)", sources=["registry"]),
        software.SoftwareEntry(
            name="Microsoft Visual C++ 2015-2022 Redistributable (x64) - 14.38.33130",
            sources=["registry"],
        ),
    ]
    stats = software.apply_winget_listings(
        entries, software.parse_winget_list(WINGET_LIST_OUTPUT)
    )
    assert stats.listed_rows == 5
    assert stats.joined_exactly == 1          # 7-Zip, by exact name
    assert stats.joined_by_prefix == 1        # the truncated VC++ row
    # Edge, Spotify and the ARP row are installed but not in our entry list.
    assert stats.unjoined_rows_with_package == 2


def test_packages_winget_listed_but_could_not_be_joined_are_flagged(monkeypatch, tmp_path):
    """Silently over-reporting manual work is the failure mode worth catching."""
    inventory = software.SoftwareInventory()
    inventory.entries, inventory.join_stats = software.merge(
        [software.SoftwareEntry(name="Totally Different Name", sources=["registry"])],
        [],
        [],
        software.parse_winget_list(WINGET_LIST_OUTPUT),
    )
    assert inventory.join_stats.unjoined_rows_with_package == 4
    diagnostics = inventory.to_json()["diagnostics"]
    assert diagnostics["winget_list_rows"] == 5
    assert diagnostics["winget_packages_not_joined"] == 4
