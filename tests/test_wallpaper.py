"""The desktop background: the picture, or the thing instead of a picture."""

from __future__ import annotations

from pathlib import Path

from winmigrate import apply as apply_mod
from winmigrate.apply import Outcome
from winmigrate.models import Category, Kind
from winmigrate.platform_win import Environment
from winmigrate.scan import wallpaper


def env_with(registry: dict, root: Path | None = None) -> Environment:
    return Environment.fixture(root or Path("/tmp/p"), registry)


def a_picture(tmp_path: Path, name: str = "lake.jpg") -> Path:
    image = tmp_path / name
    image.write_bytes(b"\xff\xd8\xff" + b"0" * 2048)
    return image


# --- reading it -------------------------------------------------------------
def test_windows_spotlight_travels_as_a_setting_not_as_todays_photograph(tmp_path: Path):
    """Spotlight is not a picture, it is a subscription to a new one every day.
    Windows still leaves the current photograph in the wallpaper value, so a
    tool that reads only that value would carry one frozen image across and
    call it the user's choice -- replacing a background that changes daily with
    a photograph of a fjord."""
    cached = a_picture(tmp_path, "spotlight-cache.jpg")
    env = env_with(
        {
            r"HKCU\Control Panel\Desktop": {"Wallpaper": str(cached), "WallpaperStyle": "10"},
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers": {
                "BackgroundType": 3,
                "BackgroundHistoryPath0": str(cached),
            },
        },
        tmp_path,
    )

    (item,), _ = wallpaper.scan_wallpaper(env)

    assert item.kind is Kind.RECORD
    assert item.record["type"] == "spotlight"
    assert item.archive_path is None and item.source_path is None
    assert "Spotlight" in item.title


def test_a_picture_travels_as_the_file_the_user_chose(tmp_path: Path):
    """Windows re-encodes the chosen picture into TranscodedWallpaper -- a JPEG
    with no extension, in AppData -- and points the wallpaper value there. The
    path the user actually picked is kept separately, and it is the one worth
    carrying: restoring a file called "TranscodedWallpaper" into someone's
    folder tells them nothing about what the picture is."""
    chosen = a_picture(tmp_path, "lake.jpg")
    transcoded = tmp_path / "TranscodedWallpaper"
    transcoded.write_bytes(b"\xff\xd8\xff" + b"1" * 2048)
    env = env_with(
        {
            r"HKCU\Control Panel\Desktop": {
                "Wallpaper": str(transcoded), "WallpaperStyle": "10", "TileWallpaper": "0",
            },
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers": {
                "BackgroundType": 0,
                "BackgroundHistoryPath0": str(chosen),
            },
        },
        tmp_path,
    )

    (item,), _ = wallpaper.scan_wallpaper(env)

    assert item.kind is Kind.FILE
    assert item.category is Category.WALLPAPER
    assert item.source_path == str(chosen)
    assert item.archive_path == "WinMigrate-Wallpaper/lake.jpg"
    assert item.record["file_name"] == "lake.jpg"
    assert item.record["style"] == "10" and item.record["style_name"] == "filled"
    # Nothing here is secret, and the sidecar is where the restore reads the
    # style from before it has decrypted anything.
    assert item.record_public is True


def test_a_background_whose_picture_has_been_deleted_carries_the_setting_only(tmp_path: Path):
    """A registry value is a path, not a promise: it survives the file being
    deleted and the drive being unplugged."""
    env = env_with(
        {
            r"HKCU\Control Panel\Desktop": {"Wallpaper": r"D:\gone\beach.jpg"},
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers": {
                "BackgroundType": 0
            },
        },
        tmp_path,
    )

    (item,), _ = wallpaper.scan_wallpaper(env)

    assert item.kind is Kind.RECORD
    assert item.record["type"] == "picture"
    assert "missing" in item.title
    assert any("not on this machine" in note.message for note in item.notes)


def test_a_solid_colour_travels_as_three_numbers(tmp_path: Path):
    env = env_with(
        {
            r"HKCU\Control Panel\Desktop": {"Wallpaper": ""},
            r"HKCU\Control Panel\Colors": {"Background": "12 34 56"},
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers": {
                "BackgroundType": 1
            },
        },
        tmp_path,
    )

    (item,), _ = wallpaper.scan_wallpaper(env)

    assert item.record["type"] == "solid_colour"
    assert item.record["colour"] == "12 34 56"


def test_a_slideshow_is_named_rather_than_half_carried(tmp_path: Path):
    """The pictures are a folder, which travels with the files if it is in the
    profile; the slideshow is a setting pointing into it."""
    env = env_with(
        {
            r"HKCU\Control Panel\Desktop": {"Wallpaper": r"C:\cache\current.jpg"},
            r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers": {
                "BackgroundType": 2
            },
        },
        tmp_path,
    )

    (item,), _ = wallpaper.scan_wallpaper(env)

    assert item.record["type"] == "slideshow"
    assert item.kind is Kind.RECORD
    assert any("cycles" in note.message for note in item.notes)


def test_an_older_windows_without_a_background_type_is_read_the_old_way(tmp_path: Path):
    """BackgroundType arrived with the Spotlight desktop. Before it, a wallpaper
    path meant a picture and no path meant the colour behind it."""
    chosen = a_picture(tmp_path)
    with_picture = env_with(
        {r"HKCU\Control Panel\Desktop": {"Wallpaper": str(chosen)}}, tmp_path
    )
    without = env_with({r"HKCU\Control Panel\Desktop": {"Wallpaper": ""}}, tmp_path)

    (picture,), _ = wallpaper.scan_wallpaper(with_picture)
    (colour,), _ = wallpaper.scan_wallpaper(without)

    assert picture.record["type"] == "picture" and picture.kind is Kind.FILE
    assert colour.record["type"] == "solid_colour"


def test_something_that_is_not_a_picture_is_not_carried_as_one(tmp_path: Path):
    """The value can point at anything, including a 900 MB video someone set as
    a background with a third-party tool, or a file that is not an image."""
    document = tmp_path / "notes.txt"
    document.write_text("not a picture", encoding="utf-8")
    empty = tmp_path / "empty.jpg"
    empty.write_bytes(b"")

    assert wallpaper.usable_image(document) is False
    assert wallpaper.usable_image(empty) is False
    assert wallpaper.usable_image(tmp_path / "absent.jpg") is False
    # No suffix is allowed, because TranscodedWallpaper has none and is real.
    transcoded = tmp_path / "TranscodedWallpaper"
    transcoded.write_bytes(b"\xff\xd8\xff" + b"0" * 64)
    assert wallpaper.usable_image(transcoded) is True


def test_a_machine_with_no_desktop_registry_at_all_offers_nothing(tmp_path: Path):
    items, followups = wallpaper.scan_wallpaper(env_with({}, tmp_path))
    assert items == [] and followups == []


# --- putting it back --------------------------------------------------------
def test_spotlight_is_restored_as_spotlight(tmp_path: Path):
    """The whole point of recording the type: the new machine subscribes to the
    same daily picture rather than being handed yesterday's."""
    env = env_with({}, tmp_path)

    results = apply_mod.apply_wallpaper({"type": "spotlight"}, None, env)

    key = r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers"
    # A number, not the string "3": Windows reads a DWORD here and treats a
    # string as no answer at all.
    assert env.registry[key]["BackgroundType"] == 3
    assert results[0].outcome is Outcome.APPLIED
    assert "every day" in results[0].detail


def test_a_picture_is_pointed_at_and_its_style_kept(tmp_path: Path):
    image = a_picture(tmp_path)
    env = env_with({}, tmp_path)

    results = apply_mod.apply_wallpaper(
        {"type": "picture", "style": "10", "tile": "0", "file_name": "lake.jpg"}, image, env
    )

    desktop = env.registry[r"HKCU\Control Panel\Desktop"]
    assert desktop["WallpaperStyle"] == "10"
    assert desktop["Wallpaper"] == str(image)
    wallpapers = env.registry[
        r"HKCU\Software\Microsoft\Windows\CurrentVersion\Explorer\Wallpapers"
    ]
    assert wallpapers["BackgroundType"] == 0
    assert [r.outcome for r in results] == [Outcome.APPLIED]


def test_a_picture_that_is_not_in_the_bundle_leaves_the_background_alone(tmp_path: Path):
    """Pointing Windows at a file that is not there is a black desktop, which is
    worse than the one the new machine came with."""
    env = env_with({}, tmp_path)

    (result,) = apply_mod.apply_wallpaper(
        {"type": "picture", "file_name": "lake.jpg"}, tmp_path / "absent.jpg", env
    )

    assert result.outcome is Outcome.SKIPPED
    assert "left alone" in result.detail
    assert "Wallpaper" not in env.registry.get(r"HKCU\Control Panel\Desktop", {})


def test_a_colour_is_written_as_windows_stores_it(tmp_path: Path):
    env = env_with({}, tmp_path)

    (result,) = apply_mod.apply_wallpaper(
        {"type": "solid_colour", "colour": "12 34 56"}, None, env
    )

    assert env.registry[r"HKCU\Control Panel\Colors"]["Background"] == "12 34 56"
    assert result.outcome is Outcome.APPLIED


def test_a_colour_that_is_not_a_colour_is_refused(tmp_path: Path):
    """The record comes out of a bundle, which is a file from another machine."""
    env = env_with({}, tmp_path)

    (result,) = apply_mod.apply_wallpaper(
        {"type": "solid_colour", "colour": "; shutdown /r"}, None, env
    )

    assert result.outcome is Outcome.SKIPPED
    assert "Background" not in env.registry.get(r"HKCU\Control Panel\Colors", {})


def test_a_slideshow_says_what_to_do_rather_than_guessing(tmp_path: Path):
    env = env_with({}, tmp_path)

    (result,) = apply_mod.apply_wallpaper({"type": "slideshow"}, None, env)

    assert result.outcome is Outcome.SKIPPED
    assert "Personalisation" in result.detail


def test_a_record_from_an_unknown_future_version_is_not_guessed_at(tmp_path: Path):
    env = env_with({}, tmp_path)

    (result,) = apply_mod.apply_wallpaper({"type": "hologram"}, None, env)

    assert result.outcome is Outcome.SKIPPED
    assert env.registry == {}


# --- never reaching past the environment it was given -----------------------
def test_a_fixture_is_never_treated_as_the_machine_it_runs_on(tmp_path: Path):
    """The bug this exists to prevent: on a Windows build machine the registry
    writes went to the fixture, as intended, and the SystemParametersInfoW call
    beside them went to the actual desktop -- so a test changed the wallpaper of
    the machine running it, then failed because the fixture was missing the
    value the real machine had just been given.

    Two conditions, not one: tests that exercise Windows-only paths set a
    fixture's is_windows True on purpose, and one of those reaching a real
    Windows call would be the same accident in a different hat."""
    from winmigrate.platform_win import Environment as Env

    fixture = env_with({}, tmp_path)
    assert apply_mod.touches_machine(fixture) is False

    fixture.is_windows = True
    assert apply_mod.touches_machine(fixture) is False

    real = Env(profile_root=tmp_path, registry=None, is_windows=True)
    assert apply_mod.touches_machine(real) is True


def test_a_picture_given_a_fixture_is_recorded_rather_than_hung_on_the_wall(
    tmp_path: Path,
):
    """Observable from either platform, which is the point: this is the
    assertion that fails on Windows if the gate goes back to asking sys."""
    image = a_picture(tmp_path)
    env = env_with({}, tmp_path)
    env.is_windows = True  # as a test exercising a Windows path would

    (result,) = apply_mod.apply_wallpaper(
        {"type": "picture", "style": "10", "file_name": "lake.jpg"}, image, env
    )

    assert result.detail == "recorded"
    assert env.registry[r"HKCU\Control Panel\Desktop"]["Wallpaper"] == str(image)
