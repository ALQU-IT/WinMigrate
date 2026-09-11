from pathlib import Path

import time
from winmigrate.models import SyncRoot
from winmigrate.platform_win import Environment
from winmigrate.scan import syncroots


def test_onedrive_is_detected_from_the_registry(env: Environment, profile: Path):
    roots = syncroots.detect(env)
    onedrive = [root for root in roots if root.provider == "onedrive"]
    assert len(onedrive) == 1, "the registry entry and the folder heuristic must not double up"
    assert Path(onedrive[0].root) == profile / "OneDrive"
    assert onedrive[0].account_hint == "personal: alice@example.com"


def test_onedrive_falls_back_to_the_folder_when_the_registry_is_silent(profile: Path):
    env = Environment.fixture(profile, {})
    roots = syncroots.detect(env)
    assert [root.provider for root in roots] == ["onedrive"]
    assert roots[0].account_hint is None


def test_nextcloud_config_yields_local_folders():
    text = (
        "[Accounts]\n"
        "0\\Folders\\1\\localPath=C:/Users/alice/Nextcloud/\n"
        "0\\url=https://cloud.example.com\n"
        "0\\dav_user=alice\n"
        "version=2\n"
    )
    roots = syncroots.parse_nextcloud_config(text)
    assert len(roots) == 1
    assert roots[0].provider == "nextcloud"
    assert roots[0].root.endswith("Nextcloud")
    assert roots[0].account_hint == "alice @ https://cloud.example.com"


def test_nextcloud_config_that_is_not_ini_is_ignored_rather_than_crashing():
    assert syncroots.parse_nextcloud_config("<<<not ini>>>") == []


def test_nextcloud_is_detected_from_appdata(profile: Path):
    config = profile / "AppData" / "Roaming" / "Nextcloud" / "nextcloud.cfg"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(
        "[Accounts]\n0\\Folders\\1\\localPath=%s\n" % (profile / "Nextcloud").as_posix()
, encoding="utf-8")
    (profile / "Nextcloud").mkdir()
    env = Environment.fixture(profile, {})
    providers = {root.provider for root in syncroots.detect(env)}
    assert providers == {"onedrive", "nextcloud"}


def test_find_root_for_prefers_the_deepest_match():
    roots = [
        SyncRoot(provider="a", root="/sync"),
        SyncRoot(provider="b", root="/sync/inner"),
    ]
    assert syncroots.find_root_for("/sync/inner/file", roots).provider == "b"
    assert syncroots.find_root_for("/sync/other/file", roots).provider == "a"
    assert syncroots.find_root_for("/elsewhere", roots) is None


def test_measure_records_the_volume_of_each_root(profile: Path):
    roots = [SyncRoot(provider="onedrive", root=str(profile / "OneDrive"))]
    syncroots.measure(roots)
    assert roots[0].bytes_skipped == 10_000
    assert roots[0].files_skipped == 2


def test_measure_can_be_switched_off_for_a_fast_scan(profile: Path):
    roots = [SyncRoot(provider="onedrive", root=str(profile / "OneDrive"))]
    syncroots.measure(roots, measure_contents=False)
    assert roots[0].bytes_skipped == 0


def test_onedrive_env_vars_are_found_despite_windows_upper_casing(tmp_path):
    """dict(os.environ) upper-cases keys on Windows; the lookup must still match."""
    root = tmp_path / "profile"
    (root / "Work").mkdir(parents=True)
    env = Environment.fixture(root, {}, environ={"ONEDRIVECOMMERCIAL": str(root / "Work")})
    roots = syncroots.detect(env)
    assert str(root / "Work") in {entry.root for entry in roots}


# --- counting what is not being copied -------------------------------------
def slow_scandir(delay: float):
    """os.scandir whose entries stat slowly, standing in for cloud placeholders.

    A OneDrive file that is not downloaded is a placeholder, and stat-ing one
    goes through the Cloud Files driver rather than the disk. On a corporate
    OneDrive that turns counting into minutes.
    """
    import os as _os

    real = _os.scandir

    class SlowEntry:
        def __init__(self, entry):
            self._entry = entry
            self.name = entry.name
            self.path = entry.path

        def is_dir(self, **kwargs):
            return self._entry.is_dir(**kwargs)

        def stat(self, **kwargs):
            time.sleep(delay)
            return self._entry.stat(**kwargs)

    return lambda path: [SlowEntry(entry) for entry in real(path)]


def build_tree(root: Path, folders: int = 20, each: int = 200) -> None:
    for index in range(folders):
        folder = root / f"folder{index}"
        folder.mkdir(parents=True)
        for number in range(each):
            (folder / f"f{number}.bin").write_bytes(b"x" * 10)


def test_counting_a_sync_root_gives_up_rather_than_taking_minutes(tmp_path: Path, monkeypatch):
    """This figure exists only to tell the user roughly how much is not being
    backed up. It is worth a few seconds and it is not worth ten minutes -- and
    ten minutes is what it cost on a real corporate OneDrive, in silence, before
    the program had printed a single character.
    """
    root = tmp_path / "OneDrive - Contoso"
    build_tree(root)
    monkeypatch.setattr(syncroots.os, "scandir", slow_scandir(0.002))

    entry = SyncRoot(provider="onedrive", root=str(root), label="OneDrive - Contoso")
    started = time.monotonic()
    syncroots.measure([entry], True, budget_seconds=0.5)
    elapsed = time.monotonic() - started

    assert elapsed < 2.0, f"took {elapsed:.1f}s despite a 0.5s budget"
    assert entry.measured_fully is False
    assert entry.files_skipped > 0  # a floor, not nothing


def test_a_root_that_fits_in_the_budget_is_counted_exactly(tmp_path: Path):
    root = tmp_path / "OneDrive"
    build_tree(root, folders=3, each=10)
    entry = SyncRoot(provider="onedrive", root=str(root), label="OneDrive")
    syncroots.measure([entry], True, budget_seconds=30)

    assert entry.measured_fully is True
    assert entry.files_skipped == 30
    assert entry.bytes_skipped == 300


def test_the_clock_is_checked_far_more_often_than_progress_is_reported(tmp_path: Path):
    """The budget exists for the case where each stat is slow. Checking the time
    every couple of thousand files would mean a minute and a half before the
    first look at a clock set for eight seconds."""
    assert syncroots._CHECK_CLOCK_EVERY < syncroots._REPORT_EVERY / 10


def test_counting_says_what_it_is_doing(tmp_path: Path):
    """Ten minutes of silence is indistinguishable from a program that has hung,
    which is exactly how it was reported."""
    root = tmp_path / "OneDrive - Contoso"
    build_tree(root, folders=15, each=200)
    said: list[str] = []
    entry = SyncRoot(provider="onedrive", root=str(root), label="OneDrive - Contoso")
    syncroots.measure([entry], True, progress=said.append, budget_seconds=30)

    assert said, "counting reported nothing at all"
    assert "OneDrive - Contoso" in said[0]
    assert any("files" in message for message in said[1:]), said


def test_an_incomplete_count_is_shown_as_a_floor_not_a_total(tmp_path: Path):
    """Reporting a number and claiming one are different things."""
    from rich.console import Console

    from winmigrate import report as report_mod
    from winmigrate.manifest import detect_source_machine
    from winmigrate.models import ScanResult

    result = ScanResult(source=detect_source_machine(str(tmp_path)))
    result.sync_roots = [
        SyncRoot(provider="onedrive", root=str(tmp_path / "a"), label="Partial",
                 bytes_skipped=1024, files_skipped=5, measured_fully=False),
        SyncRoot(provider="onedrive", root=str(tmp_path / "b"), label="Whole",
                 bytes_skipped=2048, files_skipped=9, measured_fully=True),
    ]
    console = Console(record=True, width=200)
    report_mod.render_preview(result, console)
    text = " ".join(console.export_text().split())
    assert "≥" in text
    assert text.count("≥") == 2  # the floor's size and its file count, not the other root's
