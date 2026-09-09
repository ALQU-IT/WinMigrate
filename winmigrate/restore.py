"""The restore stage: verify a bundle, put the files back, and say what is left.

Restore is the only stage that writes into a user profile, so it is the one that
has to be most careful:

* the bundle's ciphertext digest is checked **before** the passphrase is asked
  for, so transfer damage is caught without anyone typing a secret;
* every chunk is authenticated as it is read, so a truncated or altered bundle
  fails loudly rather than restoring a plausible-looking subset;
* each file is hashed as it is written and checked against the manifest, which
  catches corruption that happened on the source side before encryption;
* an interrupted restore can be re-run: files already present and matching are
  skipped, so progress is never thrown away.

What restore deliberately will **not** do is complete anything the OS gates
behind human authentication. Those become the follow-up list, reproduced from
the manifest, for the user to work through.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle as bundle_mod
from . import manifest as manifest_mod
from .errors import IntegrityError, WinMigrateError
from .models import Followup, Note, Severity
from .util import paths as pathutil

log = logging.getLogger(__name__)

ProgressCallback = Callable[[str, int], None]

COPY_CHUNK = 1024 * 1024


class RestoreError(WinMigrateError):
    """Restore could not proceed safely."""


@dataclass(slots=True)
class RestoreOptions:
    bundle: Path
    passphrase: str
    destination: Path | None = None   # defaults to the current user's profile
    dry_run: bool = False
    overwrite: bool = False           # replace files that differ, instead of keeping them
    items: tuple[str, ...] = ()       # restore only these item ids


@dataclass(slots=True)
class RestoreReport:
    """What restore did, and what the user still has to do."""

    bundle: Path
    destination: Path | None = None
    dry_run: bool = False
    restored_files: int = 0
    restored_bytes: int = 0
    skipped_existing: int = 0
    kept_existing: int = 0
    digest_mismatches: list[str] = field(default_factory=list)
    failures: list[tuple[str, str]] = field(default_factory=list)
    followups: list[Followup] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)
    duration_seconds: float = 0.0
    manifest: dict | None = None
    verified: bool = False

    @property
    def ok(self) -> bool:
        return not self.digest_mismatches and not self.failures


def inspect(bundle_path: os.PathLike[str] | str) -> dict:
    """Read a bundle's plaintext header. No passphrase required."""
    header, _digest, handle = bundle_mod.read_header(bundle_path)
    handle.close()
    return header


def load_sidecar(bundle_path: Path) -> dict | None:
    """Read the public sidecar manifest beside a bundle, if there is one."""
    import json  # noqa: PLC0415

    sidecar = bundle_path.with_suffix(".manifest.json")
    if not sidecar.is_file():
        return None
    try:
        return json.loads(sidecar.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def verify_sidecar(bundle_path: Path) -> tuple[bool, str | None]:
    """Check the bundle against its sidecar manifest, if one is beside it.

    Returns ``(checked, error)``. A missing sidecar is not an error -- the AEAD
    tags still authenticate everything -- but its digest lets damage be caught
    before a passphrase is entered.
    """
    sidecar = bundle_path.with_suffix(".manifest.json")
    if not sidecar.is_file():
        return False, None
    import json  # noqa: PLC0415

    try:
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        expected = data.get("bundle", {}).get("ciphertext", {}).get("sha256")
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"could not read {sidecar.name}: {exc}"
    if not expected:
        return False, None
    try:
        bundle_mod.verify_ciphertext(bundle_path, expected)
    except IntegrityError as exc:
        return True, str(exc)
    return True, None


def restore(options: RestoreOptions, progress: ProgressCallback | None = None) -> RestoreReport:
    """Restore a bundle onto this machine."""
    started = time.monotonic()
    bundle_path = Path(os.fspath(options.bundle))
    if not bundle_path.is_file():
        raise RestoreError(f"bundle not found: {bundle_path}")

    report = RestoreReport(bundle=bundle_path, dry_run=options.dry_run)
    checked, error = verify_sidecar(bundle_path)
    report.verified = checked
    if error:
        raise IntegrityError(error)
    if not checked:
        report.notes.append(
            Note(
                Severity.INFO,
                "no sidecar manifest beside the bundle; integrity rests on the "
                "bundle's own authentication tags",
            )
        )

    destination = Path(os.fspath(options.destination)) if options.destination else _default_profile()
    report.destination = destination
    written: dict[str, str] = {}

    # The authoritative manifest is the last member of the stream, so selecting
    # items by id has to be resolved from the sidecar before extraction starts.
    prefixes = _selected_prefixes(options, bundle_path)

    with bundle_mod.BundleReader(bundle_path, options.passphrase) as reader:
        for info, stream in reader.members():
            if not info.isfile() or stream is None:
                continue
            if info.name == manifest_mod.MANIFEST_ARCHIVE_NAME:
                report.manifest = _load_manifest(stream)
                continue
            if prefixes is not None and not any(
                info.name.startswith(f"{prefix}/") for prefix in prefixes
            ):
                continue
            _restore_member(info, stream, destination, options, report, written, progress)

    if report.manifest is None:
        raise IntegrityError(
            "the bundle contains no manifest: it is incomplete or not a WinMigrate bundle"
        )
    manifest_mod.validate(report.manifest)
    _check_digests(report, written)
    _collect_followups(report)
    report.duration_seconds = time.monotonic() - started
    return report


def _default_profile() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.path.expanduser("~"))


def _load_manifest(stream) -> dict:
    import json  # noqa: PLC0415

    try:
        return json.loads(stream.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError(f"the bundle's manifest is unreadable: {exc}") from exc


def _restore_member(info, stream, destination: Path, options: RestoreOptions,
                    report: RestoreReport, written: dict[str, str],
                    progress: ProgressCallback | None) -> None:
    target = _target_for(info.name, destination)
    if target is None:
        return

    if options.dry_run:
        report.restored_files += 1
        report.restored_bytes += info.size
        return

    existing = _existing_state(target, info.size)
    if existing == "same":
        # Re-running an interrupted restore must not redo finished work.
        report.skipped_existing += 1
        return
    if existing == "differs" and not options.overwrite:
        report.kept_existing += 1
        report.notes.append(
            Note(
                Severity.WARNING,
                f"kept the existing {target.name}",
                f"{target} differs from the bundle; pass --overwrite to replace it",
            )
        )
        return

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        # Written to a temporary name first: an interrupted write must not leave
        # a half-file that the next run would mistake for finished work.
        temporary = target.with_name(target.name + ".winmigrate-part")
        with open(pathutil.extended(temporary), "wb") as handle:
            while True:
                block = stream.read(COPY_CHUNK)
                if not block:
                    break
                digest.update(block)
                handle.write(block)
        os.replace(pathutil.extended(temporary), pathutil.extended(target))
    except OSError as exc:
        report.failures.append((str(target), exc.strerror or str(exc)))
        log.warning("could not restore %s: %s", target, exc)
        return

    written[info.name] = digest.hexdigest()
    report.restored_files += 1
    report.restored_bytes += info.size
    if progress is not None:
        progress(target.name, info.size)


def _selected_prefixes(options: RestoreOptions, bundle_path: Path) -> list[str] | None:
    """Archive-path prefixes for ``--item``, or ``None`` to restore everything."""
    if not options.items:
        return None
    sidecar = load_sidecar(bundle_path)
    if sidecar is None:
        raise RestoreError(
            "selecting items needs the sidecar manifest (<bundle>.manifest.json) "
            "beside the bundle, because the bundle's own manifest is only "
            "readable at the end of the stream"
        )
    known = {item.get("id") for item in sidecar.get("items", [])}
    unknown = sorted(set(options.items) - known)
    if unknown:
        raise RestoreError(f"no such item(s) in this bundle: {', '.join(unknown)}")
    prefixes = [
        item["archive_path"]
        for item in sidecar.get("items", [])
        if item.get("id") in options.items and item.get("archive_path")
    ]
    if not prefixes:
        raise RestoreError("the selected item(s) hold no files to restore")
    return prefixes


def _existing_state(target: Path, size: int) -> str:
    """``missing``, ``same`` (size matches) or ``differs``."""
    try:
        stat_result = os.stat(pathutil.extended(target))
    except OSError:
        return "missing"
    return "same" if stat_result.st_size == size else "differs"


def _target_for(archive_name: str, destination: Path) -> Path | None:
    """Map an archive path to a real one, refusing anything that escapes.

    A bundle is data, not a trusted instruction: a member named ``../../..`` or
    ``C:\\Windows\\System32\\...`` must not be able to write outside the
    destination just because someone handed the user a bundle.
    """
    name = archive_name.replace("\\", "/").lstrip("/")
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        log.warning("refusing bundle member with a parent reference: %s", archive_name)
        return None
    if len(parts) >= 2 and parts[0] == "data" and parts[1] == "user_files":
        parts = parts[2:]
        if parts and parts[0] == "_other":
            parts = parts[1:]
    elif parts and parts[0] == "data":
        parts = parts[1:]
    if not parts:
        return None
    if ":" in parts[0]:
        log.warning("refusing bundle member with a drive letter: %s", archive_name)
        return None
    return destination.joinpath(*parts)


def _check_digests(report: RestoreReport, written: dict[str, str]) -> None:
    """Compare what we wrote against the digests the manifest recorded."""
    manifest = report.manifest or {}
    for item in manifest.get("items", []):
        archive_path = item.get("archive_path")
        if not archive_path or not item.get("digest"):
            continue
        pairs = [
            (name[len(archive_path) + 1 :], digest)
            for name, digest in written.items()
            if name.startswith(f"{archive_path}/")
        ]
        if not pairs:
            continue
        from .util.hashing import tree_digest  # noqa: PLC0415

        if tree_digest(pairs) != item["digest"]:
            report.digest_mismatches.append(item["id"])
            log.error("digest mismatch for item %s", item["id"])


def _collect_followups(report: RestoreReport) -> None:
    for raw in (report.manifest or {}).get("followups", []):
        report.followups.append(
            Followup(
                id=raw.get("id", ""),
                title=raw.get("title", ""),
                why=raw.get("why", ""),
                steps=list(raw.get("steps", [])),
                required=raw.get("required", True),
            )
        )
