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
        Step.PASSWORDS,
        Step.CREDENTIALS,
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
    assert previous_step(Step.PASSWORDS) is Step.SELECT
    assert previous_step(Step.CREDENTIALS) is Step.PASSWORDS
    assert previous_step(Step.DESTINATION) is Step.CREDENTIALS
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
    assert next_label(Step.CONFIRM) == "Start the backup"
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
    assert len(progress_steps()) == 7


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
        Step.SOFTWARE,
        Step.INSTALLING,
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
    data = WizardData(mode=Mode.RESTORE, destination="", bundle_passphrase="pw")
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

    # Every colour, which is every field but the flag saying which palette it is.
    fields = {
        f.name for f in dataclasses.fields(theme.Palette) if f.type in ("str", str)
    } - {"dark"}
    assert len(fields) >= 10, "the check has lost track of the palette's shape"
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
        # Against the frosted panel, not the flat page colour: the panel is
        # what the text is actually read on, and it is tinted by the wash
        # behind it, so checking the wrong one would pass while the real
        # surface drifted towards the ink.
        page = brightness(theme.surfaces_for(palette).page)
        for name in ("ink", "ink_soft", "accent", "secret", "bad"):
            assert abs(brightness(getattr(palette, name)) - page) > 60, (palette, name)


def test_neither_palette_uses_the_vista_widget_theme():
    """vista draws its widgets from Windows' own bitmaps. They cannot be
    recoloured and they ignore every background handed to them, so under vista
    an accent-filled button is simply a grey button and a frosted panel is
    framed in chrome that does not match it. Both themes use clam, which is
    drawn from the colours it is given."""

    class Recorder:
        def __init__(self):
            self.used = []

        def theme_use(self, name):
            self.used.append(name)

        def configure(self, *a, **k):
            pass

        def map(self, *a, **k):
            pass

        def layout(self, *a, **k):
            pass

    for palette in (theme.DARK, theme.LIGHT):
        recorder = Recorder()
        theme.apply(recorder, "Segoe UI", palette)
        assert recorder.used == ["clam"], palette



def test_neither_confirm_page_lets_an_empty_passphrase_through(tmp_path: Path):
    """The field is emptied once the job it was for is finished, and a user who
    comes back to retry would otherwise find the button live and the attempt
    failing with "the passphrase is wrong, or the file has been altered" --
    untrue twice over, and it sends them looking at their backup for a fault
    that is not there.
    """
    backup = WizardData(output_path=str(tmp_path / "b.dat"), passphrase="")
    verdict = check(Step.CONFIRM, backup)
    assert verdict.ok is False and "passphrase" in verdict.message.lower()
    backup.passphrase = "hunter2"
    assert check(Step.CONFIRM, backup).ok is True

    restore = WizardData(
        mode=Mode.RESTORE, destination=str(tmp_path), bundle_passphrase=""
    )
    verdict = check(Step.RESTORE_CONFIRM, restore)
    assert verdict.ok is False and "passphrase" in verdict.message.lower()
    restore.bundle_passphrase = "hunter2"
    assert check(Step.RESTORE_CONFIRM, restore).ok is True



def test_a_palette_knows_whether_it_is_dark_rather_than_being_recognised_by_identity():
    """apply() used to decide with ``palette is DARK``, and identity stops being
    true the moment the module is imported twice -- a frozen build, a reload, a
    test that clears sys.modules. It does not raise; it quietly hands a dark
    window the light treatment.

    The widget theme no longer turns on it, but the frosting still does: a dark
    panel is veiled towards white a fraction as hard as a light one, because
    over a near-black field a heavy white veil is a grey slab. Getting that
    backwards on a copied palette would light the whole window up."""
    import dataclasses

    assert theme.LIGHT.dark is False and theme.DARK.dark is True
    assert theme.palette_for(True).dark is True

    # A copy is a different object and must still be treated as dark.
    copied = dataclasses.replace(theme.DARK)
    assert copied is not theme.DARK
    assert theme.surfaces_for(copied) == theme.surfaces_for(theme.DARK)


# --- how the pages read -----------------------------------------------------
JARGON = (
    "passphrase",   # a security person's word for a password
    "bundle",       # what the code calls the file; the user calls it a backup
    "manifest",
    "sidecar",
    "shadow copy",
    "item",         # a row in a list is not an "item" to anybody but a programmer
    ".dat",         # a file extension is not a description
    "elevation",
)


def test_no_page_speaks_in_jargon():
    """Every one of these has a plain word that means the same thing, and the
    person this is for stops reading at the first one that does not."""
    from winmigrate.gui.wizard import TITLES

    for step, (heading, subtitle) in TITLES.items():
        text = f"{heading} {subtitle}".lower()
        for word in JARGON:
            assert word not in text, f"{step.value}: {word}"


def test_no_page_says_the_programs_name_at_the_person_using_it():
    """"WinMigrate never reads a password store" is how a README talks. A
    window says "nothing here reads them", because there is only one thing in
    the room that could."""
    from winmigrate.gui.wizard import TITLES

    for step, (heading, subtitle) in TITLES.items():
        assert "winmigrate" not in f"{heading} {subtitle}".lower(), step.value


def test_every_page_says_what_it_is_for_before_what_it_is_safe_from():
    """The reassurance matters -- this copies somebody's whole life off a
    computer -- but it is the answer to a question they have not asked yet."""
    from winmigrate.gui.wizard import TITLES

    for step, (heading, subtitle) in TITLES.items():
        assert heading, step.value
        assert not heading.endswith("."), step.value
        assert subtitle.endswith("."), step.value
        # The first sentence is the one that gets read.
        first = subtitle.split(".")[0].lower()
        assert not first.startswith("nothing here"), step.value


def test_the_relaunch_carries_the_branch_and_the_tick_and_nothing_secret():
    """Nothing typed later goes on a command line: the backup's password is
    collected pages after this, in the process that will use it."""
    from winmigrate.gui.elevate import ALREADY_TRIED_FLAG, forward_arguments

    arguments = forward_arguments(mode="restore", restore_as_admin=True)

    assert arguments[:2] == ["gui", ALREADY_TRIED_FLAG]
    assert "--mode" in arguments and "restore" in arguments
    assert "--restore-as-admin" in arguments
    assert not any("pass" in part.lower() for part in arguments)


def test_the_offer_is_made_for_either_reason_and_never_twice():
    """A shadow copy on the way out, installing programs on the way in."""
    from winmigrate.gui import elevate

    assert elevate.should_offer(False, False) is False   # nothing asked for it
    assert elevate.should_offer(True, True) is False     # this is the relaunched copy


def test_the_tick_boxes_are_styled_with_option_names_clam_actually_has():
    """ttk accepts an option a theme has never heard of without a word.

    "indicatorcolor" reads like the right name and clam has no such option, so
    a style written against it silently does nothing: the boxes keep clam's
    default white fill, which on the dark theme makes the *unticked* ones the
    brightest thing on the page. It looked styled and was inverted, and nothing
    failed. clam's indicator takes indicatorbackground and indicatorforeground.
    """

    class Recorder:
        def __init__(self):
            self.options: dict[str, set[str]] = {}

        def theme_use(self, name):
            pass

        def configure(self, name, **kwargs):
            self.options.setdefault(name, set()).update(kwargs)

        def map(self, name, **kwargs):
            self.options.setdefault(name, set()).update(kwargs)

        def layout(self, *a, **k):
            pass

    recorder = Recorder()
    theme.apply(recorder, "Segoe UI", theme.DARK)

    indicators = [
        name for name in recorder.options
        if name.endswith(("TCheckbutton", "TRadiobutton"))
    ]
    assert indicators, "no tick box or radio style was configured at all"
    for name in indicators:
        used = recorder.options[name]
        assert "indicatorbackground" in used, name
        assert "indicatorforeground" in used, name
        assert "indicatorcolor" not in used, (
            f"{name} uses indicatorcolor, which clam ignores"
        )
