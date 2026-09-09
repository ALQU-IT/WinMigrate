"""Measure the user's file trees without reading a single byte of content.

The walk answers four questions per directory entry:

1. Is it inside a cloud-sync root?     -> skip, credited to that provider
2. Does it match an exclusion rule?    -> skip, credited to junk or regenerable
3. Is it a cloud placeholder?          -> skip; its bytes are not on this disk
4. Is it a reparse point?              -> skip by default; junctions cause loops

Everything else is counted for capture. Nothing is opened, so a scan is safe to
run while the machine is in use and cannot itself trigger a sync download.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ScanConfig
from ..models import Note, Severity, SkippedGroup, SkipReason, SyncRoot
from ..platform_win import CLOUD_PLACEHOLDER_MASK, FILE_ATTRIBUTE_REPARSE_POINT, Environment
from ..util import paths as pathutil
from . import syncroots as syncroots_mod


@dataclass(slots=True)
class TreeMeasurement:
    """What a walk of one tree found."""

    root: str
    size_bytes: int = 0
    file_count: int = 0
    dir_count: int = 0
    exists: bool = True
    skipped: list[SkippedGroup] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    long_path_count: int = 0
    placeholder_count: int = 0
    #: Sync roots this tree (or part of it) lives inside. Their volume is
    #: reported on the sync-root line, not here.
    synced_into: set[str] = field(default_factory=set)

    @property
    def skipped_bytes(self) -> int:
        return sum(group.bytes for group in self.skipped)

    @property
    def skipped_files(self) -> int:
        return sum(group.files for group in self.skipped)


@dataclass(slots=True)
class _Accumulator:
    """Groups skip records so the report stays readable on a real profile."""

    groups: dict[tuple[str, str], SkippedGroup] = field(default_factory=dict)
    unreadable_examples: list[str] = field(default_factory=list)

    def add(
        self,
        reason: SkipReason,
        path: str,
        *,
        size: int = 0,
        files: int = 0,
        detail: str | None = None,
        key: str | None = None,
    ) -> None:
        group_key = (reason.value, key if key is not None else path)
        group = self.groups.get(group_key)
        if group is None:
            group = SkippedGroup(reason=reason, path=path, detail=detail)
            self.groups[group_key] = group
        group.bytes += size
        group.files += files

    def as_list(self) -> list[SkippedGroup]:
        return sorted(self.groups.values(), key=lambda g: (-g.bytes, g.path))


def measure_tree(
    root: os.PathLike[str] | str,
    config: ScanConfig,
    env: Environment,
    sync_roots: list[SyncRoot] | None = None,
    *,
    relative_base: os.PathLike[str] | str | None = None,
) -> TreeMeasurement:
    """Walk ``root`` and report what would be captured and what would not.

    ``relative_base`` is the path exclusion patterns are matched against --
    normally the profile root, so that ``AppData/Local/Temp/*`` means what it
    says regardless of which tree the walk started in.
    """
    root_path = Path(os.fspath(root))
    base = Path(os.fspath(relative_base)) if relative_base is not None else env.profile_root
    sync_roots = sync_roots or []
    measurement = TreeMeasurement(root=str(root_path))
    accumulator = _Accumulator()

    if not root_path.exists():
        measurement.exists = False
        return measurement

    # A tree that *is* inside a sync root is skipped whole, without descending.
    containing = syncroots_mod.find_root_for(str(root_path), sync_roots) if config.skip_synced else None
    if containing is not None:
        accumulator.add(
            SkipReason.SYNCED,
            str(root_path),
            detail=f"already synced by {containing.provider} ({containing.root})",
            key=f"synced:{containing.provider}:{containing.root}",
        )
        measurement.synced_into.add(containing.root)
        measurement.skipped = accumulator.as_list()
        return measurement

    _walk(root_path, base, config, env, sync_roots, measurement, accumulator)
    measurement.skipped = accumulator.as_list()
    if accumulator.unreadable_examples:
        measurement.notes.append(
            Note(
                Severity.WARNING,
                f"{len(accumulator.unreadable_examples)} path(s) under {root_path} could not be read",
                "; ".join(accumulator.unreadable_examples[:5]),
            )
        )
    return measurement


def _walk(
    root_path: Path,
    base: Path,
    config: ScanConfig,
    env: Environment,
    sync_roots: list[SyncRoot],
    measurement: TreeMeasurement,
    accumulator: _Accumulator,
) -> None:
    stack: list[Path] = [root_path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(pathutil.extended(current)))
        except OSError as exc:
            accumulator.add(
                SkipReason.UNREADABLE,
                str(current),
                detail="permission denied or path unavailable",
                key="unreadable",
            )
            accumulator.unreadable_examples.append(f"{current}: {exc.strerror or exc}")
            continue

        measurement.dir_count += 1
        for entry in entries:
            path = Path(entry.path)
            relative = pathutil.relative_posix(path, base)
            if pathutil.needs_long_path_support(entry.path):
                measurement.long_path_count += 1

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                accumulator.add(
                    SkipReason.UNREADABLE, str(path), detail="stat failed", key="unreadable"
                )
                accumulator.unreadable_examples.append(str(path))
                continue

            attributes = getattr(stat_result, "st_file_attributes", 0)

            if config.skip_synced:
                containing = syncroots_mod.find_root_for(str(path), sync_roots)
                if containing is not None:
                    # Bytes are attributed to the sync root itself (measured
                    # once by syncroots.measure) so they are not counted twice.
                    accumulator.add(
                        SkipReason.SYNCED,
                        containing.root,
                        detail=f"already synced by {containing.provider}",
                        key=f"synced:{containing.provider}:{containing.root}",
                    )
                    measurement.synced_into.add(containing.root)
                    continue

            if config.is_excluded(relative, entry.name):
                regenerable = is_dir and config.is_regenerable(entry.name)
                reason = SkipReason.REGENERABLE if regenerable else SkipReason.EXCLUDED
                size, files = _measure_raw(path, config) if is_dir else (stat_result.st_size, 1)
                accumulator.add(
                    reason,
                    str(root_path),
                    size=size,
                    files=files,
                    detail=(
                        "regenerable build/dependency output (use --include-regenerable to keep)"
                        if regenerable
                        else "matched an exclusion rule"
                    ),
                    key=f"{reason.value}:{root_path}",
                )
                continue

            if attributes & FILE_ATTRIBUTE_REPARSE_POINT and not config.follow_reparse_points:
                accumulator.add(
                    SkipReason.REPARSE_POINT,
                    str(root_path),
                    files=0 if is_dir else 1,
                    detail="junction or symlink; not followed",
                    key=f"reparse:{root_path}",
                )
                continue

            if is_dir:
                stack.append(path)
                continue

            if attributes & CLOUD_PLACEHOLDER_MASK:
                measurement.placeholder_count += 1
                accumulator.add(
                    SkipReason.CLOUD_PLACEHOLDER,
                    str(root_path),
                    size=stat_result.st_size,
                    files=1,
                    detail="online-only file; its bytes are not on this disk",
                    key=f"placeholder:{root_path}",
                )
                continue

            measurement.size_bytes += stat_result.st_size
            measurement.file_count += 1


def _measure_raw(path: Path, config: ScanConfig) -> tuple[int, int]:
    """Total size and file count of a subtree we are about to skip.

    Returns ``(0, 0)`` when ``measure_skipped`` is off, so a fast scan does not
    pay for directories it will not capture.
    """
    if not config.measure_skipped:
        return (0, 0)
    total = 0
    files = 0
    stack = [path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(pathutil.extended(current)))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(Path(entry.path))
                else:
                    total += entry.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                continue
    return (total, files)
