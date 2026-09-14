"""Reading a bundle back, to earn the right to wipe the machine it came from.

``inspect`` checks the ciphertext digest, which catches a download that was cut
short or a drive that went bad in transit. That is worth having, and it is not
the question someone is actually asking before they reformat a laptop. They want
to know that the bundle *opens*, that every file inside it is there, and that
each one still hashes to what the manifest says it hashed to when it was read
off the disk that is about to be erased.

So this decrypts the whole thing and hashes every member, comparing against the
manifest's own record. It writes nothing. It is the restore's verification pass
without the restore -- the same digests, the same tree rule, and the same
distinction between "this item is intact" and "I could not check this item".

Three failures it is meant to find, in rising order of how badly you want to
know:

* a member whose bytes no longer match what was recorded -- the disk lied, or
  something altered the bundle after it was written;
* an item the manifest lists and the archive does not contain;
* a bundle that cannot be decrypted or whose stream ends early, which every
  other check would have called fine.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import bundle as bundle_mod
from . import manifest as manifest_mod
from .errors import IntegrityError
from .util.hashing import tree_digest

log = logging.getLogger(__name__)

READ_CHUNK = 1024 * 1024


@dataclass(slots=True)
class VerifyReport:
    """What reading the bundle back established."""

    bundle: Path
    files_checked: int = 0
    bytes_checked: int = 0
    items_checked: int = 0
    #: Items whose contents no longer hash to what the manifest recorded.
    mismatches: list[str] = field(default_factory=list)
    #: Items the manifest describes that the archive does not contain.
    missing: list[str] = field(default_factory=list)
    #: Items with no recorded digest to compare against. Records and reports
    #: have none by nature; a file item without one is worth saying out loud.
    unchecked: list[str] = field(default_factory=list)
    sidecar_verified: bool = False
    duration_seconds: float = 0.0

    @property
    def ok(self) -> bool:
        return not self.mismatches and not self.missing


def verify(
    bundle_path: Path,
    passphrase: str,
    progress=None,
) -> VerifyReport:
    """Decrypt a bundle and check every file against the manifest.

    Raises :class:`~winmigrate.errors.IntegrityError` when the bundle cannot be
    read at all -- a wrong passphrase, a truncated stream, a failed
    authentication tag. Those are not findings to collect and report at the end;
    they mean there is nothing to verify.
    """
    started = time.monotonic()
    bundle_path = Path(bundle_path)
    if not bundle_path.is_file():
        raise IntegrityError(f"bundle not found: {bundle_path}")

    report = VerifyReport(bundle=bundle_path)

    from .restore import verify_sidecar  # noqa: PLC0415 -- avoid a cycle

    checked, error = verify_sidecar(bundle_path)
    if error:
        # The ciphertext does not match what was written. Reading on would only
        # produce a second, more confusing failure.
        raise IntegrityError(error)
    report.sidecar_verified = checked

    # archive name -> digest, built as the stream is read.
    digests: dict[str, str] = {}
    manifest: dict | None = None

    with bundle_mod.BundleReader(bundle_path, passphrase) as reader:
        for info, stream in reader.members():
            if not info.isfile() or stream is None:
                continue
            if info.name == manifest_mod.MANIFEST_ARCHIVE_NAME:
                manifest = json.loads(stream.read().decode("utf-8"))
                continue
            digest = hashlib.sha256()
            size = 0
            while True:
                block = stream.read(READ_CHUNK)
                if not block:
                    break
                digest.update(block)
                size += len(block)
            digests[info.name] = digest.hexdigest()
            report.files_checked += 1
            report.bytes_checked += size
            if progress is not None and report.files_checked % 200 == 0:
                progress(info.name, size)

    if manifest is None:
        raise IntegrityError(
            "the bundle contains no manifest: it is incomplete or not a WinMigrate bundle"
        )
    manifest_mod.validate(manifest)
    _compare(report, manifest, digests)
    report.duration_seconds = time.monotonic() - started
    return report


def _compare(report: VerifyReport, manifest: dict, digests: dict[str, str]) -> None:
    """Check each item's recorded digest against what the archive actually held."""
    for item in manifest.get("items", []):
        if not isinstance(item, dict):
            continue
        archive_path = item.get("archive_path")
        recorded = item.get("digest")
        item_id = str(item.get("id", "?"))

        if not archive_path:
            continue  # a record or a report: nothing in the archive to check
        if not recorded:
            report.unchecked.append(item_id)
            continue

        report.items_checked += 1

        if item.get("kind") == "file":
            # A single file's archive name is its path exactly, with no trailing
            # separator -- prefix matching would never find it.
            actual = digests.get(archive_path)
            if actual is None:
                report.missing.append(item_id)
                log.error("%s: the manifest lists %s and the archive has no such member",
                          item_id, archive_path)
            elif actual != recorded:
                report.mismatches.append(item_id)
                log.error("%s: contents do not match what was recorded", item_id)
            continue

        prefix = f"{archive_path}/"
        members = {
            name[len(prefix):]: value
            for name, value in digests.items()
            if name.startswith(prefix)
        }
        if not members:
            report.missing.append(item_id)
            log.error("%s: the manifest lists %s and the archive has nothing under it",
                      item_id, archive_path)
            continue
        if tree_digest(members.items()) != recorded:
            report.mismatches.append(item_id)
            log.error("%s: the files under %s do not match what was recorded",
                      item_id, archive_path)
