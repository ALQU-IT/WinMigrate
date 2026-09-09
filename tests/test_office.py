"""Office detection, licence parsing, and ODT configuration generation.

The licence tests use real-shaped ``ospp.vbs /dstatus`` output. Note what is
asserted throughout: the *last five characters* of a key, which ospp prints
itself, and never a full key. Nothing in this module reconstructs one.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from winmigrate.odt import generate_configuration
from winmigrate.platform_win import Environment
from winmigrate.scan import office

CONFIG_KEY = f"HKLM\\{office.CLICK_TO_RUN_CONFIG}"

M365_DSTATUS = """\
---Processing--------------------------
---------------------------------------
SKU ID: 0d1cbcf5-b6a8-4c8a-9f5a-1a2b3c4d5e6f
LICENSE NAME: Office 21, Office16O365ProPlusR_Subscription1 edition
LICENSE DESCRIPTION: Office 21, RETAIL(Subscription) channel
LICENSE STATUS:  ---LICENSED---
Last 5 characters of installed product key: 6MWKP
---------------------------------------
---Exiting-----------------------------
"""

RETAIL_DSTATUS = """\
---Processing--------------------------
LICENSE NAME: Office 19, Office19HomeBusinessR_Retail edition
LICENSE DESCRIPTION: Office 19, RETAIL channel
LICENSE STATUS:  ---LICENSED---
Last 5 characters of installed product key: 4T7XY
---Exiting-----------------------------
"""

VOLUME_DSTATUS = """\
LICENSE NAME: Office 21, Office21ProPlus2021VL_KMS_Client edition
LICENSE DESCRIPTION: Office 21, VOLUME_KMSCLIENT channel
LICENSE STATUS:  ---LICENSED---
Last 5 characters of installed product key: 6F7TH
"""


def test_the_click_to_run_configuration_is_read():
    values = {
        "ProductReleaseIds": "O365ProPlusRetail,VisioProRetail",
        "Platform": "x64",
        "ClientCulture": "en-us",
        "VersionToReport": "16.0.17328.20124",
        "UpdateChannel": "http://officecdn.microsoft.com/pr/492350f6-3a01-4f97-b9c0-c7c6ddf67d60",
    }
    installation = office.read_configuration(values)
    assert installation.present
    assert installation.product_ids == ["O365ProPlusRetail", "VisioProRetail"]
    assert installation.platform == "x64"
    assert installation.client_culture == "en-us"
    assert installation.channel == "Current"
    assert installation.titles == ["Microsoft 365 Apps for enterprise", "Visio Plan 2"]


def test_no_office_is_reported_as_absent_rather_than_empty_strings():
    assert not office.read_configuration({}).present
    assert not office.read_configuration({"ProductReleaseIds": ""}).present


@pytest.mark.parametrize(
    ("values", "expected"),
    [
        ({"UpdateChannel": ".../55336b82-a18d-4dd6-b5f6-9e5095c314a6"}, "MonthlyEnterprise"),
        ({"AudienceId": "7ffbc6bf-bc32-4f92-8982-f9dd17fd3114"}, "SemiAnnual"),
        ({"AudienceData": "b8f9b850-328d-4355-9145-c59439a0c4cf"}, "CurrentPreview"),
        ({"UpdateChannel": "Current"}, "Current"),
    ],
)
def test_the_update_channel_is_found_wherever_this_build_records_it(values, expected):
    assert office.resolve_channel(values) == expected


def test_an_unrecognised_channel_is_passed_through_rather_than_guessed():
    assert office.resolve_channel({"UpdateChannel": "SomethingNew"}) == "SomethingNew"
    assert office.resolve_channel({}) == ""


@pytest.mark.parametrize(
    ("text", "kind", "hint"),
    [
        (M365_DSTATUS, "subscription", "6MWKP"),
        (RETAIL_DSTATUS, "retail", "4T7XY"),
        (VOLUME_DSTATUS, "volume", "6F7TH"),
    ],
)
def test_licence_status_is_parsed_and_classified(text, kind, hint):
    licences = office.parse_ospp_dstatus(text)
    assert len(licences) == 1
    assert licences[0].activation_type == kind
    assert licences[0].status == "---LICENSED---"
    # Only the last five characters, which ospp prints itself.
    assert licences[0].key_last_five == hint
    assert len(licences[0].key_last_five) == 5


def test_multiple_licences_are_kept_separate():
    licences = office.parse_ospp_dstatus(M365_DSTATUS + RETAIL_DSTATUS)
    assert [licence.activation_type for licence in licences] == ["subscription", "retail"]


def test_empty_or_unexpected_ospp_output_yields_no_licences():
    assert office.parse_ospp_dstatus("") == []
    assert office.parse_ospp_dstatus("something went wrong") == []


def test_an_installation_with_no_licence_information_is_still_reinstallable():
    installation = office.read_configuration(
        {"ProductReleaseIds": "O365ProPlusRetail", "Platform": "x64", "ClientCulture": "en-us"}
    )
    assert installation.activation_type == "unknown"
    assert generate_configuration(installation)


def test_the_generated_configuration_reproduces_what_was_detected():
    installation = office.read_configuration(
        {
            "ProductReleaseIds": "O365ProPlusRetail,VisioProRetail",
            "Platform": "x86",
            "ClientCulture": "de-de",
            "UpdateChannel": ".../55336b82-a18d-4dd6-b5f6-9e5095c314a6",
        }
    )
    root = ET.fromstring(generate_configuration(installation))
    add = root.find("Add")
    assert add.get("OfficeClientEdition") == "32"
    assert add.get("Channel") == "MonthlyEnterprise"
    assert [product.get("ID") for product in add.findall("Product")] == [
        "O365ProPlusRetail",
        "VisioProRetail",
    ]
    assert {lang.get("ID") for lang in add.iter("Language")} == {"de-de"}


def test_the_generated_configuration_never_contains_a_product_key():
    installation = office.read_configuration(
        {"ProductReleaseIds": "HomeBusiness2021Retail", "Platform": "x64", "ClientCulture": "en-us"}
    )
    installation.licences = [office.parse_ospp_dstatus(RETAIL_DSTATUS)[0]]
    xml = generate_configuration(installation)
    assert "PIDKEY" not in xml.upper()
    assert "4T7XY" not in xml
    assert 'Name="AUTOACTIVATE" Value="0"' in xml


def test_generating_a_configuration_without_an_installation_is_refused():
    with pytest.raises(ValueError, match="no Office installation"):
        generate_configuration(office.read_configuration({}))


def test_reactivation_steps_differ_by_licence_type():
    """A subscription reactivates by signing in; retail needs the user's own key."""
    subscription = office.REACTIVATION_STEPS["subscription"]
    retail = office.REACTIVATION_STEPS["retail"]
    volume = office.REACTIVATION_STEPS["volume"]

    assert any("sign in" in step.lower() for step in subscription)
    # It may mention a key only to say there is not one to enter.
    assert any("no key to enter" in step.lower() for step in subscription)
    assert not any("enter the key" in step.lower() for step in subscription)

    assert any("enter the key" in step.lower() for step in retail)
    # And it must be honest that the tool cannot supply it.
    assert any("does not copy it" in step for step in retail)

    assert any("administrator" in step.lower() for step in volume)
    assert not any("enter the key" in step.lower() for step in volume)


def test_detect_reads_the_registry_without_touching_ospp_off_windows(tmp_path: Path):
    env = Environment.fixture(
        tmp_path,
        {CONFIG_KEY: {"ProductReleaseIds": "O365ProPlusRetail", "Platform": "x64"}},
    )
    installation = office.detect(env)
    assert installation.present
    assert installation.licences == []


# --- driven by a real ProPlus2024 + Visio machine ---------------------------
TWO_PRODUCT_DSTATUS = """\
LICENSE NAME: Office 24, Office24VisioStd2024R_Retail edition
LICENSE DESCRIPTION: Office 24, RETAIL channel
LICENSE STATUS:  ---NOTIFICATIONS---
Last 5 characters of installed product key: RGHKQ
LICENSE NAME: Office 24, Office24ProPlus2024R_Retail edition
LICENSE DESCRIPTION: Office 24, RETAIL channel
LICENSE STATUS:  ---LICENSED---
Last 5 characters of installed product key: 88KC6
"""


def test_2024_products_have_readable_titles():
    """They were reported as raw ids like ProPlus2024Retail on a real machine."""
    installation = office.read_configuration(
        {"ProductReleaseIds": "ProPlus2024Retail,VisioStd2024Retail"}
    )
    assert installation.titles == ["Office Professional Plus 2024", "Visio Standard 2024"]


def test_each_installed_product_keeps_its_own_key_hint():
    """Office and Visio install together and carry different keys.

    A single hint would send the user looking for the wrong one.
    """
    installation = office.read_configuration({"ProductReleaseIds": "ProPlus2024Retail"})
    installation.licences = office.parse_ospp_dstatus(TWO_PRODUCT_DSTATUS)
    assert installation.key_hints() == [
        ("VisioStd2024R", "RGHKQ"),
        ("ProPlus2024R", "88KC6"),
    ]
    for _product, hint in installation.key_hints():
        assert len(hint) == 5, "only the last five characters, never a whole key"


def test_a_notification_state_is_not_reported_as_activated():
    """---NOTIFICATIONS--- means installed but not activated.

    It is the state a migration most often leaves someone in, and it otherwise
    reads as success.
    """
    visio, proplus = office.parse_ospp_dstatus(TWO_PRODUCT_DSTATUS)
    assert visio.is_activated is False
    assert proplus.is_activated is True

    installation = office.read_configuration({"ProductReleaseIds": "ProPlus2024Retail"})
    installation.licences = [visio, proplus]
    assert installation.fully_activated is False


def test_activation_state_reaches_the_record():
    installation = office.read_configuration({"ProductReleaseIds": "ProPlus2024Retail"})
    installation.licences = office.parse_ospp_dstatus(TWO_PRODUCT_DSTATUS)
    record = installation.to_json()
    assert record["fully_activated"] is False
    assert [licence["activated"] for licence in record["licences"]] == [False, True]
