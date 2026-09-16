"""Saved Windows sign-ins, moved the way Windows itself moves them."""

from __future__ import annotations

from pathlib import Path

import pytest

from winmigrate import credentials
from winmigrate.models import Category, Kind, RestoreStrategy, ScanResult, Sensitivity
from winmigrate.platform_win import Environment

CMDKEY_OUTPUT = """
Currently stored credentials:

    Target: Domain:target=fileserver
    Type: Domain Password
    User: CONTOSO\\maria

    Target: LegacyGeneric:target=git:https://github.com
    Type: Generic
    User: maria@example.com

    Target: WindowsLive:target=virtualapp/didlogical
    Type: Generic
    User: 02abcdef
"""


def test_the_listing_reads_names_and_never_secrets():
    """cmdkey prints targets, types and user names. It does not print passwords
    and there is no switch that makes it -- which is exactly why it is the
    command used here."""
    entries = credentials.parse_cmdkey(CMDKEY_OUTPUT)

    assert [entry["target"] for entry in entries] == [
        "Domain:target=fileserver",
        "LegacyGeneric:target=git:https://github.com",
        "WindowsLive:target=virtualapp/didlogical",
    ]
    assert entries[0]["user"] == "CONTOSO\\maria"
    assert entries[1]["type"] == "Generic"


def test_parsing_survives_empty_and_unexpected_output():
    assert credentials.parse_cmdkey("") == []
    assert credentials.parse_cmdkey("Currently stored credentials:\n\n* NONE *\n") == []


def test_nothing_reads_the_credential_store_itself():
    """Reading it directly means CredEnumerate and CryptUnprotectData, which
    turns somebody's saved passwords into plaintext. That is a credential
    dumper however politely it is described, and it is the boundary this whole
    module exists to respect."""
    import ast

    source = Path("winmigrate/credentials.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    # Everything but the module docstring, which is where the boundary is
    # explained and so is the one place these names are allowed to appear.
    body = ast.unparse(ast.Module(body=tree.body[1:], type_ignores=[]))

    for forbidden in ("CredEnumerate", "CredRead", "CryptUnprotectData", "credui"):
        assert forbidden not in body, forbidden


def test_what_is_saved_is_detected_and_handed_to_windows(tmp_path: Path, monkeypatch):
    env = Environment.fixture(tmp_path, {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(
        credentials, "list_credentials",
        lambda: (credentials.parse_cmdkey(CMDKEY_OUTPUT), None),
    )

    (item,), (followup,) = credentials.scan_credentials(env)

    assert item.category is Category.CREDENTIALS
    assert item.restore.strategy is RestoreStrategy.GUIDED
    # No passwords in it, but it names the servers and accounts somebody signs
    # into, so it rides in the payload rather than beside the bundle.
    assert item.sensitivity is Sensitivity.SECRET
    assert item.record_public is False
    assert "keymgr.dll" in " ".join(followup.steps)


def test_files_only_says_nothing_at_all(tmp_path: Path, monkeypatch):
    """The list holds no passwords, but it is a map of where somebody has
    accounts, and files-only promises no credential material travels."""
    env = Environment.fixture(tmp_path, {})
    monkeypatch.setattr(env, "is_windows", True)
    monkeypatch.setattr(credentials, "list_credentials", lambda: ([{"target": "x"}], None))

    assert credentials.scan_credentials(env, files_only=True) == ([], [])


# --- the file Windows writes ------------------------------------------------
def test_only_the_file_credential_manager_writes_is_accepted(tmp_path: Path):
    """Checked by name and size and nothing else. Its contents are Windows' own
    encrypted format and this tool does not open them -- that is the handoff."""
    good = tmp_path / "sign-ins.crd"
    good.write_bytes(b"\x01\x02encrypted")
    assert credentials.looks_like_backup(good) == (True, "")

    wrong = tmp_path / "sign-ins.txt"
    wrong.write_bytes(b"hello")
    assert credentials.looks_like_backup(wrong)[0] is False

    empty = tmp_path / "empty.crd"
    empty.write_bytes(b"")
    assert credentials.looks_like_backup(empty)[0] is False


def test_a_backup_rides_encrypted_only_and_swaps_the_instruction(tmp_path: Path):
    backup = tmp_path / "sign-ins.crd"
    backup.write_bytes(b"\x01\x02encrypted")
    scan = ScanResult(source=None)
    scan.followups.append(credentials.export_followup([{"target": "x"}]))

    item = credentials.ingest_backup(backup, scan)

    assert item.kind is Kind.FILE
    assert item.sensitivity is Sensitivity.SECRET
    assert item.archive_path == "secrets/WinMigrate-Credentials/sign-ins.crd"
    # The "please export" instruction is replaced by the "here is how to put it
    # back" one, rather than both being shown.
    assert [f.id for f in scan.followups] == ["credentials:import"]
    assert "Restore..." in " ".join(scan.followups[0].steps)


def test_a_file_that_is_not_a_backup_is_refused_rather_than_carried(tmp_path: Path):
    wrong = tmp_path / "notes.txt"
    wrong.write_bytes(b"hello")

    with pytest.raises(ValueError):
        credentials.ingest_backup(wrong, ScanResult(source=None))
