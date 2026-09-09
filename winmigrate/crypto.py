"""Key derivation and authenticated encryption for the bundle payload.

Why the payload is encrypted in chunks rather than in one call:

* AES-GCM is only safe for roughly 64 GiB under a single (key, nonce) pair, and
  a single ``encrypt()`` cannot exceed 2^39-256 bits at all. A real profile can
  exceed that on its own -- the machine this was first run against had 217 GiB
  to capture -- so a one-shot encrypt is not merely slow, it is invalid.
* A one-shot encrypt would also require the whole payload in memory.

So the payload is a sequence of independently authenticated chunks. Each chunk's
associated data binds it to this bundle's header, to its position in the stream,
and to whether it is the final chunk, which makes reordering, splicing between
bundles and truncation all detectable rather than silently tolerated.

Nonces are ``prefix(4) || counter(8)``: the prefix is random per bundle, the
counter increments per chunk, so a nonce is never reused under one key.
"""

from __future__ import annotations

import hashlib
import os
import struct
from dataclasses import dataclass
from typing import BinaryIO

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .errors import IntegrityError, WinMigrateError

#: Plaintext bytes per chunk. Small enough to bound memory, large enough that
#: the 16-byte tag and 4-byte length are negligible overhead.
CHUNK_SIZE = 4 * 1024 * 1024

TAG_SIZE = 16
NONCE_SIZE = 12
NONCE_PREFIX_SIZE = 4
KEY_SIZE = 32
SALT_SIZE = 16

#: Domain separator, so a chunk can never be replayed into another protocol.
AAD_MAGIC = b"winmigrate-payload-v1"

#: Refuse absurd frames rather than trying to allocate them.
MAX_CHUNK_BYTES = 64 * 1024 * 1024


class DecryptionError(WinMigrateError):
    """The passphrase is wrong, or the bundle has been altered."""


@dataclass(slots=True)
class KdfParams:
    """Everything needed to re-derive the key, recorded in the bundle header."""

    name: str
    salt: bytes
    key_len: int = KEY_SIZE
    time_cost: int | None = None
    memory_cost_kib: int | None = None
    parallelism: int | None = None
    iterations: int | None = None


def argon2_available() -> bool:
    try:
        import argon2.low_level  # noqa: PLC0415

        return hasattr(argon2.low_level, "hash_secret_raw")
    except ImportError:
        return False


def default_kdf_params(salt: bytes | None = None) -> KdfParams:
    """Argon2id where available, high-iteration PBKDF2 otherwise."""
    salt = salt if salt is not None else os.urandom(SALT_SIZE)
    if argon2_available():
        from .manifest import DEFAULT_KDF  # noqa: PLC0415 -- avoid import cycle

        return KdfParams(
            name="argon2id",
            salt=salt,
            key_len=DEFAULT_KDF["key_len"],
            time_cost=DEFAULT_KDF["time_cost"],
            memory_cost_kib=DEFAULT_KDF["memory_cost_kib"],
            parallelism=DEFAULT_KDF["parallelism"],
        )
    from .manifest import FALLBACK_KDF  # noqa: PLC0415

    return KdfParams(
        name="pbkdf2-hmac-sha256",
        salt=salt,
        key_len=FALLBACK_KDF["key_len"],
        iterations=FALLBACK_KDF["iterations"],
    )


def derive_key(passphrase: str, params: KdfParams) -> bytes:
    """Derive the payload key. The passphrase itself is never stored anywhere."""
    secret = passphrase.encode("utf-8")
    try:
        if params.name == "argon2id":
            from argon2.low_level import Type, hash_secret_raw  # noqa: PLC0415

            return hash_secret_raw(
                secret=secret,
                salt=params.salt,
                time_cost=params.time_cost,
                memory_cost=params.memory_cost_kib,
                parallelism=params.parallelism,
                hash_len=params.key_len,
                type=Type.ID,
            )
        if params.name == "pbkdf2-hmac-sha256":
            return hashlib.pbkdf2_hmac(
                "sha256", secret, params.salt, params.iterations, dklen=params.key_len
            )
    finally:
        # Best effort: CPython strings are immutable, so this only clears our
        # local reference. Documented rather than pretended about.
        del secret
    raise WinMigrateError(f"unsupported key derivation function: {params.name}")


def kdf_params_to_json(params: KdfParams) -> dict:
    import base64  # noqa: PLC0415

    data = {"name": params.name, "key_len": params.key_len,
            "salt_b64": base64.b64encode(params.salt).decode("ascii")}
    for field in ("time_cost", "memory_cost_kib", "parallelism", "iterations"):
        value = getattr(params, field)
        if value is not None:
            data[field] = value
    return data


def kdf_params_from_json(data: dict) -> KdfParams:
    import base64  # noqa: PLC0415

    return KdfParams(
        name=data["name"],
        salt=base64.b64decode(data["salt_b64"]),
        key_len=data.get("key_len", KEY_SIZE),
        time_cost=data.get("time_cost"),
        memory_cost_kib=data.get("memory_cost_kib"),
        parallelism=data.get("parallelism"),
        iterations=data.get("iterations"),
    )


def _aad(header_digest: bytes, index: int, final: bool) -> bytes:
    return AAD_MAGIC + header_digest + struct.pack(">Q?", index, final)


def _nonce(prefix: bytes, index: int) -> bytes:
    return prefix + struct.pack(">Q", index)


class ChunkedEncryptor:
    """Buffers plaintext and writes authenticated chunks to a stream.

    Used as a binary file-like object so gzip/tar can write straight through it.
    """

    def __init__(self, stream: BinaryIO, key: bytes, nonce_prefix: bytes, header_digest: bytes):
        self._stream = stream
        self._aead = AESGCM(key)
        self._nonce_prefix = nonce_prefix
        self._header_digest = header_digest
        self._buffer = bytearray()
        self._index = 0
        self._closed = False
        self.plaintext_bytes = 0
        self.ciphertext_bytes = 0

    def write(self, data: bytes) -> int:
        if self._closed:
            raise ValueError("write to a closed encryptor")
        self._buffer.extend(data)
        self.plaintext_bytes += len(data)
        while len(self._buffer) >= CHUNK_SIZE:
            self._emit(bytes(self._buffer[:CHUNK_SIZE]), final=False)
            del self._buffer[:CHUNK_SIZE]
        return len(data)

    def _emit(self, plaintext: bytes, *, final: bool) -> None:
        ciphertext = self._aead.encrypt(
            _nonce(self._nonce_prefix, self._index),
            plaintext,
            _aad(self._header_digest, self._index, final),
        )
        self._stream.write(struct.pack(">I", len(ciphertext)))
        self._stream.write(ciphertext)
        self.ciphertext_bytes += 4 + len(ciphertext)
        self._index += 1

    def close(self) -> None:
        """Flush the remainder and write the terminating chunk.

        The final chunk is always written, even when empty, because it is what
        marks the end of the stream: a truncated bundle simply never presents
        one, and decryption fails instead of returning a short payload.
        """
        if self._closed:
            return
        self._emit(bytes(self._buffer), final=True)
        self._buffer.clear()
        self._closed = True

    def flush(self) -> None:  # tarfile/gzip call this
        return

    @property
    def chunk_count(self) -> int:
        return self._index

    def __enter__(self) -> "ChunkedEncryptor":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


class ChunkedDecryptor:
    """Reads authenticated chunks back into a plaintext stream."""

    def __init__(self, stream: BinaryIO, key: bytes, nonce_prefix: bytes, header_digest: bytes):
        self._stream = stream
        self._aead = AESGCM(key)
        self._nonce_prefix = nonce_prefix
        self._header_digest = header_digest
        self._buffer = bytearray()
        self._index = 0
        self._finished = False
        self.plaintext_bytes = 0

    def read(self, size: int = -1) -> bytes:
        while (size < 0 or len(self._buffer) < size) and not self._finished:
            self._fill()
        if size < 0 or size >= len(self._buffer):
            data = bytes(self._buffer)
            self._buffer.clear()
        else:
            data = bytes(self._buffer[:size])
            del self._buffer[:size]
        return data

    def _fill(self) -> None:
        length_bytes = self._read_exactly(4)
        if not length_bytes:
            # Ran out of chunks without ever seeing the final one.
            raise IntegrityError(
                "bundle payload is truncated: the stream ends before its final chunk"
            )
        (length,) = struct.unpack(">I", length_bytes)
        if length < TAG_SIZE or length > MAX_CHUNK_BYTES + TAG_SIZE:
            raise IntegrityError(f"bundle payload declares an implausible chunk size: {length}")
        ciphertext = self._read_exactly(length)
        if len(ciphertext) != length:
            raise IntegrityError("bundle payload is truncated mid-chunk")

        nonce = _nonce(self._nonce_prefix, self._index)
        for final in (False, True):
            try:
                plaintext = self._aead.decrypt(
                    nonce, ciphertext, _aad(self._header_digest, self._index, final)
                )
            except InvalidTag:
                continue
            self._buffer.extend(plaintext)
            self.plaintext_bytes += len(plaintext)
            self._index += 1
            self._finished = final
            return
        raise DecryptionError(
            "could not authenticate the bundle payload: the passphrase is wrong, "
            "or the file has been altered or corrupted"
        )

    def _read_exactly(self, size: int) -> bytes:
        data = bytearray()
        while len(data) < size:
            chunk = self._stream.read(size - len(data))
            if not chunk:
                break
            data.extend(chunk)
        return bytes(data)
