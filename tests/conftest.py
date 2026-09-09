"""Shared fixtures: a fake Windows profile tree that works on any OS."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from winmigrate.platform_win import USER_SHELL_FOLDERS_KEY, Environment

os.environ.setdefault("WINMIGRATE_ALLOW_NON_WINDOWS", "1")


def write(root: Path, relative: str, size: int = 100) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def profile(tmp_path: Path) -> Path:
    """A profile with real work, junk, regenerable output and a sync root."""
    root = tmp_path / "Users" / "alice"
    write(root, "Documents/report.docx", 5000)
    write(root, "Documents/notes.txt", 200)
    write(root, "Documents/proj/src/main.py", 300)
    write(root, "Documents/proj/node_modules/dep/index.js", 90_000)
    write(root, "Documents/~$report.docx", 10)
    write(root, "Documents/Thumbs.db", 7)
    write(root, "Desktop/shortcut.lnk", 50)
    write(root, "Downloads/installer.exe", 120_000)
    write(root, "Projects/app/main.rs", 400)
    write(root, "AppData/Local/Temp/junk.tmp", 999)
    write(root, "AppData/Roaming/App/settings.json", 42)
    write(root, "OneDrive/Documents/synced.docx", 7000)
    write(root, "OneDrive/Pictures/photo.jpg", 3000)
    (root / "Pictures").mkdir(parents=True, exist_ok=True)
    return root


@pytest.fixture
def registry(profile: Path) -> dict[str, dict[str, object]]:
    """Registry values a real machine would have for this profile."""
    return {
        f"HKCU\\{USER_SHELL_FOLDERS_KEY}": {
            "Desktop": "%USERPROFILE%\\Desktop",
            "Personal": "%USERPROFILE%\\Documents",
            "My Pictures": "%USERPROFILE%\\Pictures",
        },
        r"HKCU\Software\Microsoft\OneDrive\Accounts\Personal": {
            "UserFolder": "%USERPROFILE%\\OneDrive",
            "UserEmail": "alice@example.com",
        },
    }


@pytest.fixture
def env(profile: Path, registry: dict) -> Environment:
    return Environment.fixture(profile, registry)


def snapshot(root: Path) -> dict[str, int]:
    """Path -> size for every file under ``root``; used to prove nothing changed."""
    return {
        str(path.relative_to(root)): path.stat().st_size
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }
