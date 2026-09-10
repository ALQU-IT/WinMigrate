"""The ``.dat`` container: framing, compression, and streamed tar membership.

Layout::

    magic ("WINMIGRATE\\0") || header_len (uint32 BE) || header_json || chunks...

The header is plaintext because the KDF parameters and salt are needed before a
key exists. Everything after it is the chunked AEAD stream from
:mod:`winmigrate.crypto`, whose plaintext is a gzip-compressed tar.

Everything is streamed. A bundle is never held in memory and never seeked, so
capturing a 200 GiB profile costs the same memory as capturing a small one.

The manifest is the *last* member of the tar. Its per-item digests cannot be
known until the files have been read, and reading them twice would double the
cost of a large capture. Restore therefore hashes each file as it extracts and
compares against the manifest when it arrives at the end. Authenticity of the
bundle as a whole does not depend on that ordering -- the AEAD tags already
guarantee it -- so the per-item digests serve their real purpose, which is
catching corruption that happened on the *source* side, before encryption.
"""

from __future__ import annotations

import gzip
import hashlib
import logging
import io
import json
import os
import struct
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator

from . import compression as compression_mod
from . import crypto
from .errors import IntegrityError, WinMigrateError
from .manifest import BUNDLE_MAGIC
from .util import paths as pathutil

log = logging.getLogger(__name__)

HEADER_LENGTH_SIZE = 4
MAX_HEADER_BYTES = 1024 * 1024


class BundleFormatError(WinMigrateError):
    """The file is not a WinMigrate bundle, or its framing is damaged."""


class _HashingWriter:
    """Passes writes through while accumulating a SHA-256 digest."""

    def __init__(self, stream: BinaryIO):
        self._stream = stream
        self._digest = hashlib.sha256()
        self.bytes_written = 0

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.bytes_written += len(data)
        return self._stream.write(data)

    def flush(self) -> None:
        flush = getattr(self._stream, "flush", None)
        if flush:
            flush()

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


class _ExactSizeReader:
    """Yields exactly ``size`` bytes, hashing them on the way past.

    A tar member declares its length in its header, and ``tarfile`` then copies
    exactly that many bytes. A live profile does not hold still: a log rotates,
    a browser rewrites its database, and the file can shrink between the stat
    that sized the header and the read that fills it. ``tarfile`` raises on the
    short read *after* writing the header, which leaves the stream misaligned --
    and every later member, including the manifest, becomes unreadable.

    So the size in the header is treated as a contract: short reads are padded
    with NULs and a file that grew is truncated, meaning the stream can never
    desynchronise. ``padded`` records whether that happened, so the capture can
    report the file as caught mid-change rather than pretend it is intact.
    """

    def __init__(self, stream: BinaryIO, size: int):
        self._stream = stream
        self._remaining = size
        self._digest = hashlib.sha256()
        self.padded = 0

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        wanted = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._stream.read(wanted)
        if len(data) < wanted:
            padding = b"\x00" * (wanted - len(data))
            self.padded += len(padding)
            data += padding
        self._remaining -= len(data)
        self._digest.update(data)
        return data

    def hexdigest(self) -> str:
        return self._digest.hexdigest()


@dataclass(slots=True)
class WriteResult:
    """What a finished bundle turned out to be."""

    path: Path
    ciphertext_sha256: str = ""
    ciphertext_size: int = 0
    payload_sha256: str = ""
    payload_size: int = 0
    chunk_count: int = 0
    file_digests: dict[str, str] = field(default_factory=dict)
    #: Files that shrank while being read; padded to keep the stream valid.
    changed_while_reading: list[str] = field(default_factory=list)


class BundleWriter:
    """Streams files into an encrypted bundle.

    Used as a context manager; the bundle is only valid once the block exits
    normally, because the terminating AEAD chunk is written on close.
    """

    def __init__(self, path: os.PathLike[str] | str, passphrase: str, header: dict):
        self.path = Path(os.fspath(path))
        self._passphrase = passphrase
        self._header = header
        self.result = WriteResult(path=self.path)
        self._file: BinaryIO | None = None
        self._tar: tarfile.TarFile | None = None
        self._gzip: gzip.GzipFile | None = None
        self._encryptor: crypto.ChunkedEncryptor | None = None
        self._payload_hasher: _HashingWriter | None = None
        self._outer_hasher: _HashingWriter | None = None

    def __enter__(self) -> "BundleWriter":
        params = crypto.kdf_params_from_json(self._header["kdf"])
        key = crypto.derive_key(self._passphrase, params)
        nonce_prefix = os.urandom(crypto.NONCE_PREFIX_SIZE)
        self._header["nonce_prefix_b64"] = _b64(nonce_prefix)

        header_bytes = json.dumps(self._header, ensure_ascii=False).encode("utf-8")
        header_digest = hashlib.sha256(header_bytes).digest()

        self._file = open(pathutil.extended(self.path), "wb")
        self._outer_hasher = _HashingWriter(self._file)
        self._outer_hasher.write(BUNDLE_MAGIC)
        self._outer_hasher.write(struct.pack(">I", len(header_bytes)))
        self._outer_hasher.write(header_bytes)

        self._encryptor = crypto.ChunkedEncryptor(
            self._outer_hasher, key, nonce_prefix, header_digest
        )
        self._payload_hasher = _HashingWriter(self._encryptor)
        # The header decides. "none" is not a degraded mode: on a profile that
        # is mostly media, gzip returns its input unchanged at a third of the
        # throughput, so skipping it is the difference between a capture bound
        # by the compressor and one bound by the disk.
        algorithm = self._header.get("compression", compression_mod.GZIP)
        if algorithm == compression_mod.NONE:
            self._gzip = None
            stream = self._payload_hasher
        else:
            level = int(self._header.get("compression_level", compression_mod.FAST_LEVEL))
            self._gzip = gzip.GzipFile(
                fileobj=self._payload_hasher, mode="wb", compresslevel=level, mtime=0
            )
            stream = self._gzip
        self._tar = tarfile.open(fileobj=stream, mode="w|")
        return self

    def add_file(self, source: os.PathLike[str] | str, archive_name: str) -> str:
        """Stream one file in, returning its SHA-256. Long-path safe."""
        source_path = Path(os.fspath(source))
        stat_result = os.stat(pathutil.extended(source_path))
        info = tarfile.TarInfo(name=archive_name)
        info.size = stat_result.st_size
        info.mtime = int(stat_result.st_mtime)
        with open(pathutil.extended(source_path), "rb") as handle:
            reader = _ExactSizeReader(handle, info.size)
            self._tar.addfile(info, reader)
        if reader.padded:
            # The file shrank while it was being read. The member is still the
            # declared length, so the bundle stays readable, but this file's
            # contents are not what was on disk when the scan sized it.
            log.warning("%s changed while being captured; %d byte(s) padded",
                        source_path, reader.padded)
            self.result.changed_while_reading.append(str(source_path))
        digest = reader.hexdigest()
        self.result.file_digests[archive_name] = digest
        return digest

    def add_bytes(self, archive_name: str, data: bytes) -> str:
        """Add an in-memory member, e.g. the manifest itself."""
        info = tarfile.TarInfo(name=archive_name)
        info.size = len(data)
        info.mtime = 0
        self._tar.addfile(info, io.BytesIO(data))
        digest = hashlib.sha256(data).hexdigest()
        self.result.file_digests[archive_name] = digest
        return digest

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._tar is not None:
            self._tar.close()
        if self._gzip is not None:
            self._gzip.close()
        if self._encryptor is not None:
            self._encryptor.close()
            self.result.chunk_count = self._encryptor.chunk_count
        if self._payload_hasher is not None:
            self.result.payload_sha256 = self._payload_hasher.hexdigest()
            self.result.payload_size = self._payload_hasher.bytes_written
        if self._outer_hasher is not None:
            self.result.ciphertext_sha256 = self._outer_hasher.hexdigest()
            self.result.ciphertext_size = self._outer_hasher.bytes_written
        if self._file is not None:
            self._file.close()
        if exc_type is not None and self.path.exists():
            # A half-written bundle is worse than none: it looks restorable.
            self.path.unlink(missing_ok=True)


def read_header(path: os.PathLike[str] | str) -> tuple[dict, bytes, BinaryIO]:
    """Open a bundle and read its plaintext header.

    Returns the header, its digest (the AEAD associated data), and the open
    stream positioned at the first chunk. No passphrase is needed.
    """
    handle = open(pathutil.extended(path), "rb")
    try:
        magic = handle.read(len(BUNDLE_MAGIC))
        if magic != BUNDLE_MAGIC:
            raise BundleFormatError(
                f"{path} is not a WinMigrate bundle (bad magic bytes)"
            )
        raw_length = handle.read(HEADER_LENGTH_SIZE)
        if len(raw_length) != HEADER_LENGTH_SIZE:
            raise BundleFormatError(f"{path} is truncated before its header")
        (length,) = struct.unpack(">I", raw_length)
        if length == 0 or length > MAX_HEADER_BYTES:
            raise BundleFormatError(f"{path} declares an implausible header size: {length}")
        header_bytes = handle.read(length)
        if len(header_bytes) != length:
            raise BundleFormatError(f"{path} is truncated inside its header")
        try:
            header = json.loads(header_bytes)
        except json.JSONDecodeError as exc:
            raise BundleFormatError(f"{path} has an unreadable header: {exc}") from exc
        return header, hashlib.sha256(header_bytes).digest(), handle
    except Exception:
        handle.close()
        raise


class BundleReader:
    """Streams the members of a bundle back out."""

    def __init__(self, path: os.PathLike[str] | str, passphrase: str):
        self.path = Path(os.fspath(path))
        self.header, self._header_digest, self._file = read_header(self.path)
        params = crypto.kdf_params_from_json(self.header["kdf"])
        key = crypto.derive_key(passphrase, params)
        nonce_prefix = _unb64(self.header["nonce_prefix_b64"])
        self._decryptor = crypto.ChunkedDecryptor(
            self._file, key, nonce_prefix, self._header_digest
        )
        # Bundles written before compression was a choice have no such field
        # and are gzip, so that is the default a missing value falls back to.
        if self.header.get("compression", compression_mod.GZIP) == compression_mod.NONE:
            self._gzip = None
            stream = self._decryptor
        else:
            self._gzip = gzip.GzipFile(fileobj=self._decryptor, mode="rb")
            stream = self._gzip
        self._tar = tarfile.open(fileobj=stream, mode="r|")

    def __enter__(self) -> "BundleReader":
        return self

    def members(self) -> Iterator[tuple[tarfile.TarInfo, BinaryIO | None]]:
        """Yield each member in stream order. The manifest comes last."""
        for info in self._tar:
            yield info, (self._tar.extractfile(info) if info.isfile() else None)

    def close(self) -> None:
        for closeable in (self._tar, self._gzip, self._file):
            if closeable is None:
                continue
            try:
                closeable.close()
            except Exception:  # noqa: BLE001 -- closing must not mask a real error
                pass

    def __exit__(self, *exc_info) -> None:
        self.close()


def verify_ciphertext(path: os.PathLike[str] | str, expected_sha256: str) -> None:
    """Check a bundle against the digest recorded in its sidecar manifest.

    This needs no passphrase, so a bundle can be checked for transfer damage
    before anyone is asked for one.
    """
    from .util.hashing import hash_file  # noqa: PLC0415

    actual = hash_file(path)
    if actual != expected_sha256:
        raise IntegrityError(
            f"{path} does not match its recorded digest: the bundle is damaged or "
            f"incomplete (expected {expected_sha256[:16]}…, got {actual[:16]}…)"
        )


def _b64(data: bytes) -> str:
    import base64  # noqa: PLC0415

    return base64.b64encode(data).decode("ascii")


def _unb64(text: str) -> bytes:
    import base64  # noqa: PLC0415

    return base64.b64decode(text)
