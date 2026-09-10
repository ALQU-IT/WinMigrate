"""Whether compressing the payload is worth what it costs.

A capture's throughput was set by gzip and nothing else. Measured on one
machine: gzip level 9 manages about 35 MB/s on data that is already compressed
-- and returns it at a ratio of 1.000, having achieved precisely nothing. The
stages either side are not close to being the limit: SHA-256 runs at roughly
1 GB/s and AES-256-GCM at roughly 550 MB/s. So on a profile full of photos and
video, the pipeline spent its entire time on a step that did no work, and a
217 GiB capture took two hours because of it.

The fix is not a faster setting, it is knowing when to skip the step. A profile
is usually two very different populations of file:

* documents, source code, configuration, mail stores -- which compress by
  hundreds to one and are a rounding error in bytes;
* photos, video, audio, archives, installers, Office files -- which are already
  compressed, cannot be compressed again, and are essentially all of the bytes.

So the scan counts which of the two the bytes fall into, and the capture
compresses only when there is something to gain. Level 1 rather than 9 when it
does: on compressible data level 1 runs 2.7x faster for the same ratio to three
decimal places, and level 9's extra effort only ever shows up on data this is
about to skip anyway.

The choice is recorded in the bundle header, so a reader honours whatever the
writer decided rather than assuming.
"""

from __future__ import annotations

#: Extensions whose contents are already compressed. Re-compressing these costs
#: full time for no gain. Office and OpenDocument formats are zip containers;
#: PDFs are usually deflate-compressed internally; disk images and installers
#: carry their own compression.
INCOMPRESSIBLE_SUFFIXES: frozenset[str] = frozenset(
    {
        # photos and raw
        ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".avif",
        ".cr2", ".cr3", ".nef", ".arw", ".dng", ".raf", ".orf", ".rw2",
        # video
        ".mp4", ".m4v", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm",
        ".mpg", ".mpeg", ".m2ts", ".mts", ".vob", ".3gp",
        # audio
        ".mp3", ".aac", ".m4a", ".ogg", ".oga", ".opus", ".wma", ".flac",
        ".ape", ".alac",
        # archives and packages
        ".zip", ".7z", ".rar", ".gz", ".bz2", ".xz", ".zst", ".lz4", ".cab",
        ".tgz", ".tbz2", ".txz", ".jar", ".war", ".apk", ".nupkg", ".whl",
        ".crx", ".xpi", ".msix", ".appx",
        # documents that are zip containers or internally compressed
        ".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp", ".epub",
        # disk images, installers, media images
        ".iso", ".vhd", ".vhdx", ".vmdk", ".vdi", ".qcow2", ".dmg", ".wim",
        ".esd", ".msi", ".msu",
        # already-compressed odds and ends
        ".woff", ".woff2", ".ipa", ".pak", ".bin.gz",
    }
)

#: Compress only when at least this share of the bytes might actually shrink.
#: Below it the compressor is being paid in full to return its input.
COMPRESSIBLE_BYTE_THRESHOLD = 0.10

#: What goes in the bundle header. The *algorithm*, not the level: gzip decodes
#: at any level, so the level is a writer-side decision that never has to be
#: understood by a reader.
GZIP = "gzip"
NONE = "none"

#: gzip level used when compressing is worth it. Level 1, not 9 -- 2.7x the
#: throughput for the same ratio on anything that actually compresses.
FAST_LEVEL = 1
BEST_LEVEL = 6

#: What --compression accepts.
CHOICES = ("auto", "none", "fast", "best")


def is_incompressible(name: str) -> bool:
    """True when a filename's extension says its contents are already compressed."""
    lowered = name.lower()
    dot = lowered.rfind(".")
    if dot < 0:
        return False
    return lowered[dot:] in INCOMPRESSIBLE_SUFFIXES


def choose(setting: str, compressible_bytes: int, total_bytes: int) -> tuple[str, int]:
    """Return ``(algorithm, level)`` for this capture.

    ``auto`` compresses only when enough of the payload could shrink to pay for
    the time. On a profile that is mostly media that means not at all, which is
    the difference between a capture bounded by gzip and one bounded by the
    disk it is writing to.
    """
    if setting == "none":
        return NONE, 0
    if setting == "fast":
        return GZIP, FAST_LEVEL
    if setting == "best":
        return GZIP, BEST_LEVEL
    if total_bytes <= 0:
        return GZIP, FAST_LEVEL
    share = compressible_bytes / total_bytes
    if share < COMPRESSIBLE_BYTE_THRESHOLD:
        return NONE, 0
    return GZIP, FAST_LEVEL


def explain(algorithm: str, compressible_bytes: int, total_bytes: int) -> str:
    """One line for the report saying what was decided and why."""
    from .util import humanize  # noqa: PLC0415

    share = (compressible_bytes / total_bytes * 100) if total_bytes else 0.0
    if algorithm == NONE:
        return (
            f"compression off: only {humanize.bytes_(compressible_bytes)} of "
            f"{humanize.bytes_(total_bytes)} ({share:.0f}%) could shrink, so compressing "
            f"would cost time and return the same bytes"
        )
    return (
        f"compressing: {humanize.bytes_(compressible_bytes)} of "
        f"{humanize.bytes_(total_bytes)} ({share:.0f}%) may shrink"
    )
