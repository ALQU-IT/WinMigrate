"""The in-memory object model shared by every stage.

The manifest schema is defined here first and serialized by
:mod:`winmigrate.manifest`; ``schema/manifest.schema.json`` is the machine
readable mirror of these classes and ``docs/manifest-schema.md`` the prose one.
Keep the three in step when changing a field.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any


class Category(str, Enum):
    """What kind of thing an item is. Drives grouping in reports."""

    USER_FILES = "user_files"
    BROWSER_PROFILE = "browser_profile"
    BROWSER_PASSWORDS = "browser_passwords"
    WIFI = "wifi"
    PRINTERS = "printers"
    MAPPED_DRIVES = "mapped_drives"
    ENV_VARS = "env_vars"
    SCHEDULED_TASKS = "scheduled_tasks"
    FONTS = "fonts"
    OUTLOOK = "outlook"
    NOTEPAD = "notepad"
    DEV_CONFIG = "dev_config"
    FILE_ASSOCIATIONS = "file_associations"
    SOFTWARE = "software"
    OFFICE = "office"


class Kind(str, Enum):
    """The shape of the payload behind an item."""

    TREE = "tree"          # a directory copied recursively
    FILE = "file"          # a single file
    RECORD = "record"      # structured data captured into the manifest itself
    REPORT = "report"      # captured for reporting only; nothing is restored


class Action(str, Enum):
    """What the capture stage will do with an item."""

    CAPTURE = "capture"    # copied into the bundle
    SKIP = "skip"          # deliberately not captured (see skip_reason)
    MANUAL = "manual"      # needs the user; see the follow-up entry


class Sensitivity(str, Enum):
    """Whether an item's payload is credential material.

    ``SECRET`` items are written only into the encrypted bundle. Their paths,
    contents and notes never reach the plaintext sidecar manifest or the log.
    """

    NORMAL = "normal"
    SECRET = "secret"


class RestoreStrategy(str, Enum):
    """How an item is put back on the target machine."""

    MERGE = "merge"        # copy in, keep existing files that are not in the bundle
    REPLACE = "replace"    # target is emptied first
    GUIDED = "guided"      # tool prepares, user completes (sign-in, activation)
    MANUAL = "manual"      # reported only; user does it


class SkipReason(str, Enum):
    """Why an item or subtree is not being captured."""

    SYNCED = "synced"                    # already in a sync provider's root
    EXCLUDED = "excluded"                # matched an exclusion rule
    REGENERABLE = "regenerable"          # build/dependency output that rebuilds itself
    REPARSE_POINT = "reparse_point"      # junction/symlink; not followed
    CLOUD_PLACEHOLDER = "cloud_placeholder"  # online-only file; downloading it is worse than skipping
    EMPTY = "empty"
    UNREADABLE = "unreadable"
    NOT_PRESENT = "not_present"
    FILES_ONLY_MODE = "files_only_mode"  # secret excluded by --files-only


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


def utcnow() -> str:
    """Timestamp in the single format used everywhere in a manifest."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(slots=True)
class Note:
    """A message attached to the scan, an item, or a stage result."""

    severity: Severity
    message: str
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = {"severity": self.severity.value, "message": self.message}
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass(slots=True)
class SyncRoot:
    """A detected cloud-sync folder whose contents we deliberately skip."""

    provider: str                    # "onedrive" | "nextcloud" | ...
    root: str                        # absolute path of the synced folder
    label: str | None = None         # e.g. "OneDrive - Contoso"
    account_hint: str | None = None  # never a full credential; e.g. "personal"
    known_folders_redirected: list[str] = field(default_factory=list)
    bytes_skipped: int = 0
    files_skipped: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "root": self.root,
            "label": self.label,
            "account_hint": self.account_hint,
            "known_folders_redirected": list(self.known_folders_redirected),
            "bytes_skipped": self.bytes_skipped,
            "files_skipped": self.files_skipped,
        }


@dataclass(slots=True)
class SkippedGroup:
    """Aggregate of what was skipped beneath an item, kept for the report."""

    reason: SkipReason
    path: str
    bytes: int = 0
    files: int = 0
    detail: str | None = None

    def to_json(self) -> dict[str, Any]:
        data = {
            "reason": self.reason.value,
            "path": self.path,
            "bytes": self.bytes,
            "files": self.files,
        }
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass(slots=True)
class RestoreSpec:
    """Where and how an item lands on the target machine."""

    target: str                      # may contain %USERPROFILE% etc.
    strategy: RestoreStrategy = RestoreStrategy.MERGE
    requires_elevation: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "target": self.target,
            "strategy": self.strategy.value,
            "requires_elevation": self.requires_elevation,
            "notes": list(self.notes),
        }


@dataclass(slots=True)
class Item:
    """One unit of migration: a tree, a file, or a structured record.

    ``size_bytes``/``file_count`` describe what will actually be captured;
    anything excluded is accounted for separately in ``skipped``.
    """

    id: str
    category: Category
    kind: Kind
    title: str
    source_path: str | None = None
    archive_path: str | None = None
    action: Action = Action.CAPTURE
    sensitivity: Sensitivity = Sensitivity.NORMAL
    skip_reason: SkipReason | None = None
    size_bytes: int = 0
    file_count: int = 0
    #: Bytes in files that might actually shrink. Decides whether the capture
    #: compresses at all; see :mod:`winmigrate.compression`.
    compressible_bytes: int = 0
    skipped: list[SkippedGroup] = field(default_factory=list)
    restore: RestoreSpec | None = None
    digest: str | None = None            # filled by the package stage
    digest_algo: str | None = None
    record: dict[str, Any] | None = None  # payload for Kind.RECORD items
    #: Whether ``record`` may appear in the plaintext sidecar manifest. Off by
    #: default: an installed-software inventory is not credential material, but
    #: it fingerprints the machine (and its unpatched versions) in a file that
    #: sits next to the bundle, and the sidecar exists to identify a bundle, not
    #: to describe its contents.
    record_public: bool = False
    notes: list[Note] = field(default_factory=list)

    @property
    def skipped_bytes(self) -> int:
        return sum(group.bytes for group in self.skipped)

    @property
    def skipped_files(self) -> int:
        return sum(group.files for group in self.skipped)

    @property
    def is_secret(self) -> bool:
        return self.sensitivity is Sensitivity.SECRET

    def to_json(self, *, redact_secrets: bool = False) -> dict[str, Any]:
        """Serialize.

        With ``redact_secrets`` the item is reduced to what is safe for the
        plaintext sidecar manifest: identity, size and disposition, but no
        path, record payload or free-text note that could leak the material.
        """
        data: dict[str, Any] = {
            "id": self.id,
            "category": self.category.value,
            "kind": self.kind.value,
            "title": self.title,
            "action": self.action.value,
            "sensitivity": self.sensitivity.value,
            "size_bytes": self.size_bytes,
            "file_count": self.file_count,
        }
        if self.skip_reason:
            data["skip_reason"] = self.skip_reason.value
        if redact_secrets and self.is_secret:
            data["redacted"] = True
            return data
        if self.source_path:
            data["source_path"] = self.source_path
        if self.archive_path:
            data["archive_path"] = self.archive_path
        if self.skipped:
            data["skipped"] = [group.to_json() for group in self.skipped]
        if self.restore:
            data["restore"] = self.restore.to_json()
        if self.digest:
            data["digest"] = self.digest
            data["digest_algo"] = self.digest_algo
        if self.record is not None:
            data["record"] = self.record
            if self.record_public:
                data["record_public"] = True
        if self.notes:
            data["notes"] = [note.to_json() for note in self.notes]
        return data


@dataclass(slots=True)
class Followup:
    """Something only the human can finish: a sign-in, an activation, an import.

    These are produced during scan (so the preview is honest about them) and
    reproduced verbatim in the restore report.
    """

    id: str
    title: str
    why: str
    steps: list[str] = field(default_factory=list)
    category: Category | None = None
    required: bool = True

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "why": self.why,
            "steps": list(self.steps),
            "category": self.category.value if self.category else None,
            "required": self.required,
        }


@dataclass(slots=True)
class SourceMachine:
    """Identity of the machine and account the bundle was captured from."""

    hostname: str = ""
    username: str = ""
    profile_path: str = ""
    os_name: str = ""
    os_version: str = ""
    os_build: str = ""
    architecture: str = ""
    locale: str = ""
    is_windows: bool = True

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class Totals:
    """Roll-up used by the preview and by the space pre-check."""

    capture_bytes: int = 0
    capture_files: int = 0
    #: How much of capture_bytes might actually shrink.
    compressible_bytes: int = 0
    skipped_bytes: int = 0
    skipped_files: int = 0
    item_count: int = 0
    secret_item_count: int = 0
    manual_item_count: int = 0

    def to_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(slots=True)
class ScanResult:
    """Everything the scan stage learned. Input to preview, capture and package."""

    source: SourceMachine
    items: list[Item] = field(default_factory=list)
    sync_roots: list[SyncRoot] = field(default_factory=list)
    followups: list[Followup] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    started_utc: str = field(default_factory=utcnow)
    finished_utc: str | None = None
    duration_seconds: float = 0.0
    files_only: bool = False

    def totals(self) -> Totals:
        totals = Totals(item_count=len(self.items))
        for item in self.items:
            if item.action is Action.CAPTURE:
                totals.capture_bytes += item.size_bytes
                totals.capture_files += item.file_count
                totals.compressible_bytes += item.compressible_bytes
            elif item.action is Action.MANUAL:
                totals.manual_item_count += 1
            totals.skipped_bytes += item.skipped_bytes
            totals.skipped_files += item.skipped_files
            if item.action is Action.SKIP:
                totals.skipped_bytes += item.size_bytes
                totals.skipped_files += item.file_count
            if item.is_secret:
                totals.secret_item_count += 1
        return totals

    def items_by_category(self) -> dict[Category, list[Item]]:
        grouped: dict[Category, list[Item]] = {}
        for item in self.items:
            grouped.setdefault(item.category, []).append(item)
        return grouped

    def add_note(self, severity: Severity, message: str, detail: str | None = None) -> None:
        self.notes.append(Note(severity, message, detail))
