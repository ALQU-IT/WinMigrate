import json
from pathlib import Path

import pytest

from winmigrate import manifest as manifest_mod
from winmigrate.config import ScanConfig
from winmigrate.errors import ManifestError
from winmigrate.models import (
    Category,
    Item,
    Kind,
    ScanResult,
    Sensitivity,
    SourceMachine,
)
from winmigrate.platform_win import Environment
from winmigrate.scan import run_scan


def secret_scan() -> ScanResult:
    result = ScanResult(source=SourceMachine(hostname="pc", username="alice"))
    result.items.append(
        Item(
            id="dev:ssh",
            category=Category.DEV_CONFIG,
            kind=Kind.TREE,
            title="SSH keys",
            source_path=r"C:\Users\alice\.ssh",
            archive_path="secrets/ssh",
            sensitivity=Sensitivity.SECRET,
            size_bytes=4096,
            file_count=3,
        )
    )
    result.items.append(
        Item(
            id="files:documents",
            category=Category.USER_FILES,
            kind=Kind.TREE,
            title="Documents",
            source_path=r"C:\Users\alice\Documents",
            archive_path="data/user_files/documents",
            size_bytes=100,
            file_count=1,
        )
    )
    return result


def test_a_valid_manifest_validates(profile: Path, env: Environment):
    result = run_scan(ScanConfig(profile_root=profile), env)
    manifest_mod.validate(manifest_mod.build(result))


def test_the_public_view_redacts_secret_items_entirely():
    manifest = manifest_mod.build(secret_scan())
    public = manifest_mod.public_view(manifest)

    secret = next(entry for entry in public["items"] if entry["id"] == "dev:ssh")
    assert secret["redacted"] is True
    assert "source_path" not in secret and "archive_path" not in secret
    assert secret["size_bytes"] == 4096  # size is not sensitive; the path is

    normal = next(entry for entry in public["items"] if entry["id"] == "files:documents")
    assert normal["source_path"] == r"C:\Users\alice\Documents"

    assert ".ssh" not in json.dumps(public)


def test_the_public_view_keeps_no_plaintext_payload_digest():
    bundle = manifest_mod.BundleInfo(
        filename="alice.dat",
        salt=b"0" * 16,
        nonce=b"1" * 12,
        payload_sha256="dead" * 16,
        ciphertext_sha256="beef" * 16,
    )
    public = manifest_mod.public_view(manifest_mod.build(secret_scan(), bundle))
    assert public["bundle"]["ciphertext"]["sha256"] == "beef" * 16
    assert "payload" not in public["bundle"]


def test_the_bundle_header_carries_kdf_parameters_but_no_content_description():
    bundle = manifest_mod.BundleInfo(
        filename="alice.dat",
        salt=b"s" * 16,
        nonce=b"n" * 12,
        payload_sha256="dead" * 16,
    )
    header = bundle.header()
    assert header["cipher"] == "AES-256-GCM"
    assert header["kdf"]["name"] == "argon2id"
    assert header["kdf"]["salt_b64"]
    assert "payload" not in header and "items" not in header
    assert "dead" not in json.dumps(header)


def test_validate_rejects_a_future_schema_version():
    manifest = manifest_mod.build(secret_scan())
    manifest["schema_version"] = "2.0"
    with pytest.raises(ManifestError, match="not compatible"):
        manifest_mod.validate(manifest)


def test_validate_rejects_duplicate_item_ids():
    manifest = manifest_mod.build(secret_scan())
    manifest["items"].append(dict(manifest["items"][0]))
    with pytest.raises(ManifestError, match="duplicate item id"):
        manifest_mod.validate(manifest)


def test_validate_rejects_an_unknown_category():
    manifest = manifest_mod.build(secret_scan())
    manifest["items"][0]["category"] = "nonsense"
    with pytest.raises(ManifestError, match="unknown category"):
        manifest_mod.validate(manifest)


def test_validate_requires_the_top_level_keys():
    manifest = manifest_mod.build(secret_scan())
    del manifest["totals"]
    with pytest.raises(ManifestError, match="missing required key: totals"):
        manifest_mod.validate(manifest)


def test_item_lookup_by_id():
    manifest = manifest_mod.build(secret_scan())
    assert manifest_mod.item_by_id(manifest, "dev:ssh")["title"] == "SSH keys"
    with pytest.raises(ManifestError):
        manifest_mod.item_by_id(manifest, "absent")


def test_totals_count_secret_items():
    assert secret_scan().totals().secret_item_count == 1
