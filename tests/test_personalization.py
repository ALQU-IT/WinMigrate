"""The settings that make a machine feel like the one it replaced."""

from __future__ import annotations

from pathlib import Path

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.models import Category, Kind
from winmigrate.platform_win import Environment
from winmigrate.scan import personalization


def env_with(registry: dict) -> Environment:
    return Environment.fixture(Path("/tmp/p"), registry)


def a_configured_machine() -> Environment:
    """A profile somebody has actually set up for themselves."""
    return env_with({
        r"HKCU\Control Panel\Accessibility\HighContrast": {"Flags": "126"},
        r"HKCU\Software\Microsoft\Accessibility": {"TextScaleFactor": 150},
        r"HKCU\Keyboard Layout\Preload": {"1": "00000807", "2": "00000409"},
        r"HKCU\Control Panel\International": {
            "sShortDate": "dd.MM.yyyy", "sDecimal": ",", "LocaleName": "de-CH",
        },
        r"HKCU\Control Panel\Mouse": {"DoubleClickSpeed": "900", "SwapMouseButtons": "1"},
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize": {
            "AppsUseLightTheme": 0,
        },
    })


# --- reading it -------------------------------------------------------------
def test_the_settings_a_person_actually_set_are_carried():
    (item,), _ = personalization.scan_personalization(a_configured_machine())

    assert item.id == "settings:personalization"
    assert item.category is Category.PERSONALIZATION
    assert item.kind is Kind.RECORD
    settings = item.record["settings"]
    assert settings["text_scale"] == {"TextScaleFactor": 150}
    assert settings["keyboard_layout"] == {"1": "00000807", "2": "00000409"}
    assert settings["region"]["sShortDate"] == "dd.MM.yyyy"
    assert settings["theme"] == {"AppsUseLightTheme": 0}


def test_only_the_named_values_travel_out_of_a_shared_key():
    """Control Panel\\Desktop holds the screen saver, the wallpaper, and a dozen
    things describing the graphics hardware this profile last ran on. Carrying
    the key would carry all of it, onto hardware it was never read from."""
    env = env_with({
        r"HKCU\Control Panel\Desktop": {
            "SCRNSAVE.EXE": "scrnsave.scr",
            "ScreenSaveTimeOut": "600",
            "Wallpaper": r"C:\Users\someone\Pictures\lake.jpg",
            "LogPixels": 144,
            "WheelScrollLines": "3",
        },
    })

    (item,), _ = personalization.scan_personalization(env)

    assert item.record["settings"]["screensaver"] == {
        "SCRNSAVE.EXE": "scrnsave.scr", "ScreenSaveTimeOut": "600",
    }


def test_a_setting_nobody_ever_set_is_not_invented():
    """Absent and "off" are different answers, and a restore that turns one into
    the other is inventing settings nobody chose."""
    items, _ = personalization.scan_personalization(env_with({}))
    assert items == []

    (item,), _ = personalization.scan_personalization(
        env_with({r"HKCU\Software\Microsoft\Accessibility": {"TextScaleFactor": 125}})
    )
    assert list(item.record["settings"]) == ["text_scale"]


def test_accessibility_comes_first_because_it_decides_whether_the_rest_can_be_read():
    """Not decoration: the order of the table is the order somebody notices, and
    the person who set the text size is the person least able to find the screen
    that sets it."""
    slots = [setting.slot for setting in personalization.SETTINGS]
    assert slots[0].startswith("accessibility")
    assert slots.index("text_scale") < slots.index("accent_colour")
    assert slots.index("keyboard_layout") < slots.index("explorer")


# --- putting it back --------------------------------------------------------
def test_the_settings_are_written_back_with_the_types_windows_expects():
    """AccentColor is a number and sShortDate is text. Writing one as the other
    leaves Windows reading a value it will not act on, which looks exactly like
    the setting not having travelled."""
    source = a_configured_machine()
    (item,), _ = personalization.scan_personalization(source)
    target = env_with({})

    results = apply_mod.apply_personalization(item.record, target)

    assert target.registry[r"HKCU\Software\Microsoft\Accessibility"]["TextScaleFactor"] == 150
    assert isinstance(
        target.registry[r"HKCU\Software\Microsoft\Accessibility"]["TextScaleFactor"], int
    )
    assert target.registry[r"HKCU\Control Panel\International"]["sShortDate"] == "dd.MM.yyyy"
    assert target.registry[r"HKCU\Keyboard Layout\Preload"] == {
        "1": "00000807", "2": "00000409",
    }
    assert all(r.outcome is Outcome.APPLIED for r in results)


def test_a_record_naming_a_value_the_table_never_asked_for_is_refused():
    """The record comes out of a bundle, and a bundle is a file from another
    machine. The slot being a real one does not make its contents real."""
    target = env_with({})

    (result,) = apply_mod.apply_personalization(
        {"settings": {"screensaver": {
            "ScreenSaveTimeOut": "600",
            "Wallpaper": r"\\attacker\share\x.jpg",
        }}},
        target,
    )

    desktop = target.registry[r"HKCU\Control Panel\Desktop"]
    assert desktop == {"ScreenSaveTimeOut": "600"}
    assert "not carried" in result.detail


def test_the_one_value_that_names_a_program_is_checked_rather_than_trusted():
    """The screen saver is an executable Windows will later run. A bundle that
    could put an arbitrary path there would have turned a backup into a way of
    starting a program on somebody else's machine."""
    target = env_with({})

    apply_mod.apply_personalization(
        {"settings": {"screensaver": {"SCRNSAVE.EXE": r"\\attacker\share\payload.exe"}}},
        target,
    )
    assert "SCRNSAVE.EXE" not in target.registry.get(r"HKCU\Control Panel\Desktop", {})

    # A .scr that ships with Windows is what a screen saver actually is.
    apply_mod.apply_personalization(
        {"settings": {"screensaver": {"SCRNSAVE.EXE": "Bubbles.scr"}}}, target
    )
    assert target.registry[r"HKCU\Control Panel\Desktop"]["SCRNSAVE.EXE"] == "Bubbles.scr"

    # And so is one named by its full path inside Windows.
    apply_mod.apply_personalization(
        {"settings": {"screensaver": {"SCRNSAVE.EXE": r"C:\Windows\system32\Mystify.scr"}}},
        target,
    )
    assert target.registry[r"HKCU\Control Panel\Desktop"]["SCRNSAVE.EXE"] == (
        r"C:\Windows\system32\Mystify.scr"
    )


def test_a_setting_from_a_newer_version_is_named_rather_than_written_blind():
    target = env_with({})

    (result,) = apply_mod.apply_personalization(
        {"settings": {"holographic_desk": {"Depth": 3}}}, target
    )

    assert result.outcome is Outcome.SKIPPED
    assert target.registry == {}


def test_a_record_that_is_not_a_record_does_nothing():
    assert apply_mod.apply_personalization({}, env_with({})) == []
    assert apply_mod.apply_personalization({"settings": "nonsense"}, env_with({})) == []
