"""Monitor arrangements, one per set of screens the machine has met.

Windows keys each layout on the identities of the monitors attached at the
time, read from their EDID. Plug a new laptop into the same dock and the key
matches, which is exactly why this is worth carrying: the arrangement someone
spent time getting right is not in any folder and not in any account, it is in
the registry of the machine about to be wiped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate.models import Category, Kind
from winmigrate.platform_win import Environment
from winmigrate.scan import displays

CONF = displays.CONFIGURATION_KEY
DPI = displays.PER_MONITOR_SETTINGS_KEY


def env_with(registry: dict) -> Environment:
    return Environment.fixture(Path("/tmp/p"), registry)


def desk_registry() -> dict:
    return {
        f"HKLM\\{CONF}\\DELA1CF^SAM0F97\\00": {
            "PrimSurfSize.cx": 3840,
            "PrimSurfSize.cy": 2160,
            "Position.cx": 0,
            "Position.cy": 0,
            "Orientation": 0,
            "RefreshRate.Numerator": 60,
            "RefreshRate.Denominator": 1,
            "MonitorID": "DELA1CF",
        },
        f"HKLM\\{CONF}\\DELA1CF^SAM0F97\\01": {
            "PrimSurfSize.cx": 2560,
            "PrimSurfSize.cy": 1440,
            "Position.cx": 3840,
            "Position.cy": 0,
            "Orientation": 1,
            "RefreshRate.Numerator": 144,
            "RefreshRate.Denominator": 1,
            "MonitorID": "SAM0F97",
        },
    }


def test_a_docked_arrangement_is_decoded_into_something_a_person_can_act_on():
    items, followups = displays.scan_displays(env_with(desk_registry()))
    item = items[0]
    assert item.kind is Kind.RECORD
    assert item.category is Category.DISPLAYS

    screens = item.record["layouts"][0]["screens"]
    assert [s["width"] for s in screens] == [3840, 2560]
    assert screens[0]["primary"] is True
    assert screens[1]["primary"] is False
    assert screens[1]["orientation"] == "portrait"
    assert screens[1]["position_x"] == 3840

    written = "\n".join(followups[0].steps)
    assert "3840×2160" in written and "144 Hz" in written and "portrait" in written


def test_the_older_registry_shape_is_read_too():
    """Some Windows versions put a timestamp between the monitor set and the
    screens under it. Encoding one version's shape would mean silently finding
    nothing on the other."""
    registry = {
        f"HKLM\\{CONF}\\LEN40BC\\132745601234567890\\00": {
            "ActiveSize.cx": 1920,
            "ActiveSize.cy": 1200,
            "Position.cx": 0,
            "Position.cy": 0,
            "MonitorID": "LEN40BC",
        }
    }
    items, _ = displays.scan_displays(env_with(registry))
    screens = items[0].record["layouts"][0]["screens"]
    assert len(screens) == 1 and screens[0]["width"] == 1920


def test_a_key_that_is_not_a_screen_is_skipped_rather_than_invented():
    """GraphicsDrivers holds plenty that is not a monitor. A key with no size
    is not one, and guessing would put a phantom screen in the instructions."""
    registry = dict(desk_registry())
    registry[f"HKLM\\{CONF}\\DELA1CF^SAM0F97\\SomethingElse"] = {"Flags": 3}
    items, _ = displays.scan_displays(env_with(registry))
    assert len(items[0].record["layouts"][0]["screens"]) == 2


def test_manufacturers_are_named_where_they_are_known_and_left_alone_where_not():
    """The first three letters are the PNP id, the only part meaningful without
    a lookup. Inventing a name for an unknown panel would be worse than showing
    the id someone can match against the label on the back."""
    assert displays.monitor_name("DELA1CF") == "Dell DELA1CF"
    assert displays.monitor_name("SAM0F97") == "Samsung SAM0F97"
    assert displays.monitor_name("GSM5B09") == "LG GSM5B09"
    assert displays.monitor_name("ZZZ1234") == "ZZZ1234"
    # A fully qualified device path keeps only its last component.
    assert displays.monitor_name(r"MONITOR\DELA1CF\{4d36e96e}\0004") == "Dell DELA1CF"


def test_per_user_scaling_is_folded_in():
    """The scaling is in HKCU: it is the user's choice rather than the
    machine's, which makes it the part most worth carrying."""
    registry = dict(desk_registry())
    registry[f"HKCU\\{DPI}\\DELA1CF"] = {"DpiValue": 2}
    items, _ = displays.scan_displays(env_with(registry))
    screens = {s["monitor_id"]: s for s in items[0].record["layouts"][0]["screens"]}
    assert screens["DELA1CF"]["scaling_percent"] == 150
    assert screens["SAM0F97"]["scaling_percent"] == 0  # nothing recorded


def test_the_recommended_scaling_is_not_guessed_at():
    """DpiValue is an offset from whatever the panel recommends, not an
    absolute. 0 is by far the most common value and means "recommended" --
    reporting it as 100% would be a number the tool made up."""
    assert displays._dpi_step_to_percent(0) == 0
    assert displays._dpi_step_to_percent(1) == 125
    assert displays._dpi_step_to_percent(3) == 175
    # Out of range is clamped rather than crashing on an index.
    assert displays._dpi_step_to_percent(999) == displays.DPI_STEPS[-1]


def test_arrangements_are_ordered_by_how_many_screens_they_have_and_capped():
    """A machine accumulates one of these for every combination of screens it
    has ever seen, meeting-room projectors included. All of them would bury the
    two or three that are someone's actual desks."""
    registry = {}
    for index in range(displays.MAX_LAYOUTS + 6):
        registry[f"HKLM\\{CONF}\\SET{index:02}\\00"] = {
            "PrimSurfSize.cx": 1920, "PrimSurfSize.cy": 1080,
            "Position.cx": 0, "Position.cy": 0, "MonitorID": f"AAA{index:04}",
        }
    registry.update(desk_registry())

    layouts = displays.read_layouts(env_with(registry))
    assert len(layouts) == displays.MAX_LAYOUTS
    assert len(layouts[0].screens) == 2  # the desk, ahead of every single-screen set


def test_nothing_recorded_means_no_item_and_no_followup():
    assert displays.scan_displays(env_with({})) == ([], [])


def test_the_arrangement_is_never_applied_automatically():
    """The stored form sits alongside adapter LUIDs belonging to the graphics
    hardware that wrote it. Replaying that onto a different GPU is how a machine
    ends up booting to a screen that never lights up, and a backup tool that can
    leave someone with no display has failed at something more important than
    remembering where their monitors were.
    """
    from winmigrate.models import RestoreStrategy

    items, followups = displays.scan_displays(env_with(desk_registry()))
    item = items[0]
    # A record, not files: there is no archive path, so restore cannot write it.
    assert item.kind is Kind.RECORD
    assert item.archive_path is None
    assert item.restore.strategy is RestoreStrategy.GUIDED
    assert any("not restored" in (n.detail or n.message).lower() for n in item.notes)
    assert "re-create" in followups[0].title.lower()


def test_monitor_ids_stay_out_of_the_plaintext_sidecar():
    """They carry serial numbers off the panel's EDID. Harmless, but there is no
    reason to put them in the one file that sits unencrypted beside the bundle."""
    from winmigrate import manifest as manifest_mod
    from winmigrate.models import ScanResult

    items, _ = displays.scan_displays(env_with(desk_registry()))
    result = ScanResult(source=manifest_mod.detect_source_machine("/tmp/p"))
    result.items = items
    sidecar = manifest_mod.dumps(manifest_mod.public_view(manifest_mod.build(result)))
    assert "DELA1CF" not in sidecar
    assert "settings:display_layouts" in sidecar  # present, just not detailed


@pytest.mark.parametrize(
    "value, expected",
    [(0, "landscape"), (1, "portrait"), (2, "landscape (flipped)"), (3, "portrait (flipped)")],
)
def test_orientations_are_named(value, expected):
    assert displays.ORIENTATIONS[value] == expected
