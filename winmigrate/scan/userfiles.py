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
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ScanConfig
from ..models import Note, Severity, SkippedGroup, SkipReason, SyncRoot
from ..platform_win import CLOUD_PLACEHOLDER_MASK, FILE_ATTRIBUTE_REPARSE_POINT, Environment
from ..util import paths as pathutil
from .. import compression
from . import syncroots as syncroots_mod


@dataclass(slots=True)
class CaptureFile:
    """A file the plan says to capture."""

    path: Path
    relative: str          # relative to the tree root, POSIX separators
    size: int


@dataclass(slots=True)
class SkipEvent:
    """Something the plan says to leave out, and why."""

    reason: SkipReason
    path: Path
    attributed_to: Path    # the tree the skip is reported against
    size: int = 0
    files: int = 0
    detail: str | None = None
    key: str | None = None
    sync_root: str | None = None


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
    #: Bytes in files whose extension does not say they are already
    #: compressed. Decides whether the capture compresses at all.
    compressible_bytes: int = 0
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

    for event in walk_tree(
        root_path, config, env, sync_roots, relative_base=base, measurement=measurement
    ):
        if isinstance(event, CaptureFile):
            measurement.size_bytes += event.size
            measurement.file_count += 1
        else:
            accumulator.add(
                event.reason,
                str(event.attributed_to),
                size=event.size,
                files=event.files,
                detail=event.detail,
                key=event.key,
            )
            if event.sync_root:
                measurement.synced_into.add(event.sync_root)
            if event.reason is SkipReason.UNREADABLE:
                accumulator.unreadable_examples.append(str(event.path))
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


def walk_tree(
    root: os.PathLike[str] | str,
    config: ScanConfig,
    env: Environment,
    sync_roots: list[SyncRoot] | None = None,
    *,
    relative_base: os.PathLike[str] | str | None = None,
    measurement: TreeMeasurement | None = None,
) -> Iterator[CaptureFile | SkipEvent]:
    """Yield what a tree contains, decision by decision.

    This is the single implementation of the capture rules. The preview counts
    these events and the capture stage acts on them, so the plan a user approves
    and the work that follows cannot diverge -- there is no second walk to fall
    out of step.
    """
    root_path = Path(os.fspath(root))
    base = Path(os.fspath(relative_base)) if relative_base is not None else env.profile_root
    sync_roots = sync_roots or []

    # A tree that *is* inside a sync root is skipped whole, without descending.
    containing = (
        syncroots_mod.find_root_for(str(root_path), sync_roots) if config.skip_synced else None
    )
    if containing is not None:
        yield SkipEvent(
            reason=SkipReason.SYNCED,
            path=root_path,
            attributed_to=root_path,
            detail=f"already synced by {containing.provider} ({containing.root})",
            key=f"synced:{containing.provider}:{containing.root}",
            sync_root=containing.root,
        )
        return

    stack: list[Path] = [root_path]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(pathutil.extended(current)))
        except OSError as exc:
            yield SkipEvent(
                reason=SkipReason.UNREADABLE,
                path=current,
                attributed_to=current,
                detail=f"permission denied or path unavailable: {exc.strerror or exc}",
                key="unreadable",
            )
            continue

        if measurement is not None:
            measurement.dir_count += 1

        for entry in entries:
            # Built from the canonical parent, never from ``entry.path``: the
            # directory was opened through pathutil.extended(), so entry.path
            # carries the \\?\ prefix on Windows and would poison every
            # relative path, exclusion match and recorded source path.
            path = current / entry.name
            relative = pathutil.relative_posix(path, base)
            if measurement is not None and pathutil.needs_long_path_support(path):
                measurement.long_path_count += 1

            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat_result = entry.stat(follow_symlinks=False)
            except OSError:
                yield SkipEvent(
                    reason=SkipReason.UNREADABLE,
                    path=path,
                    attributed_to=root_path,
                    detail="stat failed",
                    key="unreadable",
                )
                continue

            attributes = getattr(stat_result, "st_file_attributes", 0)

            if config.skip_synced:
                containing = syncroots_mod.find_root_for(str(path), sync_roots)
                if containing is not None:
                    # Bytes are attributed to the sync root itself (measured
                    # once by syncroots.measure) so they are not counted twice.
                    yield SkipEvent(
                        reason=SkipReason.SYNCED,
                        path=path,
                        attributed_to=Path(containing.root),
                        detail=f"already synced by {containing.provider}",
                        key=f"synced:{containing.provider}:{containing.root}",
                        sync_root=containing.root,
                    )
                    continue

            if config.is_excluded(relative, entry.name):
                regenerable = is_dir and config.is_regenerable(entry.name)
                reason = SkipReason.REGENERABLE if regenerable else SkipReason.EXCLUDED
                size, files = _measure_raw(path, config) if is_dir else (stat_result.st_size, 1)
                yield SkipEvent(
                    reason=reason,
                    path=path,
                    attributed_to=root_path,
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
                yield SkipEvent(
                    reason=SkipReason.REPARSE_POINT,
                    path=path,
                    attributed_to=root_path,
                    files=0 if is_dir else 1,
                    detail="junction or symlink; not followed",
                    key=f"reparse:{root_path}",
                )
                continue

            if is_dir:
                stack.append(path)
                continue

            if attributes & CLOUD_PLACEHOLDER_MASK:
                if measurement is not None:
                    measurement.placeholder_count += 1
                yield SkipEvent(
                    reason=SkipReason.CLOUD_PLACEHOLDER,
                    path=path,
                    attributed_to=root_path,
                    size=stat_result.st_size,
                    files=1,
                    detail="online-only file; its bytes are not on this disk",
                    key=f"placeholder:{root_path}",
                )
                continue

            if measurement is not None and not compression.is_incompressible(entry.name):
                measurement.compressible_bytes += stat_result.st_size

            yield CaptureFile(
                path=path,
                relative=pathutil.relative_posix(path, root_path),
                size=stat_result.st_size,
            )


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
                    stack.append(current / entry.name)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
                    files += 1
            except OSError:
                continue
    return (total, files)
