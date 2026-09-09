"""The capture stage: turn an approved plan into an encrypted bundle.

Capture walks the same :func:`winmigrate.scan.walk_tree` generator the preview
counted, so it cannot take something the preview did not show. Files are
streamed straight into the bundle -- there is no staging directory, because a
217 GiB profile cannot afford a second copy on disk.

Three things make this survivable on a real machine:

* a **space pre-check** before a byte is written, since discovering the disk is
  full 200 GiB in is the worst possible time to find out;
* a **shadow copy** where one can be created, so files held open by running
  programs are captured cleanly rather than skipped or torn;
* **per-file failure isolation** -- one unreadable file is recorded and the
  capture continues, instead of losing the whole run.

Capture cannot be resumed. The bundle is a single authenticated stream, so an
interrupted capture leaves no usable prefix; the partial file is deleted rather
than left looking restorable, and the run must start again.
"""

from __future__ import annotations

import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from . import bundle as bundle_mod
from . import crypto, manifest as manifest_mod, vss
from .config import ScanConfig
from .errors import WinMigrateError
from .models import Action, Item, Kind, Note, ScanResult, Severity, utcnow
from .platform_win import Environment
from .scan import CaptureFile, walk_tree
from .util import hashing
from .util import humanize

log = logging.getLogger(__name__)

#: Leave the target volume some room rather than filling it exactly.
FREE_SPACE_MARGIN = 512 * 1024 * 1024

ProgressCallback = Callable[[str, int], None]


class CaptureError(WinMigrateError):
    """Capture could not start, or could not finish safely."""


@dataclass(slots=True)
class CaptureOptions:
    output: Path
    passphrase: str
    use_vss: bool = True
    skip_space_check: bool = False


@dataclass(slots=True)
class CaptureReport:
    """What capture actually did, as opposed to what the plan proposed."""

    bundle_path: Path
    manifest_path: Path
    captured_bytes: int = 0
    captured_files: int = 0
    bundle_bytes: int = 0
    duration_seconds: float = 0.0
    used_shadow_copy: bool = False
    failures: list[tuple[str, str]] = field(default_factory=list)
    notes: list[Note] = field(default_factory=list)

    @property
    def compression_ratio(self) -> float:
        return (self.bundle_bytes / self.captured_bytes) if self.captured_bytes else 1.0


def check_free_space(destination: Path, required_bytes: int) -> tuple[bool, int]:
    """Is there room for the bundle? Returns ``(ok, free_bytes)``."""
    target = destination if destination.is_dir() else destination.parent
    target.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(target).free
    return free >= required_bytes + FREE_SPACE_MARGIN, free


def capture(
    scan: ScanResult,
    options: CaptureOptions,
    config: ScanConfig,
    env: Environment,
    progress: ProgressCallback | None = None,
) -> CaptureReport:
    """Write the planned items into an encrypted bundle."""
    started = time.monotonic()
    output = Path(os.fspath(options.output))
    if output.suffix != ".dat":
        output = output.with_suffix(".dat")
    manifest_path = output.with_suffix(".manifest.json")

    totals = scan.totals()
    if not options.skip_space_check:
        ok, free = check_free_space(output, totals.capture_bytes)
        if not ok:
            raise CaptureError(
                f"not enough free space at {output.parent}: the plan needs about "
                f"{humanize.bytes_(totals.capture_bytes)} and only "
                f"{humanize.bytes_(free)} is free. Compression may reduce this, but "
                f"it cannot be relied on for media or disk images. Free some space, "
                f"choose another destination, or pass --no-space-check to try anyway."
            )

    report = CaptureReport(bundle_path=output, manifest_path=manifest_path)
    shadow = _open_shadow_copy(scan, options, report)

    kdf = crypto.default_kdf_params()
    header = {
        "format": manifest_mod.BUNDLE_FORMAT_VERSION,
        "schema_version": manifest_mod.SCHEMA_VERSION,
        "tool": {"name": "winmigrate", "version": __version__},
        "created_utc": utcnow(),
        "cipher": manifest_mod.CIPHER,
        "compression": manifest_mod.DEFAULT_COMPRESSION,
        "kdf": crypto.kdf_params_to_json(kdf),
    }

    try:
        with bundle_mod.BundleWriter(output, options.passphrase, header) as writer:
            for item in scan.items:
                if item.action is not Action.CAPTURE or item.kind not in {Kind.TREE, Kind.FILE}:
                    continue
                _capture_item(item, writer, scan, config, env, shadow, report, progress)
            manifest = manifest_mod.build(scan)
            writer.add_bytes(
                manifest_mod.MANIFEST_ARCHIVE_NAME,
                manifest_mod.dumps(manifest).encode("utf-8"),
            )
    finally:
        if shadow is not None:
            shadow.remove()

    result = writer.result
    report.bundle_bytes = result.ciphertext_size
    report.duration_seconds = time.monotonic() - started

    info = manifest_mod.BundleInfo(
        filename=output.name,
        compression=manifest_mod.DEFAULT_COMPRESSION,
        kdf=crypto.kdf_params_to_json(kdf),
        salt=kdf.salt,
        ciphertext_sha256=result.ciphertext_sha256,
        ciphertext_size=result.ciphertext_size,
        payload_sha256=result.payload_sha256,
        payload_size=result.payload_size,
    )
    full_manifest = manifest_mod.build(scan, info)
    manifest_path.write_text(
        manifest_mod.dumps(manifest_mod.public_view(full_manifest)), encoding="utf-8"
    )
    log.info("bundle written: %s (%s)", output, humanize.bytes_(report.bundle_bytes))
    return report


def _open_shadow_copy(scan: ScanResult, options: CaptureOptions, report: CaptureReport):
    """Try for a shadow copy; carry on without one, saying so."""
    if not options.use_vss or not vss.is_windows():
        return None
    try:
        volume = vss.volume_of(scan.source.profile_path)
        shadow = vss.create(volume)
        report.used_shadow_copy = True
        return shadow
    except vss.ShadowCopyError as exc:
        report.notes.append(
            Note(
                Severity.WARNING,
                "capturing without a shadow copy: files held open by running "
                "programs may be unreadable or inconsistent",
                str(exc),
            )
        )
        log.warning("no shadow copy: %s", exc)
        return None


def _capture_item(
    item: Item,
    writer: bundle_mod.BundleWriter,
    scan: ScanResult,
    config: ScanConfig,
    env: Environment,
    shadow,
    report: CaptureReport,
    progress: ProgressCallback | None,
) -> None:
    if not item.source_path or not item.archive_path:
        return
    root = Path(item.source_path)
    digests: list[tuple[str, str]] = []
    captured_bytes = 0

    for event in walk_tree(root, config, env, scan.sync_roots, relative_base=env.profile_root):
        if not isinstance(event, CaptureFile):
            continue
        archive_name = f"{item.archive_path}/{event.relative}"
        source = shadow.map(event.path) if shadow is not None else event.path
        try:
            digest = writer.add_file(source, archive_name)
        except OSError as exc:
            # One locked or vanished file must not cost the whole capture.
            reason = exc.strerror or str(exc)
            report.failures.append((str(event.path), reason))
            log.warning("could not capture %s: %s", event.path, reason)
            continue
        digests.append((event.relative, digest))
        captured_bytes += event.size
        report.captured_bytes += event.size
        report.captured_files += 1
        if progress is not None:
            progress(item.title, event.size)

    item.digest = hashing.tree_digest(digests)
    item.digest_algo = hashing.TREE_DIGEST_ALGO
    # Record what was actually taken, which may differ from what was measured if
    # the profile changed between the scan and the capture.
    item.size_bytes = captured_bytes
    item.file_count = len(digests)


def default_bundle_name(scan: ScanResult) -> str:
    host = scan.source.hostname or "machine"
    user = scan.source.username or "user"
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{host}-{user}-{stamp}.dat"
