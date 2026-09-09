"""Chunked AEAD: the properties the bundle format depends on."""

from __future__ import annotations

import io
import os
import struct

import pytest

from winmigrate import crypto
from winmigrate.errors import IntegrityError

KEY = b"k" * 32
PREFIX = b"npfx"
HEADER_DIGEST = b"h" * 32


def build(payload: bytes, header_digest: bytes = HEADER_DIGEST) -> bytes:
    buffer = io.BytesIO()
    encryptor = crypto.ChunkedEncryptor(buffer, KEY, PREFIX, header_digest)
    encryptor.write(payload)
    encryptor.close()
    return buffer.getvalue()


def chunks_of(raw: bytes) -> list[bytes]:
    parts, offset = [], 0
    while offset < len(raw):
        (length,) = struct.unpack(">I", raw[offset : offset + 4])
        parts.append(raw[offset : offset + 4 + length])
        offset += 4 + length
    return parts


def decrypt(raw: bytes, key: bytes = KEY, header_digest: bytes = HEADER_DIGEST) -> bytes:
    return crypto.ChunkedDecryptor(io.BytesIO(raw), key, PREFIX, header_digest).read()


@pytest.mark.parametrize("size", [0, 1, 1024, crypto.CHUNK_SIZE, crypto.CHUNK_SIZE * 2 + 7])
def test_round_trip_at_and_across_chunk_boundaries(size):
    payload = os.urandom(size)
    assert decrypt(build(payload)) == payload


def test_a_payload_larger_than_one_chunk_is_split():
    raw = build(os.urandom(crypto.CHUNK_SIZE * 2 + 10))
    assert len(chunks_of(raw)) == 3  # two full chunks plus the terminator


def test_the_stream_always_ends_with_a_final_chunk_even_when_empty():
    assert len(chunks_of(build(b""))) == 1


def test_a_wrong_passphrase_is_rejected():
    with pytest.raises(crypto.DecryptionError):
        decrypt(build(b"secret"), key=b"x" * 32)


def test_truncation_before_the_final_chunk_is_detected():
    """The property a plain size check cannot give you."""
    raw = build(os.urandom(crypto.CHUNK_SIZE * 2))
    parts = chunks_of(raw)
    with pytest.raises(IntegrityError, match="truncated"):
        decrypt(b"".join(parts[:-1]))


def test_truncation_mid_chunk_is_detected():
    raw = build(os.urandom(crypto.CHUNK_SIZE * 2))
    with pytest.raises(IntegrityError):
        decrypt(raw[: len(raw) // 2])


def test_reordered_chunks_are_rejected():
    parts = chunks_of(build(os.urandom(crypto.CHUNK_SIZE * 2 + 5)))
    with pytest.raises(crypto.DecryptionError):
        decrypt(b"".join([parts[1], parts[0], parts[2]]))


def test_a_duplicated_chunk_is_rejected():
    parts = chunks_of(build(os.urandom(crypto.CHUNK_SIZE * 2 + 5)))
    with pytest.raises(crypto.DecryptionError):
        decrypt(b"".join([parts[0], parts[0], parts[1], parts[2]]))


def test_a_single_flipped_bit_is_rejected():
    raw = bytearray(build(os.urandom(2048)))
    raw[-20] ^= 0x01
    with pytest.raises(crypto.DecryptionError):
        decrypt(bytes(raw))


def test_a_chunk_from_another_bundle_cannot_be_spliced_in():
    """Chunks are bound to their header, so bundles cannot be interbred."""
    payload = os.urandom(crypto.CHUNK_SIZE * 2)
    mine = chunks_of(build(payload))
    theirs = chunks_of(build(payload, header_digest=b"o" * 32))
    with pytest.raises(crypto.DecryptionError):
        decrypt(b"".join([theirs[0], mine[1], mine[2]]))


def test_an_implausible_chunk_length_is_refused_without_allocating():
    raw = struct.pack(">I", 0xFFFFFFF) + b"\x00" * 16
    with pytest.raises(IntegrityError, match="implausible"):
        decrypt(raw)


def test_key_derivation_is_deterministic_and_salt_dependent():
    params = crypto.default_kdf_params(salt=b"s" * 16)
    other = crypto.default_kdf_params(salt=b"t" * 16)
    assert crypto.derive_key("pw", params) == crypto.derive_key("pw", params)
    assert crypto.derive_key("pw", params) != crypto.derive_key("pw", other)
    assert crypto.derive_key("pw", params) != crypto.derive_key("pw2", params)
    assert len(crypto.derive_key("pw", params)) == 32


def test_kdf_params_survive_a_json_round_trip():
    params = crypto.default_kdf_params()
    restored = crypto.kdf_params_from_json(crypto.kdf_params_to_json(params))
    assert restored.name == params.name
    assert restored.salt == params.salt
    assert crypto.derive_key("pw", restored) == crypto.derive_key("pw", params)


def test_pbkdf2_is_usable_as_the_fallback():
    params = crypto.KdfParams(name="pbkdf2-hmac-sha256", salt=b"s" * 16, iterations=1000)
    assert len(crypto.derive_key("pw", params)) == 32
