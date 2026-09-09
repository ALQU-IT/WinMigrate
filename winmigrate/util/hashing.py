"""Content hashing used for manifest integrity records.

Two digest shapes are recorded in a manifest:

``sha256``
    Plain SHA-256 of a byte stream (a single file, the compressed payload, or
    the finished ciphertext).

``sha256-tree-v1``
    A deterministic digest over a directory tree. It is SHA-256 of the
    concatenation of ``"<sha256hex>  <relative/posix/path>\\n"`` lines, sorted by
    relative path. Sorting and the ``/`` separator make the digest identical
    whichever machine, filesystem order, or OS produced the tree.
"""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterable, Iterator
from pathlib import Path

from . import paths as pathutil

CHUNK_SIZE = 1024 * 1024

TREE_DIGEST_ALGO = "sha256-tree-v1"
FILE_DIGEST_ALGO = "sha256"


def hash_stream(stream, chunk_size: int = CHUNK_SIZE) -> str:
    """SHA-256 of a binary file-like object, read incrementally."""
    digest = hashlib.sha256()
    while True:
        chunk = stream.read(chunk_size)
        if not chunk:
            break
        digest.update(chunk)
    return digest.hexdigest()


def hash_file(path: os.PathLike[str] | str, chunk_size: int = CHUNK_SIZE) -> str:
    """SHA-256 of a file's contents, long-path safe."""
    with open(pathutil.extended(path), "rb") as handle:
        return hash_stream(handle, chunk_size)


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_digest(entries: Iterable[tuple[str, str]]) -> str:
    """Combine ``(relative_posix_path, sha256hex)`` pairs into a tree digest."""
    digest = hashlib.sha256()
    for rel, file_hash in sorted(entries, key=lambda item: item[0]):
        digest.update(f"{file_hash}  {rel}\n".encode())
    return digest.hexdigest()


def hash_tree(root: os.PathLike[str] | str) -> tuple[str, list[tuple[str, str]]]:
    """Hash every regular file under ``root``; return the tree digest and pairs."""
    entries = [(rel, hash_file(path)) for rel, path in iter_tree_files(root)]
    return tree_digest(entries), entries


def iter_tree_files(root: os.PathLike[str] | str) -> Iterator[tuple[str, Path]]:
    """Yield ``(relative_posix_path, absolute_path)`` for files under ``root``."""
    root_path = Path(os.fspath(root))
    for dirpath, dirnames, filenames in os.walk(pathutil.extended(root_path)):
        dirnames.sort()
        for name in sorted(filenames):
            absolute = Path(dirpath) / name
            yield pathutil.relative_posix(absolute, root_path), absolute
