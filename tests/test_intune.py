"""Programs the organisation installed: found, matched, and left to Company Portal."""

from __future__ import annotations

import json
from pathlib import Path

from winmigrate import reinstall
from winmigrate.platform_win import Environment
from winmigrate.scan import intune
from winmigrate.scan.software import SoftwareEntry, SoftwareInventory, read_registry_entries

USER = "8f2b6c1e-0d3a-4b57-9c1f-2a6e5d4c3b21"
DEVICE = "00000000-0000-0000-0000-000000000000"
SEVEN_ZIP = "1a2b3c4d-1111-2222-3333-444455556666"
VPN = "9f8e7d6c-aaaa-bbbb-cccc-ddddeeeeffff"
GONE = "0b0b0b0b-1212-3434-5656-787878787878"
MYSTERY = "5c5c5c5c-9999-8888-7777-666655554444"
STRAY = "7d7d7d7d-4444-5555-6666-777788889999"
MSI_CODE = "{11111111-2222-3333-4444-555555555555}"
WIN32 = intune.WIN32_KEY
UNINSTALL = r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def state(compliance=None, enforcement=None) -> dict[str, dict]:
    keys: dict[str, dict] = {}
    if compliance is not None:
        keys["ComplianceStateMessage"] = {
            "ComplianceStateMessage": json.dumps({"Applicability": 0, "ComplianceState": compliance})
        }
    if enforcement is not None:
        keys["EnforcementStateMessage"] = {
            "EnforcementStateMessage": json.dumps({"EnforcementState": enforcement})
        }
    return keys


def managed_machine(tmp_path: Path, logs: bool = True) -> Environment:
    """A work PC: enrolled, with packaged apps, an MSI, and the agent's logs."""
    registry: dict[str, dict] = {
        rf"HKLM\{intune.ENROLLMENTS_KEY}\AAAA-1": {"ProviderID": "MS DM Server"},
        rf"HKLM\{intune.ENROLLMENTS_KEY}\BBBB-2": {"ProviderID": "Something Else"},
        # Things the agent keeps beside the per-user keys, which are not users.
        rf"HKLM\{WIN32}\Reporting": {"x": 1},
        rf"HKLM\{WIN32}\Reporting\{STRAY}_1": {},
        rf"HKLM\{WIN32}\Reporting\{STRAY}_1\ComplianceStateMessage": {
            "ComplianceStateMessage": json.dumps({"ComplianceState": 1})
        },
        rf"HKLM\{WIN32}\{USER}\GRS": {"x": 1},
        rf"HKLM\{intune.MSI_KEY}\S-0-0-00-0000000000-0000000000-000000000-000\MSI\{MSI_CODE}": {
            "Status": 70
        },
        rf"HKLM\{intune.MSI_KEY}\S-1-5-21-1-2-3-1001\MSI"
        r"\{99999999-2222-3333-4444-555555555555}": {"Status": 60},
        rf"{UNINSTALL}\7-Zip": {"DisplayName": "7-Zip 23.01 (x64)", "DisplayVersion": "23.01"},
        rf"{UNINSTALL}\{MSI_CODE}": {"DisplayName": "Contoso Timesheet", "DisplayVersion": "4.2"},
        rf"{UNINSTALL}\Notepad++": {"DisplayName": "Notepad++ (64-bit x64)"},
    }
    for owner, app, keys in (
        (USER, f"{SEVEN_ZIP}_1", state(compliance=1, enforcement=1000)),
        (DEVICE, f"{VPN}_3", state(enforcement=1000)),
        (USER, f"{GONE}_1", state(compliance=2)),
        (USER, f"{MYSTERY}_1", state()),
    ):
        registry[rf"HKLM\{WIN32}\{owner}\{app}"] = {}
        for name, values in keys.items():
            registry[rf"HKLM\{WIN32}\{owner}\{app}\{name}"] = values
    env = Environment.fixture(tmp_path / "alice", registry, {"ProgramData": str(tmp_path / "pd")})
    if logs:
        folder = intune.log_folder(env)
        folder.mkdir(parents=True)
        policies = json.dumps([
            {"Id": SEVEN_ZIP, "Name": "7-Zip 23.01 x64 - Required", "Version": 1, "Intent": 3,
             "InstallCommandLine": "setup.exe /S LICENCEKEY=SECRET-1234"},
            {"Id": VPN, "Name": "Contoso \"Secure\" VPN", "Version": 3, "Intent": 1,
             "InstallCommandLine": "vpn.msi /qn"},
            {"Id": GONE, "Name": "Removed Tool", "Version": 1, "Intent": 4},
        ])
        (folder / "IntuneManagementExtension.log").write_text(
            f'<![LOG[Get policies = {policies}]LOG]!><time="10:00:00" component="Win32App">\n'
            '<![LOG[something else entirely]LOG]!>\n', encoding="utf-8",
        )
        (folder / "Unrelated.log").write_text(
            '{"Id":"%s","Name":"Wrong Log"}' % SEVEN_ZIP, encoding="utf-8"
        )
    return env


def test_a_machine_is_managed_only_by_an_intune_enrolment(tmp_path: Path):
    assert intune.is_enrolled(managed_machine(tmp_path))
    other = Environment.fixture(tmp_path / "b", {
        rf"HKLM\{intune.ENROLLMENTS_KEY}\X": {"ProviderID": "Something Else"},
    })
    assert not intune.is_enrolled(other)


def test_the_packaged_apps_are_found_with_the_names_the_company_gave_them(tmp_path: Path):
    portal = intune.scan(managed_machine(tmp_path))

    apps = {app.app_id or app.product_code: app for app in portal.apps}
    assert apps[SEVEN_ZIP].name == "7-Zip 23.01 x64 - Required"
    assert apps[VPN].name == 'Contoso "Secure" VPN'
    assert apps[VPN].installed is True
    # Intune itself says this one is not installed, so it is not listed.
    assert GONE not in apps
    # No name and no clear "installed": nothing anyone could act on.
    assert MYSTERY not in apps
    # Kept beside the per-user keys by the agent, and not under any user.
    assert STRAY not in apps
    # The MSI whose install completed, not the one that failed.
    assert apps[MSI_CODE.upper()].kind == "msi"
    assert len([a for a in portal.apps if a.kind == "msi"]) == 1


def test_only_the_id_and_the_name_leave_the_log(tmp_path: Path):
    """A policy line holds the install command, and an install command can hold
    a licence key. Nothing but the two fields may come out of it."""
    env = managed_machine(tmp_path)
    portal = intune.scan(env)
    names = intune.read_app_names(intune.log_folder(env))

    everything = json.dumps(portal.to_json()) + json.dumps(names)
    assert "SECRET-1234" not in everything and "LICENCEKEY" not in everything
    assert "setup.exe" not in everything
    assert set(names) == {SEVEN_ZIP, VPN, GONE}


def test_without_logs_an_installed_app_is_still_listed_by_its_id(tmp_path: Path):
    portal = intune.scan(managed_machine(tmp_path, logs=False))
    by_id = {app.app_id: app for app in portal.apps if app.kind == "win32"}
    assert set(by_id) == {SEVEN_ZIP, VPN}
    assert by_id[SEVEN_ZIP].name == "" and by_id[SEVEN_ZIP].link.endswith(SEVEN_ZIP)
    assert portal.unnamed == 2


def test_a_private_machine_has_nothing_to_say(tmp_path: Path):
    portal = intune.scan(Environment.fixture(tmp_path / "home", {}))
    assert portal.apps == [] and portal.enrolled is False


def test_an_odd_record_is_unknown_never_a_guess(tmp_path: Path):
    key = rf"HKLM\{WIN32}\{USER}\{SEVEN_ZIP}_1"
    for raw in ("not json", "[1, 2]", json.dumps({"ComplianceState": "x"}),
                json.dumps({"ComplianceState": True}), 7):
        env = Environment.fixture(tmp_path / "x", {
            key: {}, rf"{key}\ComplianceStateMessage": {"ComplianceStateMessage": raw},
        })
        (app,) = intune.read_win32_apps(env)
        assert app.installed is None


def test_names_are_matched_strictly():
    assert intune.same_program("7-Zip 23.01 x64 - Required", "7-Zip 23.01 (x64)")
    assert intune.same_program("Adobe Acrobat Reader", "Adobe Acrobat Reader DC - Deutsch")
    assert intune.same_program("Notepad++ 8.6", "Notepad++ (64-bit x64)")
    # One word in common is not a program in common.
    assert not intune.same_program("Teams", "Teams Machine-Wide Installer")
    assert not intune.same_program("Chrome", "Google Chrome")
    assert not intune.same_program("Contoso VPN", "Contoso Timesheet")
    assert not intune.same_program("", "Anything")


def test_company_apps_are_tied_to_the_installed_programs_they_are(tmp_path: Path):
    env = managed_machine(tmp_path)
    entries = read_registry_entries(env)
    portal = intune.scan(env)

    matched = intune.match(portal, entries)

    by_name = {entry.name: entry for entry in entries}
    assert matched == 2
    # By name, for the packaged app...
    assert by_name["7-Zip 23.01 (x64)"].company_app["id"] == SEVEN_ZIP
    # ...and exactly, by product code, for the MSI -- which takes its name from
    # the installed program, since Intune's record of an MSI has none.
    assert by_name["Contoso Timesheet"].company_app["product_code"] == MSI_CODE.upper()
    assert by_name["Notepad++ (64-bit x64)"].company_app is None
    assert {app.name or app.matched for app in portal.apps} == {
        "7-Zip 23.01 x64 - Required", 'Contoso "Secure" VPN', "Contoso Timesheet",
    }


def test_two_candidates_is_a_guess_and_a_guess_is_not_written_down():
    portal = intune.CompanyPortal(apps=[
        intune.CompanyApp(kind="win32", app_id=SEVEN_ZIP, name="Contoso Tools", installed=True)
    ])
    entries = [SoftwareEntry(name="Contoso Tools Client"), SoftwareEntry(name="Contoso Tools Server")]
    assert intune.match(portal, entries) == 0
    assert all(entry.company_app is None for entry in entries)
    # Still listed: Intune says it was installed, whichever program it was.
    assert len(portal.apps) == 1


def test_the_inventory_carries_the_flag_and_the_list():
    portal = intune.CompanyPortal(enrolled=True, apps=[
        intune.CompanyApp(kind="win32", app_id=SEVEN_ZIP, name="7-Zip", installed=True)
    ])
    entry = SoftwareEntry(name="7-Zip", winget_id="7zip.7zip")
    intune.match(portal, [entry])
    record = SoftwareInventory(entries=[entry], company_portal=portal).to_json()

    assert record["applications"][0]["company_portal"]["id"] == SEVEN_ZIP
    assert record["company_portal"]["apps"][0]["name"] == "7-Zip"
    # And a machine with no company apps has no such section at all.
    assert "company_portal" not in SoftwareInventory(entries=[entry]).to_json()


# --- on the new machine ------------------------------------------------------
def software_record() -> dict:
    flag = {"kind": "win32", "id": SEVEN_ZIP, "name": "7-Zip (Company)"}
    return {
        "applications": [
            {"name": "7-Zip", "winget_id": "7zip.7zip", "company_portal": flag},
            {"name": "Contoso Timesheet", "company_portal": {"kind": "msi", "name": "x"}},
            {"name": "Mozilla Firefox", "winget_id": "Mozilla.Firefox"},
            {"name": "Bespoke Tool"},
        ],
        "winget_export": {"Sources": [{"Packages": [
            {"PackageIdentifier": "7zip.7zip"}, {"PackageIdentifier": "Mozilla.Firefox"},
        ]}]},
        "company_portal": {"enrolled": True, "apps": [
            flag,
            {"kind": "msi", "name": "Contoso Timesheet"},
            {"kind": "win32", "id": "not-a-guid\"><script>", "name": "<b>Odd</b> & Co"},
            {"kind": "win32", "id": "", "name": ""},
            "nonsense",
        ]},
    }


def manifest() -> dict:
    return {"items": [{
        "id": "software:inventory", "category": "software", "kind": "record",
        "title": "software", "action": "capture", "sensitivity": "normal",
        "record": software_record(),
    }]}


def test_on_a_managed_machine_company_apps_are_left_to_company_portal(tmp_path: Path):
    """Company Portal has them there, in the organisation's own build. Fetching
    the public one too leaves Intune looking at a program it did not install."""
    artifacts = reinstall.write_artifacts(manifest(), tmp_path, company_managed=True)

    assert reinstall.package_identifiers(artifacts.winget_import) == ["Mozilla.Firefox"]
    assert artifacts.reinstallable_count == 1
    by_hand = artifacts.manual_list.read_text(encoding="utf-8")
    assert "Bespoke Tool" in by_hand and "Contoso Timesheet" not in by_hand
    assert artifacts.company_count == 3 and artifacts.company_managed

    page = artifacts.company_apps.read_text(encoding="utf-8")
    assert f'href="https://portal.manage.microsoft.com/apps/{SEVEN_ZIP}"' in page
    assert "7-Zip (Company)" in page and "Contoso Timesheet" in page
    assert "were <b>not</b>" in page


def test_on_a_private_machine_they_are_reinstalled_like_anything_else(tmp_path: Path):
    """Company Portal will not have them there. Leaving them out would mean the
    programs simply did not come back."""
    artifacts = reinstall.write_artifacts(manifest(), tmp_path, company_managed=False)

    assert reinstall.package_identifiers(artifacts.winget_import) == [
        "7zip.7zip", "Mozilla.Firefox"
    ]
    assert "Contoso Timesheet" in artifacts.manual_list.read_text(encoding="utf-8")
    page = artifacts.company_apps.read_text(encoding="utf-8")
    assert "<b>not</b> managed" in page and "IT department" in page


def test_nothing_from_a_backup_becomes_markup_or_part_of_a_link(tmp_path: Path):
    """The list comes out of a bundle and goes into a web page."""
    artifacts = reinstall.write_artifacts(manifest(), tmp_path, company_managed=True)
    page = artifacts.company_apps.read_text(encoding="utf-8")

    assert "<script>" not in page and "not-a-guid" not in page
    assert "&lt;b&gt;Odd&lt;/b&gt; &amp; Co" in page


def test_when_every_installable_program_is_the_companys_no_empty_install_is_offered(
    tmp_path: Path,
):
    record = software_record()
    record["applications"] = record["applications"][:2]
    record["winget_export"]["Sources"][0]["Packages"] = [{"PackageIdentifier": "7zip.7zip"}]
    data = manifest()
    data["items"][0]["record"] = record

    artifacts = reinstall.write_artifacts(data, tmp_path, company_managed=True)

    assert artifacts.winget_import is None
    assert artifacts.anything_to_do and artifacts.company_apps.is_file()


def test_a_backup_without_company_apps_writes_no_such_page(tmp_path: Path):
    data = manifest()
    del data["items"][0]["record"]["company_portal"]
    artifacts = reinstall.write_artifacts(data, tmp_path, company_managed=True)
    assert artifacts.company_apps is None and artifacts.company_count == 0
    assert not (artifacts.directory / reinstall.COMPANY_APPS_FILE).exists()


# --- end to end --------------------------------------------------------------
def test_from_the_old_machines_records_to_the_new_machines_list(tmp_path: Path, monkeypatch):
    from conftest import write
    from winmigrate import capture as capture_mod
    from winmigrate import restore as restore_mod
    from winmigrate.capture import CaptureOptions
    from winmigrate.config import ScanConfig
    from winmigrate.restore import RestoreOptions
    from winmigrate.scan import run_scan

    env = managed_machine(tmp_path)
    write(env.profile_root, "Documents/note.txt", 10)
    config = ScanConfig(profile_root=env.profile_root)
    scan = run_scan(config, env)

    followup = next(f for f in scan.followups if f.id == "software:company_portal")
    assert "3 organisation program(s)" in followup.title
    assert "Contoso Timesheet" in followup.why
    software = next(item for item in scan.items if item.id == "software:inventory")
    assert len(software.record["company_portal"]["apps"]) == 3
    # An inventory fingerprints a machine; it stays out of the plaintext listing.
    assert software.record_public is False

    bundle = tmp_path / "work.dat"
    capture_mod.capture(
        scan, CaptureOptions(output=bundle, passphrase="pw", use_vss=False), config, env
    )
    monkeypatch.setattr(restore_mod, "_company_managed", lambda: True)
    report = restore_mod.restore(
        RestoreOptions(bundle=bundle, passphrase="pw", destination=tmp_path / "new")
    )

    assert report.artifacts.company_managed and report.artifacts.company_count == 3
    page = report.artifacts.company_apps.read_text(encoding="utf-8")
    assert SEVEN_ZIP in page and "Contoso Timesheet" in page
    by_hand = report.artifacts.manual_list.read_text(encoding="utf-8")
    assert "Notepad++" in by_hand and "7-Zip" not in by_hand


def test_whether_this_machine_is_managed_is_asked_of_this_machine(monkeypatch):
    from winmigrate import restore as restore_mod

    monkeypatch.setattr(intune, "is_enrolled", lambda env: True)
    assert restore_mod._company_managed() is True

    def broken(env):
        raise OSError("registry unavailable")

    # Not knowing means a private machine: nothing is left out on a guess.
    monkeypatch.setattr(intune, "is_enrolled", broken)
    assert restore_mod._company_managed() is False
