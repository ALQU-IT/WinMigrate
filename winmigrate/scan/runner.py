"""Scan orchestration: turn a machine into a :class:`ScanResult`.

The scan stage never writes to the profile it is inspecting and never opens a
file's contents. Its output is the single input to preview, capture and package,
which keeps "what the preview promised" and "what capture does" the same list.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from pathlib import Path

from ..config import PROFILE_DIRS_NOT_USER_DATA, ScanConfig
from ..manifest import detect_source_machine
from ..models import (
    Action,
    Category,
    Followup,
    Item,
    Kind,
    Note,
    RestoreSpec,
    RestoreStrategy,
    ScanResult,
    Severity,
    SkipReason,
    utcnow,
)
from ..platform_win import Environment
from ..util import paths as pathutil
from . import syncroots as syncroots_mod
from .userfiles import TreeMeasurement, measure_tree

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str], None]


def run_scan(
    config: ScanConfig | None = None,
    env: Environment | None = None,
    progress: ProgressCallback | None = None,
) -> ScanResult:
    """Inventory the current user's profile. Read-only."""
    config = config or ScanConfig()
    env = env or _environment_for(config)
    started = time.monotonic()

    source = detect_source_machine(profile_path=str(env.profile_root))
    result = ScanResult(source=source, started_utc=utcnow(), files_only=config.files_only)

    if not env.profile_root.is_dir():
        result.add_note(
            Severity.ERROR,
            f"user profile directory not found: {env.profile_root}",
            "Nothing can be scanned. Check --profile-root.",
        )
        result.finished_utc = utcnow()
        return result

    _emit(progress, "Detecting cloud-sync roots")
    result.sync_roots = syncroots_mod.detect(env) if config.skip_synced else []
    syncroots_mod.measure(result.sync_roots, config.measure_skipped)
    for root in result.sync_roots:
        log.info("sync root: %s -> %s", root.provider, root.root)

    long_paths = _scan_known_folders(config, env, result, progress)
    if config.include_other_profile_dirs:
        long_paths += _scan_other_profile_dirs(config, env, result, progress)
    _record_sync_roots(result)
    _add_sync_followups(result)
    _note_long_paths(result, long_paths)

    result.duration_seconds = time.monotonic() - started
    result.finished_utc = utcnow()
    return result


def _environment_for(config: ScanConfig) -> Environment:
    if config.profile_root is not None:
        return Environment.fixture(config.profile_root)
    return Environment.live()


def _emit(progress: ProgressCallback | None, message: str) -> None:
    if progress is not None:
        progress(message)
    log.debug("%s", message)


# --- known folders ---------------------------------------------------------
def _scan_known_folders(
    config: ScanConfig, env: Environment, result: ScanResult, progress: ProgressCallback | None
) -> int:
    long_paths = 0
    for folder_id in config.known_folders:
        title = env.known_folder_title(folder_id)
        path = env.known_folder(folder_id)
        _emit(progress, f"Scanning {title}")
        measurement = measure_tree(path, config, env, result.sync_roots)
        item = _item_from_measurement(
            item_id=f"files:{folder_id}",
            title=title,
            path=path,
            measurement=measurement,
            archive_path=f"data/user_files/{folder_id}",
            restore_target=_restore_target(folder_id, path, env),
        )
        _note_redirection(item, path, env)
        long_paths += measurement.long_path_count
        result.items.append(item)
    return long_paths


def _restore_target(folder_id: str, path: Path, env: Environment) -> str:
    """Where this folder goes on the target machine.

    Known folders are restored by *name*, not by absolute path: the target
    machine's own known-folder location (which may itself be redirected) is
    resolved at restore time.
    """
    name = path.name or folder_id
    return f"%USERPROFILE%\\{name}"


def _note_redirection(item: Item, path: Path, env: Environment) -> None:
    """Flag a known folder that has been redirected out of the profile.

    OneDrive's Known Folder Move does exactly this, so a redirected Documents
    folder is the normal case rather than an oddity -- but the user should see
    it in the preview, because it explains why the folder is not being copied.
    """
    if pathutil.is_within(path, env.profile_root):
        return
    item.notes.append(
        Note(
            Severity.INFO,
            f"{item.title} is redirected to {path}",
            "Restore will place it in the target machine's own location for this folder.",
        )
    )


def _scan_other_profile_dirs(
    config: ScanConfig, env: Environment, result: ScanResult, progress: ProgressCallback | None
) -> int:
    """Capture user-created top-level folders in the profile root.

    People keep real work in ``C:\\Users\\me\\Projects``; a migration that only
    took the known folders would silently leave it behind.
    """
    known_paths = {
        pathutil.normalize_key(env.known_folder(folder_id)) for folder_id in config.known_folders
    }
    sync_paths = {pathutil.normalize_key(root.root) for root in result.sync_roots}
    try:
        entries = sorted(os.scandir(pathutil.extended(env.profile_root)), key=lambda e: e.name)
    except OSError as exc:
        result.add_note(Severity.WARNING, f"could not list {env.profile_root}", str(exc))
        return 0

    long_paths = 0

    for entry in entries:
        try:
            if not entry.is_dir(follow_symlinks=False):
                continue
        except OSError:
            continue
        name = entry.name
        # As in the walk: reconstruct from the profile root rather than trusting
        # entry.path, which carries the extended-length prefix on Windows.
        entry_path = env.profile_root / name
        key = pathutil.normalize_key(entry_path)
        if name.lower() in PROFILE_DIRS_NOT_USER_DATA or name.startswith("."):
            continue
        if key in known_paths or key in sync_paths:
            continue
        if config.is_excluded(pathutil.relative_posix(entry_path, env.profile_root), name):
            continue
        _emit(progress, f"Scanning {name}")
        measurement = measure_tree(entry_path, config, env, result.sync_roots)
        long_paths += measurement.long_path_count
        result.items.append(
            _item_from_measurement(
                item_id=f"files:other:{name.lower()}",
                title=name,
                path=entry_path,
                measurement=measurement,
                archive_path=f"data/user_files/_other/{name}",
                restore_target=f"%USERPROFILE%\\{name}",
            )
        )
    return long_paths


def _item_from_measurement(
    *,
    item_id: str,
    title: str,
    path: Path,
    measurement: TreeMeasurement,
    archive_path: str,
    restore_target: str,
) -> Item:
    item = Item(
        id=item_id,
        category=Category.USER_FILES,
        kind=Kind.TREE,
        title=title,
        source_path=str(path),
        archive_path=archive_path,
        size_bytes=measurement.size_bytes,
        file_count=measurement.file_count,
        skipped=list(measurement.skipped),
        notes=list(measurement.notes),
        restore=RestoreSpec(target=restore_target, strategy=RestoreStrategy.MERGE),
    )
    if not measurement.exists:
        item.action = Action.SKIP
        item.skip_reason = SkipReason.NOT_PRESENT
        item.restore = None
    elif measurement.file_count == 0:
        item.action = Action.SKIP
        synced = [group for group in measurement.skipped if group.reason is SkipReason.SYNCED]
        item.skip_reason = SkipReason.SYNCED if synced else SkipReason.EMPTY
    return item


# --- sync roots ------------------------------------------------------------
def _record_sync_roots(result: ScanResult) -> None:
    """Record each sync root as a reported (not captured) item.

    They belong in the manifest so the restore report can say *why* a folder is
    missing from the bundle and what the user should do about it.
    """
    for index, root in enumerate(result.sync_roots):
        result.items.append(
            Item(
                id=f"sync:{root.provider}:{index}",
                category=Category.USER_FILES,
                kind=Kind.REPORT,
                title=f"{root.label or root.provider} ({root.provider})",
                source_path=root.root,
                action=Action.SKIP,
                skip_reason=SkipReason.SYNCED,
                size_bytes=root.bytes_skipped,
                file_count=root.files_skipped,
                record={
                    "provider": root.provider,
                    "root": root.root,
                    "account_hint": root.account_hint,
                },
                restore=RestoreSpec(
                    target=root.root,
                    strategy=RestoreStrategy.GUIDED,
                    notes=["Content returns by signing in to the provider on the new machine."],
                ),
                notes=[
                    Note(
                        Severity.INFO,
                        f"Not captured: this folder is synced by {root.provider}.",
                        f"Account: {root.account_hint}" if root.account_hint else None,
                    )
                ],
            )
        )


def _add_sync_followups(result: ScanResult) -> None:
    for root in result.sync_roots:
        provider = root.provider
        result.followups.append(
            Followup(
                id=f"signin:{provider}:{pathutil.normalize_key(root.root)}",
                title=f"Sign in to {provider} on the new machine",
                why=(
                    f"{root.label or root.root} was not captured because it is already "
                    f"synced. Signing in brings it back without doubling the bundle."
                ),
                steps=[
                    f"Install and open the {provider} client on the new machine.",
                    f"Sign in{f' as {root.account_hint}' if root.account_hint else ''}.",
                    "Wait for the initial sync to finish before decommissioning the old machine.",
                    "Confirm the folder contents match before deleting anything.",
                ],
                category=Category.USER_FILES,
            )
        )


def _note_long_paths(result: ScanResult, long_path_count: int) -> None:
    if long_path_count:
        result.add_note(
            Severity.WARNING,
            f"{long_path_count} path(s) are at or beyond the classic 260-character limit",
            "Capture uses extended-length paths; third-party tools may not.",
        )
