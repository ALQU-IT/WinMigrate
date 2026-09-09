from pathlib import Path

from conftest import snapshot

from winmigrate.config import ScanConfig
from winmigrate.models import Action, Category, Kind, SkipReason
from winmigrate.platform_win import USER_SHELL_FOLDERS_KEY, Environment
from winmigrate.scan import run_scan


def scan(profile: Path, env: Environment, **overrides) -> object:
    config = ScanConfig(profile_root=profile, **overrides)
    return run_scan(config, env)


def item(result, item_id):
    return next(entry for entry in result.items if entry.id == item_id)


def test_documents_capture_excludes_junk_and_regenerable_output(profile: Path, env: Environment):
    result = scan(profile, env)
    documents = item(result, "files:documents")
    assert documents.action is Action.CAPTURE
    # report.docx + notes.txt + proj/src/main.py; ~$report.docx and Thumbs.db excluded
    assert documents.size_bytes == 5500
    assert documents.file_count == 3
    reasons = {group.reason for group in documents.skipped}
    assert reasons == {SkipReason.REGENERABLE, SkipReason.EXCLUDED}
    regenerable = next(g for g in documents.skipped if g.reason is SkipReason.REGENERABLE)
    assert regenerable.bytes == 90_000


def test_include_regenerable_keeps_node_modules(profile: Path, env: Environment):
    result = scan(profile, env, include_regenerable=True)
    documents = item(result, "files:documents")
    assert documents.size_bytes == 95_500
    assert documents.file_count == 4


def test_user_created_profile_folders_are_captured(profile: Path, env: Environment):
    result = scan(profile, env)
    projects = item(result, "files:other:projects")
    assert projects.action is Action.CAPTURE
    assert projects.size_bytes == 400
    assert projects.restore.target == "%USERPROFILE%\\Projects"


def test_appdata_is_not_treated_as_a_plain_profile_folder(profile: Path, env: Environment):
    result = scan(profile, env)
    assert not any(entry.id == "files:other:appdata" for entry in result.items)


def test_absent_and_empty_known_folders_are_marked_not_captured(profile: Path, env: Environment):
    result = scan(profile, env)
    assert item(result, "files:music").skip_reason is SkipReason.NOT_PRESENT
    assert item(result, "files:pictures").skip_reason is SkipReason.EMPTY


def test_synced_folder_is_reported_once_and_not_double_counted(profile: Path, env: Environment):
    result = scan(profile, env)
    onedrive = item(result, "sync:onedrive:0")
    assert onedrive.kind is Kind.REPORT
    assert onedrive.action is Action.SKIP
    assert onedrive.size_bytes == 10_000  # both files under OneDrive/, counted once
    totals = result.totals()
    # 90,000 regenerable + 10 junk + 7 junk + 10,000 synced
    assert totals.skipped_bytes == 100_017
    assert totals.capture_bytes == 5500 + 50 + 120_000 + 400


def test_known_folder_redirected_into_onedrive_is_skipped_as_synced(profile: Path, registry: dict):
    """OneDrive Known Folder Move: Documents lives inside the sync root."""
    registry[f"HKCU\\{USER_SHELL_FOLDERS_KEY}"]["Personal"] = "%USERPROFILE%\\OneDrive\\Documents"
    env = Environment.fixture(profile, registry)
    result = run_scan(ScanConfig(profile_root=profile), env)
    documents = item(result, "files:documents")
    assert documents.action is Action.SKIP
    assert documents.skip_reason is SkipReason.SYNCED
    assert documents.size_bytes == 0
    # the bytes are attributed to the sync root, and only there
    assert item(result, "sync:onedrive:0").size_bytes == 10_000


def test_no_skip_synced_captures_the_sync_root_contents(profile: Path, registry: dict):
    registry[f"HKCU\\{USER_SHELL_FOLDERS_KEY}"]["Personal"] = "%USERPROFILE%\\OneDrive\\Documents"
    env = Environment.fixture(profile, registry)
    result = run_scan(ScanConfig(profile_root=profile, skip_synced=False), env)
    documents = item(result, "files:documents")
    assert documents.action is Action.CAPTURE
    assert documents.size_bytes == 7000
    assert result.sync_roots == []


def test_every_sync_root_produces_a_sign_in_followup(profile: Path, env: Environment):
    result = scan(profile, env)
    followups = [f for f in result.followups if f.id.startswith("signin:onedrive")]
    assert len(followups) == 1
    assert "alice@example.com" in followups[0].steps[1]


def test_item_ids_are_unique(profile: Path, env: Environment):
    result = scan(profile, env)
    ids = [entry.id for entry in result.items]
    assert len(ids) == len(set(ids))


def test_every_captured_item_has_a_restore_target(profile: Path, env: Environment):
    result = scan(profile, env)
    for entry in result.items:
        if entry.action is Action.CAPTURE:
            assert entry.restore is not None and entry.restore.target
            assert entry.archive_path


def test_scan_is_read_only(profile: Path, env: Environment):
    before = snapshot(profile)
    scan(profile, env)
    assert snapshot(profile) == before


def test_fast_scan_skips_measuring_what_it_will_not_capture(profile: Path, env: Environment):
    """--fast drops the extra walks into skipped subtrees, not the capture plan.

    Files the walk already stat'd (junk it rejected in passing) still carry
    their size; only the subtrees it never descends into -- node_modules, the
    sync root -- go uncounted.
    """
    result = scan(profile, env, measure_skipped=False)
    assert result.totals().capture_bytes == 5500 + 50 + 120_000 + 400
    documents = item(result, "files:documents")
    regenerable = next(g for g in documents.skipped if g.reason is SkipReason.REGENERABLE)
    assert regenerable.bytes == 0
    assert item(result, "sync:onedrive:0").size_bytes == 0
    # the two junk files were stat'd during the walk, so they cost nothing to count
    assert result.totals().skipped_bytes == 17


def test_missing_profile_root_is_an_error_note_not_a_crash(tmp_path: Path):
    absent = tmp_path / "nobody"
    result = run_scan(ScanConfig(profile_root=absent), Environment.fixture(absent, {}))
    assert result.items == []
    assert any("not found" in note.message for note in result.notes)


def test_all_items_are_user_files_in_phase_one(profile: Path, env: Environment):
    result = scan(profile, env)
    assert {entry.category for entry in result.items} == {Category.USER_FILES}
