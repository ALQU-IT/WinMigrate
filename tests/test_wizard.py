"""The wizard's rules, which are deliberately not in the widgets.

A wizard is mostly rules: which page follows which, when the forward button
works, what Back means where. Rules in button callbacks are rules nobody can
test, because tkinter needs a display and CI has none.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate.gui import theme
from winmigrate.gui.wizard import (
    AUTOMATIC,
    Mode,
    ORDER,
    Step,
    WizardData,
    can_go_back,
    check,
    next_label,
    next_step,
    previous_step,
    progress_steps,
    rail_index,
    title,
)


def test_the_pages_run_in_the_order_an_installer_taught_everyone():
    assert ORDER == (
        Step.CHOOSE,
        Step.WELCOME,
        Step.SCANNING,
        Step.SELECT,
        Step.DESTINATION,
        Step.CONFIRM,
        Step.WORKING,
        Step.DONE,
    )
    assert next_step(Step.DONE) is None


def test_back_is_not_offered_where_it_would_mean_nothing():
    """Not while something is running -- there is no half-finished state to
    return to -- and not once it has finished, because the work is done."""
    for step in (Step.CHOOSE, Step.SCANNING, Step.WORKING, Step.DONE):
        assert not can_go_back(step), step
    assert previous_step(Step.WELCOME) is Step.CHOOSE
    assert previous_step(Step.SELECT) is Step.WELCOME
    assert previous_step(Step.DESTINATION) is Step.SELECT
    assert previous_step(Step.CONFIRM) is Step.DESTINATION


def test_back_skips_the_pages_that_run_by_themselves():
    """Back from Select goes to Welcome, not to Scanning. Landing on a progress
    bar that immediately runs again is not going back, it is going forward
    sideways."""
    assert previous_step(Step.SELECT) not in AUTOMATIC
    assert previous_step(Step.DESTINATION) not in AUTOMATIC


def test_the_forward_button_says_what_it_will_do():
    """A wizard whose button always reads "Next" makes the user find out what it
    did by pressing it. This one commits to a backup, so it says so."""
    assert next_label(Step.WELCOME) == "Scan"
    assert next_label(Step.CONFIRM) == "Start backup"
    assert next_label(Step.DONE) == "Finish"
    assert next_label(Step.SELECT) == "Next"


def test_you_cannot_leave_welcome_without_a_profile():
    assert check(Step.WELCOME, WizardData(profile_root="")).ok is False
    assert check(Step.WELCOME, WizardData(profile_root="   ")).ok is False
    assert check(Step.WELCOME, WizardData(profile_root="C:/Users/a")).ok is True


def test_you_cannot_leave_select_having_chosen_nothing():
    verdict = check(Step.SELECT, WizardData(selected=set()))
    assert verdict.ok is False
    assert "nothing" in verdict.message.lower()
    assert check(Step.SELECT, WizardData(selected={"files:documents"})).ok is True


@pytest.mark.parametrize(
    "passphrase, confirm, expect_ok, expect_word",
    [
        ("", "", False, "required"),
        ("hunter2", "", False, "match"),
        ("hunter2", "hunter3", False, "match"),
        ("hunter2", "hunter2", True, ""),
    ],
)
def test_the_passphrase_must_be_present_and_typed_twice(
    tmp_path: Path, passphrase, confirm, expect_ok, expect_word
):
    """The one field with no recovery path. Typing it once is how someone ends
    up with a backup nobody can open, including them."""
    data = WizardData(
        output_path=str(tmp_path / "b.dat"),
        passphrase=passphrase,
        passphrase_confirm=confirm,
    )
    verdict = check(Step.DESTINATION, data)
    assert verdict.ok is expect_ok
    if expect_word:
        assert expect_word in verdict.message.lower()


def test_a_destination_in_a_folder_that_does_not_exist_is_refused(tmp_path: Path):
    data = WizardData(
        output_path=str(tmp_path / "nope" / "b.dat"),
        passphrase="x",
        passphrase_confirm="x",
    )
    verdict = check(Step.DESTINATION, data)
    assert verdict.ok is False and "does not exist" in verdict.message


def test_the_automatic_pages_never_enable_the_forward_button():
    """Scanning and Working advance when their worker finishes. A live forward
    button there would skip a page whose whole content is the work in progress."""
    for step in AUTOMATIC:
        assert check(step, WizardData()).ok is False


def test_the_rail_folds_the_working_pages_into_the_step_they_belong_to():
    """Nobody thinks of "scanning" as a stage of a backup separate from
    choosing. A rail entry that highlights for half a second is noise."""
    assert rail_index(Step.SCANNING) == rail_index(Step.WELCOME)
    assert rail_index(Step.WORKING) == rail_index(Step.CONFIRM)
    assert rail_index(Step.SELECT) == 1
    assert len(progress_steps()) == 5


def test_every_page_has_a_heading_and_a_sentence_under_it():
    for step in Step:
        heading, subtitle = title(step)
        assert heading and not heading.endswith("."), step
        assert subtitle, step


def test_the_rail_shows_progress_without_relying_on_colour():
    """Ticks for done, a dot for here, a ring for ahead. Colour alone would
    leave someone who cannot distinguish it with no idea where they are."""
    assert theme.rail_marker(0, 2) == "✓"
    assert theme.rail_marker(2, 2) == "●"
    assert theme.rail_marker(4, 2) == "○"
    assert theme.rail_style(2, 2) == theme.RAIL_ON
    assert theme.rail_style(0, 2) == theme.RAIL_DONE
    assert theme.rail_style(4, 2) == theme.RAIL_OFF


def test_the_font_falls_back_rather_than_failing():
    """Segoe UI is on every supported Windows, but the window must still open on
    a machine that has none of the preferred faces."""
    assert theme.font_family(lambda: ["Segoe UI", "Arial"]) == "Segoe UI"
    assert theme.font_family(lambda: ["Arial"]) == theme.FAMILY[-1]

    def explode():
        raise RuntimeError("no display")

    assert theme.font_family(explode) == theme.FAMILY[-1]



# --- the two jobs ----------------------------------------------------------
def test_the_first_page_is_the_choice_between_the_two_jobs():
    """Someone opening this on a new machine wants to put a backup back, and
    someone on the old one wants to make it. Guessing which would be wrong half
    the time, and the wrong guess writes files."""
    from winmigrate.gui.wizard import order

    assert order(Mode.BACKUP)[0] is Step.CHOOSE
    assert order(Mode.RESTORE)[0] is Step.CHOOSE
    assert next_step(Step.CHOOSE, Mode.BACKUP) is Step.WELCOME
    assert next_step(Step.CHOOSE, Mode.RESTORE) is Step.SOURCE


def test_the_restore_branch_runs_in_its_own_order():
    from winmigrate.gui.wizard import RESTORE_ORDER

    assert RESTORE_ORDER == (
        Step.CHOOSE,
        Step.SOURCE,
        Step.OPENING,
        Step.RESTORE_SELECT,
        Step.RESTORE_CONFIRM,
        Step.RESTORING,
        Step.RESTORE_DONE,
    )
    assert next_step(Step.RESTORE_DONE, Mode.RESTORE) is None


def test_back_never_crosses_from_one_job_to_the_other():
    """The two branches share only the first page. Back from a restore page must
    land on a restore page, or the window shows a form whose data was never
    gathered."""
    from winmigrate.gui.wizard import RESTORE_ORDER, BACKUP_ORDER

    for step in RESTORE_ORDER:
        earlier = previous_step(step, Mode.RESTORE)
        assert earlier is None or earlier in RESTORE_ORDER, step
    for step in BACKUP_ORDER:
        earlier = previous_step(step, Mode.BACKUP)
        assert earlier is None or earlier in BACKUP_ORDER, step


def test_back_is_refused_once_files_have_been_written():
    """A restore that has written files is not something a button can undo."""
    assert not can_go_back(Step.RESTORE_DONE, Mode.RESTORE)
    assert not can_go_back(Step.RESTORING, Mode.RESTORE)


def test_you_cannot_open_a_backup_without_naming_one(tmp_path: Path):
    data = WizardData(mode=Mode.RESTORE)
    assert check(Step.SOURCE, data).ok is False

    data.bundle_path = str(tmp_path / "missing.dat")
    verdict = check(Step.SOURCE, data)
    assert verdict.ok is False and "not there" in verdict.message

    bundle = tmp_path / "b.dat"
    bundle.write_bytes(b"x")
    data.bundle_path = str(bundle)
    verdict = check(Step.SOURCE, data)
    assert verdict.ok is False and "passphrase" in verdict.message.lower()

    data.bundle_passphrase = "hunter2"
    assert check(Step.SOURCE, data).ok is True


def test_the_restore_passphrase_is_not_asked_for_twice(tmp_path: Path):
    """Typing it twice guards against a typo becoming an unopenable backup. On
    the way back in a typo just fails to open, immediately and harmlessly, so
    asking twice would be ceremony."""
    bundle = tmp_path / "b.dat"
    bundle.write_bytes(b"x")
    data = WizardData(
        mode=Mode.RESTORE, bundle_path=str(bundle), bundle_passphrase="typed once"
    )
    assert check(Step.SOURCE, data).ok is True


def test_you_cannot_restore_nothing():
    data = WizardData(mode=Mode.RESTORE, restore_selected=set())
    assert check(Step.RESTORE_SELECT, data).ok is False
    data.restore_selected = {"files:documents"}
    assert check(Step.RESTORE_SELECT, data).ok is True


def test_the_destination_must_be_somewhere_that_could_exist(tmp_path: Path):
    data = WizardData(mode=Mode.RESTORE, destination="")
    assert check(Step.RESTORE_CONFIRM, data).ok is False

    data.destination = str(tmp_path / "nowhere" / "deeper")
    assert check(Step.RESTORE_CONFIRM, data).ok is False

    # A folder that does not exist yet but whose parent does is fine: restore
    # creates it, which is the normal case for an empty profile.
    data.destination = str(tmp_path / "new-profile")
    assert check(Step.RESTORE_CONFIRM, data).ok is True

    data.destination = str(tmp_path)
    assert check(Step.RESTORE_CONFIRM, data).ok is True


def test_the_rail_describes_whichever_job_is_running():
    from winmigrate.gui.wizard import progress_steps

    assert progress_steps(Mode.BACKUP) != progress_steps(Mode.RESTORE)
    assert rail_index(Step.OPENING, Mode.RESTORE) == rail_index(Step.SOURCE, Mode.RESTORE)
    assert rail_index(Step.RESTORING, Mode.RESTORE) == rail_index(
        Step.RESTORE_CONFIRM, Mode.RESTORE
    )
    assert rail_index(Step.RESTORE_DONE, Mode.RESTORE) == len(progress_steps(Mode.RESTORE)) - 1


def test_every_page_of_both_branches_has_a_heading():
    for step in Step:
        heading, subtitle = title(step)
        assert heading and subtitle, step


# --- light and dark --------------------------------------------------------
def test_windows_dark_mode_is_read_from_the_registry():
    """AppsUseLightTheme is the value that governs application windows.
    SystemUsesLightTheme is the taskbar and Start menu and can differ, so it is
    deliberately not the one consulted.

    The name reads backwards -- it names the *light* theme -- so 0 means dark.
    """
    from winmigrate.platform_win import Environment

    def env_with(values):
        return Environment.fixture(Path("/tmp/p"), {f"HKCU\\{theme.PERSONALIZE_KEY}": values})

    assert theme.detect_dark_mode(env_with({"AppsUseLightTheme": 0})) is True
    assert theme.detect_dark_mode(env_with({"AppsUseLightTheme": 1})) is False

    # The taskbar setting must not be mistaken for the application one.
    assert theme.detect_dark_mode(env_with({"SystemUsesLightTheme": 0})) is False


def test_anything_unreadable_means_light():
    """The value is absent on older builds and on every non-Windows host, and
    light is what Windows itself falls back to. Guessing dark would put white
    text on a white page for anyone whose registry could not be read."""
    from winmigrate.platform_win import Environment

    assert theme.detect_dark_mode(Environment.fixture(Path("/tmp/p"), {})) is False
    assert theme.detect_dark_mode(
        Environment.fixture(Path("/tmp/p"), {f"HKCU\\{theme.PERSONALIZE_KEY}": {}})
    ) is False
    # A string where a number belongs is not a vote for dark.
    assert theme.detect_dark_mode(
        Environment.fixture(Path("/tmp/p"), {f"HKCU\\{theme.PERSONALIZE_KEY}": {"AppsUseLightTheme": "0"}})
    ) is False


def test_both_palettes_define_every_colour():
    """A colour added to one palette and forgotten in the other is a widget
    that renders black on black -- the sort of thing nobody notices until it is
    in front of someone."""
    import dataclasses

    fields = {f.name for f in dataclasses.fields(theme.Palette)}
    for palette in (theme.LIGHT, theme.DARK):
        for name in fields:
            value = getattr(palette, name)
            assert isinstance(value, str) and value.startswith("#"), (palette, name)
            assert len(value) == 7, (palette, name, value)


def test_the_two_palettes_are_actually_different_and_the_right_way_round():
    def brightness(colour: str) -> int:
        r, g, b = (int(colour[i : i + 2], 16) for i in (1, 3, 5))
        return (r * 299 + g * 587 + b * 114) // 1000

    # Dark pages are dark, light pages are light, and text contrasts with both.
    assert brightness(theme.DARK.page) < 60
    assert brightness(theme.LIGHT.page) > 200
    assert brightness(theme.DARK.ink) > 200
    assert brightness(theme.LIGHT.ink) < 60

    for palette in (theme.LIGHT, theme.DARK):
        page = brightness(palette.page)
        for name in ("ink", "ink_soft", "accent", "secret", "bad"):
            assert abs(brightness(getattr(palette, name)) - page) > 60, (palette, name)


def test_the_dark_palette_does_not_use_the_vista_widget_theme():
    """vista draws its widgets from Windows' own light bitmaps, which cannot be
    recoloured. A dark page framed in white chrome looks broken, so clam -- which
    is drawn from the colours it is given -- is used instead."""

    class Recorder:
        def __init__(self):
            self.used = []

        def theme_use(self, name):
            self.used.append(name)

        def configure(self, *a, **k):
            pass

        def map(self, *a, **k):
            pass

    dark = Recorder()
    theme.apply(dark, "Segoe UI", theme.DARK)
    assert dark.used == ["clam"]

    light = Recorder()
    theme.apply(light, "Segoe UI", theme.LIGHT)
    assert light.used[0] == "vista"
