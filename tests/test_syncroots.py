from pathlib import Path

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
    )
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
