"""Manifest construction, serialization and validation.

A WinMigrate bundle is three artefacts that travel together:

``<name>.dat``
    ``magic || header_len || header_json || ciphertext``. The header is
    plaintext because the KDF parameters and salt are needed *before* a key
    exists. It contains no content description and no payload digest -- only
    what is required to derive a key and authenticate the ciphertext.

``<name>.manifest.json`` (sidecar, plaintext)
    The *public view*: what the bundle is, how big, which categories it holds,
    and the ciphertext digest, so a bundle can be inspected and integrity
    checked without the passphrase. Secret items appear as redacted stubs --
    id, category, size and disposition, nothing more.

``manifest.json`` (inside the encrypted payload)
    The authoritative manifest: every item with its source path, archive path,
    per-item digest, restore spec and notes. This is the copy the restore stage
    trusts, and the only one that may describe secret material.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import socket
from dataclasses import dataclass, field
from typing import Any

from . import __version__
from .errors import ManifestError
from .models import Category, ScanResult, SourceMachine, utcnow

#: Bumped when a change would stop an older restore from reading a newer bundle.
SCHEMA_VERSION = "1.0"

#: Bundle container format version (magic + header framing).
BUNDLE_FORMAT_VERSION = 1

BUNDLE_MAGIC = b"WINMIGRATE\x00"

CIPHER = "AES-256-GCM"
DEFAULT_COMPRESSION = "gzip"

#: Argon2id parameters. Deliberately conservative so a bundle restores on a
#: modest target machine; recorded per-bundle so they can be raised later
#: without breaking existing bundles.
DEFAULT_KDF = {
    "name": "argon2id",
    "time_cost": 3,
    "memory_cost_kib": 262144,  # 256 MiB
    "parallelism": 4,
    "key_len": 32,
}

#: Used only where argon2-cffi is unavailable; recorded so restore knows.
FALLBACK_KDF = {
    "name": "pbkdf2-hmac-sha256",
    "iterations": 600_000,
    "key_len": 32,
}

MANIFEST_ARCHIVE_NAME = "manifest.json"


@dataclass(slots=True)
class BundleInfo:
    """Cryptographic and container facts about a written bundle."""

    filename: str
    cipher: str = CIPHER
    compression: str = DEFAULT_COMPRESSION
    kdf: dict[str, Any] = field(default_factory=lambda: dict(DEFAULT_KDF))
    salt: bytes = b""
    nonce: bytes = b""
    ciphertext_sha256: str | None = None
    ciphertext_size: int | None = None
    payload_sha256: str | None = None
    payload_size: int | None = None

    def header(self) -> dict[str, Any]:
        """The plaintext header written into the ``.dat`` file.

        Intentionally free of content description and of the payload digest:
        a plaintext digest of the plaintext payload would let a holder of the
        file confirm guesses about its contents without the passphrase.
        """
        kdf = dict(self.kdf)
        kdf["salt_b64"] = base64.b64encode(self.salt).decode("ascii")
        return {
            "format": BUNDLE_FORMAT_VERSION,
            "schema_version": SCHEMA_VERSION,
            "tool": {"name": "winmigrate", "version": __version__},
            "created_utc": utcnow(),
            "cipher": self.cipher,
            "nonce_b64": base64.b64encode(self.nonce).decode("ascii"),
            "kdf": kdf,
        }

    def to_json(self) -> dict[str, Any]:
        kdf = dict(self.kdf)
        kdf["salt_b64"] = base64.b64encode(self.salt).decode("ascii")
        return {
            "filename": self.filename,
            "cipher": self.cipher,
            "compression": self.compression,
            "kdf": kdf,
            "ciphertext": {
                "sha256": self.ciphertext_sha256,
                "size_bytes": self.ciphertext_size,
            },
            "payload": {
                "sha256": self.payload_sha256,
                "size_bytes": self.payload_size,
            },
        }


def detect_source_machine(profile_path: str | None = None, username: str | None = None) -> SourceMachine:
    """Describe the machine and account being captured."""
    uname = platform.uname()
    return SourceMachine(
        hostname=socket.gethostname(),
        username=(
            username
            or os.environ.get("USERNAME")
            or os.environ.get("USER")
            or (os.path.basename(profile_path.rstrip("/\\")) if profile_path else "")
        ),
        profile_path=profile_path or os.environ.get("USERPROFILE") or os.path.expanduser("~"),
        os_name=uname.system,
        os_version=platform.win32_ver()[0] if os.name == "nt" else uname.release,
        os_build=platform.win32_ver()[1] if os.name == "nt" else uname.version,
        architecture=uname.machine,
        locale=os.environ.get("LANG", ""),
        is_windows=os.name == "nt",
    )


def build(scan: ScanResult, bundle: BundleInfo | None = None) -> dict[str, Any]:
    """Build the full (encrypted-copy) manifest from a scan result."""
    totals = scan.totals()
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "tool": {"name": "winmigrate", "version": __version__},
        "created_utc": utcnow(),
        "mode": "files-only" if scan.files_only else "full",
        "source": scan.source.to_json(),
        "scan": {
            "started_utc": scan.started_utc,
            "finished_utc": scan.finished_utc,
            "duration_seconds": round(scan.duration_seconds, 3),
            "notes": [note.to_json() for note in scan.notes],
        },
        "sync_roots": [root.to_json() for root in scan.sync_roots],
        "items": [item.to_json() for item in scan.items],
        "followups": [followup.to_json() for followup in scan.followups],
        "totals": totals.to_json(),
    }
    if bundle is not None:
        manifest["bundle"] = bundle.to_json()
    return manifest


def public_view(manifest: dict[str, Any]) -> dict[str, Any]:
    """Reduce a full manifest to what may be written in plaintext.

    Drops per-item detail for secret items, and drops the plaintext payload
    digest (see :meth:`BundleInfo.header`). Everything retained here is
    information the owner already has by virtue of holding the file.
    """
    public = {
        key: value
        for key, value in manifest.items()
        if key not in {"items", "bundle", "followups"}
    }
    public["items"] = [_redact_item(item) for item in manifest.get("items", [])]
    public["followups"] = [
        {key: value for key, value in followup.items() if key != "steps"}
        for followup in manifest.get("followups", [])
    ]
    bundle = manifest.get("bundle")
    if bundle:
        public["bundle"] = {
            "filename": bundle.get("filename"),
            "cipher": bundle.get("cipher"),
            "compression": bundle.get("compression"),
            "kdf": bundle.get("kdf"),
            "ciphertext": bundle.get("ciphertext"),
        }
    public["categories"] = sorted({item["category"] for item in manifest.get("items", [])})
    public["_note"] = (
        "Public sidecar manifest. Secret items are listed as redacted stubs; "
        "their paths and contents exist only inside the encrypted bundle."
    )
    return public


def _redact_item(item: dict[str, Any]) -> dict[str, Any]:
    if item.get("sensitivity") != "secret":
        return item
    keep = {"id", "category", "kind", "title", "action", "sensitivity", "size_bytes", "file_count"}
    redacted = {key: value for key, value in item.items() if key in keep}
    redacted["redacted"] = True
    return redacted


def dumps(manifest: dict[str, Any], *, indent: int = 2) -> str:
    return json.dumps(manifest, indent=indent, ensure_ascii=False, sort_keys=False) + "\n"


REQUIRED_TOP_LEVEL = ("schema_version", "tool", "created_utc", "source", "items", "totals")


def validate(manifest: dict[str, Any]) -> None:
    """Structural validation. Raises :class:`ManifestError` on the first problem.

    This is what restore runs before trusting a manifest; it is deliberately
    strict about the schema version because a newer bundle may describe items
    an older restore would silently drop.
    """
    if not isinstance(manifest, dict):
        raise ManifestError("manifest is not a JSON object")
    for key in REQUIRED_TOP_LEVEL:
        if key not in manifest:
            raise ManifestError(f"manifest is missing required key: {key}")
    version = str(manifest["schema_version"])
    major = version.split(".", 1)[0]
    if major != SCHEMA_VERSION.split(".", 1)[0]:
        raise ManifestError(
            f"manifest schema version {version} is not compatible with "
            f"this build's {SCHEMA_VERSION}"
        )
    if not isinstance(manifest["items"], list):
        raise ManifestError("manifest 'items' must be a list")
    seen: set[str] = set()
    valid_categories = {category.value for category in Category}
    for index, item in enumerate(manifest["items"]):
        if not isinstance(item, dict):
            raise ManifestError(f"item #{index} is not an object")
        for key in ("id", "category", "kind", "action", "sensitivity"):
            if key not in item:
                raise ManifestError(f"item #{index} is missing required key: {key}")
        if item["id"] in seen:
            raise ManifestError(f"duplicate item id: {item['id']}")
        seen.add(item["id"])
        if item["category"] not in valid_categories:
            raise ManifestError(f"item {item['id']} has unknown category {item['category']!r}")


def item_by_id(manifest: dict[str, Any], item_id: str) -> dict[str, Any]:
    for item in manifest.get("items", []):
        if item.get("id") == item_id:
            return item
    raise ManifestError(f"no such item in manifest: {item_id}")
