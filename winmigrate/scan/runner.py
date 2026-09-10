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
    SyncRoot,
    utcnow,
)
from ..platform_win import KNOWN_FOLDERS, Environment
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
    if config.active_presets:
        result.add_note(
            Severity.INFO,
            "Exclusion preset(s) active: " + ", ".join(config.active_presets),
            "the large items these cover are left out of the plan on purpose",
        )
    result.sync_roots = syncroots_mod.detect(env) if config.skip_synced else []
    syncroots_mod.measure(result.sync_roots, config.measure_skipped)
    for root in result.sync_roots:
        log.info("sync root: %s -> %s", root.provider, root.root)

    long_paths = _scan_known_folders(config, env, result, progress)
    if config.include_other_profile_dirs:
        long_paths += _scan_other_profile_dirs(config, env, result, progress)
    _record_sync_roots(result)
    _add_sync_followups(result)
    if config.include_software:
        # Office is detected first: its Click-to-Run build version decides which
        # inventory entries the Office item already covers, and that has to be
        # known before the software record and its follow-up are built from it.
        installation = _detect_office(env, progress)
        _scan_software(env, result, progress, installation)
        _record_office(result, installation)
    _scan_dev_config(env, result, config, progress)
    _scan_system_settings(env, result, progress)
    _scan_wifi(env, result, config, progress)
    _scan_browsers(env, result, config, progress)
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
            # The archive path carries the folder's real name, not the internal
            # id, so a bundle restores to correctly-named folders on its own.
            # Ids stay stable and lowercase; the two are deliberately separate.
            archive_path=f"data/user_files/{path.name or folder_id}",
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
    # Every known folder, not just the configured subset: a folder deselected
    # from ``known_folders`` must stay out, rather than reappearing here as an
    # "other" directory and defeating the setting.
    known_paths = {
        pathutil.normalize_key(env.known_folder(folder_id)) for folder_id in KNOWN_FOLDERS
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
def _is_dormant(root: SyncRoot) -> bool:
    """True when a detected sync folder holds nothing and names no account.

    A leftover ``C:\\Users\\me\\OneDrive`` from a client that was never used
    looks identical to an active one until you look inside it.
    """
    return root.bytes_skipped == 0 and root.files_skipped == 0 and not root.account_hint


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
                record_public=True,
                restore=RestoreSpec(
                    target=root.root,
                    strategy=RestoreStrategy.GUIDED,
                    notes=["Content returns by signing in to the provider on the new machine."],
                ),
                notes=[
                    Note(
                        Severity.INFO,
                        (
                            f"Empty, and no {root.provider} account was found: "
                            "nothing to migrate and nothing to sign in to."
                            if _is_dormant(root)
                            else f"Not captured: this folder is synced by {root.provider}."
                        ),
                        f"Account: {root.account_hint}" if root.account_hint else None,
                    )
                ],
            )
        )


def _add_sync_followups(result: ScanResult) -> None:
    for root in result.sync_roots:
        if _is_dormant(root):
            # An empty folder we cannot tie to an account has nothing to bring
            # back, so telling the user to sign in would be noise in the one
            # list that must stay worth reading.
            continue
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


# --- software and Office ---------------------------------------------------
def _scan_software(
    env: Environment,
    result: ScanResult,
    progress: ProgressCallback | None,
    installation=None,
) -> None:
    """Inventory installed applications as a manifest record, not as files.

    Nothing is copied: what migrates is the *list*, so the target machine can
    reinstall from its own sources rather than carrying binaries that would be
    the wrong build, unlicensed, or simply out of date by the time they land.
    """
    from . import software as software_mod  # noqa: PLC0415 -- optional stage

    _emit(progress, "Inventorying installed software")
    inventory = software_mod.scan_software(env)
    if installation is not None and installation.present:
        software_mod.flag_office_entries(inventory, installation.version)
    if not inventory.entries and not inventory.winget_export:
        for note in inventory.notes:
            result.add_note(Severity.WARNING, f"software inventory incomplete: {note}")
        return None

    result.items.append(
        Item(
            id="software:inventory",
            category=Category.SOFTWARE,
            kind=Kind.RECORD,
            title=f"Installed software ({len(inventory.entries)})",
            action=Action.CAPTURE,
            record=inventory.to_json(),
            restore=RestoreSpec(
                target="winget import",
                strategy=RestoreStrategy.GUIDED,
                notes=[
                    "Restore writes a winget import file; 'winmigrate reinstall' replays it.",
                ],
            ),
            notes=[
                Note(
                    Severity.INFO,
                    f"{len(inventory.reinstallable)} of {len(inventory.entries)} "
                    f"can be reinstalled by winget; {len(inventory.components)} are "
                    f"runtimes or drivers that come with whatever needs them",
                )
            ],
        )
    )
    for note in inventory.notes:
        result.add_note(Severity.WARNING, f"software inventory: {note}")

    launchers = inventory.launchers()
    if inventory.manual:
        components = len(inventory.components)
        office_covered = sum(1 for entry in inventory.entries if entry.covered_by_office)
        launcher_managed = len(inventory.launcher_managed)
        result.followups.append(
            Followup(
                id="software:manual",
                title=f"Reinstall {len(inventory.manual)} application(s) by hand",
                why=(
                    f"winget can reinstall {len(inventory.reinstallable)} of the "
                    f"{len(inventory.entries)} applications found. These are the rest"
                    + (
                        f", excluding {components} runtimes and drivers that arrive "
                        "with whatever needs them"
                        if components
                        else ""
                    )
                    + (
                        f" and {office_covered} Office entries covered by the Office "
                        "step below"
                        if office_covered
                        else ""
                    )
                    + (
                        f" and {launcher_managed} title(s) that come back through a "
                        "game launcher"
                        if launcher_managed
                        else ""
                    )
                    + ". The full list is written next to the restored files."
                ),
                steps=[
                    "Open WinMigrate-Reinstall\\reinstall-by-hand.md in the restore folder.",
                    "Work down the list, installing what you still want.",
                    "Being listed means it was on the old machine, not that you need it.",
                ],
                category=Category.SOFTWARE,
            )
        )

    for launcher, count in launchers.items():
        result.followups.append(
            Followup(
                id=f"software:launcher:{launcher.lower().replace(' ', '_').replace('.', '')}",
                title=f"Sign in to {launcher} to get {count} game(s) back",
                why=(
                    f"{count} title(s) were installed through {launcher}. They are not "
                    "carried in the bundle -- they re-download from your library once "
                    "you sign in, which is faster than copying them and keeps them "
                    "up to date."
                ),
                steps=[
                    f"Install {launcher} on the new machine (it is in the reinstall list).",
                    "Sign in to your account.",
                    "Re-download the titles you still play from your library.",
                ],
                category=Category.SOFTWARE,
            )
        )


def _measure_capture_items(items: list[Item], config: ScanConfig, env: Environment) -> None:
    """Fill in size and skip accounting for capture items so the preview is honest.

    The browser and dev-config scanners produce items without walking them;
    without this the preview would show every one as 0 bytes, which reads as a
    failed capture rather than an unmeasured one. Same walk capture will use, so
    the numbers match. Skipped items (files-only mode) and non-file kinds are
    left alone.
    """
    from .userfiles import measure_tree  # noqa: PLC0415

    for item in items:
        if item.action is not Action.CAPTURE or not item.source_path:
            continue
        source = Path(item.source_path)
        if item.kind is Kind.TREE:
            measurement = measure_tree(source, config, env, relative_base=env.profile_root)
            item.size_bytes = measurement.size_bytes
            item.file_count = measurement.file_count
            item.skipped = list(measurement.skipped)
        elif item.kind is Kind.FILE:
            try:
                item.size_bytes = os.stat(pathutil.extended(source)).st_size
                item.file_count = 1
            except OSError:
                item.action = Action.SKIP
                item.skip_reason = SkipReason.UNREADABLE


def _scan_browsers(
    env: Environment, result: ScanResult, config: ScanConfig, progress: ProgressCallback | None
) -> None:
    """Capture browser profiles and report how their passwords come across."""
    from . import browsers as browsers_mod  # noqa: PLC0415 -- optional stage

    _emit(progress, "Checking browsers")
    items, followups, notes = browsers_mod.scan_browsers(env, files_only=config.files_only)
    _measure_capture_items(items, config, env)
    result.items.extend(items)
    result.followups.extend(followups)
    result.notes.extend(notes)


def _scan_dev_config(
    env: Environment, result: ScanResult, config: ScanConfig, progress: ProgressCallback | None
) -> None:
    """Capture developer and credential config -- the encrypted-only material."""
    from . import devconfig as devconfig_mod  # noqa: PLC0415 -- optional stage

    _emit(progress, "Checking developer configuration")
    items, notes = devconfig_mod.scan_dev_config(env, files_only=config.files_only)
    _measure_capture_items(items, config, env)
    result.items.extend(items)
    result.notes.extend(notes)

    captured_secret = [
        item for item in items if item.is_secret and item.action is Action.CAPTURE
    ]
    if captured_secret:
        result.followups.append(
            Followup(
                id="dev:secrets",
                title="Check the developer credentials that were migrated",
                why=(
                    f"{len(captured_secret)} credential location(s) (SSH keys, cloud "
                    "credentials and the like) were captured into the encrypted bundle "
                    "only. They restore to their original paths; some still need a step "
                    "from you."
                ),
                steps=[
                    "SSH keys restore to ~/.ssh -- check their permissions are still "
                    "restrictive on the new machine.",
                    "Cloud CLIs (aws, az, gcloud) may prompt to re-authenticate even "
                    "with the config in place.",
                    "Rotate anything you would rather not have travelled, now that it "
                    "is on a second machine.",
                ],
                category=Category.DEV_CONFIG,
            )
        )


def _scan_system_settings(
    env: Environment, result: ScanResult, progress: ProgressCallback | None
) -> None:
    from . import syssettings as syssettings_mod  # noqa: PLC0415 -- optional stage

    _emit(progress, "Reading system settings")
    items, followups = syssettings_mod.scan_system_settings(env)
    result.items.extend(items)
    result.followups.extend(followups)


def _scan_wifi(
    env: Environment, result: ScanResult, config: ScanConfig, progress: ProgressCallback | None
) -> None:
    from . import wifi as wifi_mod  # noqa: PLC0415 -- optional stage

    if not config.include_wifi:
        return
    _emit(progress, "Reading Wi-Fi profiles")
    items, followups = wifi_mod.scan_wifi(env, config.include_wifi)
    result.items.extend(items)
    result.followups.extend(followups)


def _detect_office(env: Environment, progress: ProgressCallback | None):
    """Detect Office so it can be reinstalled -- never so its key can be taken."""
    from . import office as office_mod  # noqa: PLC0415 -- optional stage

    _emit(progress, "Detecting Microsoft Office")
    return office_mod.detect(env)


def _record_office(result: ScanResult, installation) -> None:
    """Record the Office installation and how the user reactivates it."""
    from . import office as office_mod  # noqa: PLC0415

    if not installation.present:
        return

    result.items.append(
        Item(
            id="office:installation",
            category=Category.OFFICE,
            kind=Kind.RECORD,
            title=", ".join(installation.titles) or "Microsoft Office",
            action=Action.CAPTURE,
            record=installation.to_json(),
            restore=RestoreSpec(
                target="Office Deployment Tool",
                strategy=RestoreStrategy.GUIDED,
                notes=["Restore writes a matching configuration.xml; you run setup.exe."],
            ),
            notes=[
                Note(
                    Severity.INFO,
                    f"{installation.platform or 'unknown bitness'}, "
                    f"{installation.client_culture or 'unknown language'}, "
                    f"channel {installation.channel or 'unknown'}",
                )
            ],
        )
    )

    steps = list(office_mod.REACTIVATION_STEPS.get(
        installation.activation_type, office_mod.REACTIVATION_STEPS["unknown"]
    ))
    hints = installation.key_hints()
    if hints and installation.activation_type == "retail":
        # Office and Visio install together and carry different keys; one hint
        # would send the user looking for the wrong one.
        listed = "; ".join(f"{product} ends in {hint}" for product, hint in hints)
        steps.append(f"Installed key(s): {listed} -- use these to recognise the right ones.")
    unactivated = [lic.product for lic in installation.licences if not lic.is_activated]
    if unactivated:
        steps.append(
            "Note: "
            + ", ".join(unactivated)
            + " is not currently activated on this machine either "
            "(ospp reports a notification state), so this was already outstanding."
        )
    result.followups.append(
        Followup(
            id="office:reactivate",
            title="Reinstall and reactivate Office",
            why=(
                f"Office ({installation.activation_type} licence) is not carried in the "
                "bundle. WinMigrate records what to install; activation needs you. "
                "No product key is copied or recoverable from this bundle."
            ),
            steps=steps,
            category=Category.OFFICE,
        )
    )
