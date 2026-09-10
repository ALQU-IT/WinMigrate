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
    for step in (Step.WELCOME, Step.SCANNING, Step.WORKING, Step.DONE):
        assert not can_go_back(step), step
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
