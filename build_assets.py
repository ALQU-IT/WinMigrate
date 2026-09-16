"""Write the build's icon and version stamp, from the code that describes them.

Run before PyInstaller. Both outputs are generated rather than committed: an
icon is drawn in :mod:`winmigrate.gui.icon`, and the version stamp is the
version already in the package, so neither can drift from what it describes.

The version stamp is why right-clicking the .exe and choosing Properties shows
a name, a description and a version instead of nothing. On an unsigned download
that is not decoration -- it is the only thing distinguishing the file from
something anonymous.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from winmigrate import __version__  # noqa: E402
from winmigrate.gui.icon import ico_bytes  # noqa: E402

COMPANY = "ALQU-IT"
PRODUCT = "WinMigrate"
DESCRIPTION = "Windows user-profile migration and backup"


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """A four-number Windows version from a dotted one. Never raises."""
    parts: list[int] = []
    for piece in version.split("."):
        # Leading digits only, stopping at the first thing that is not one:
        # "0rc2" is release candidate 2 of version 0, not version 2, and
        # sweeping up every digit in the string turns 0.1.0rc2 into 0.1.2.
        digits = ""
        for character in piece:
            if not character.isdigit():
                break
            digits += character
        parts.append(int(digits) if digits else 0)
    parts += [0, 0, 0, 0]
    return tuple(parts[:4])  # type: ignore[return-value]


def version_info(version: str = __version__) -> str:
    """The VSVersionInfo literal PyInstaller's --version-file expects."""
    numbers = version_tuple(version)
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={numbers}, prodvers={numbers},
    mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
    date=(0, 0),
  ),
  kids=[
    StringFileInfo([
      StringTable(
        '040904B0',
        [StringStruct('CompanyName', {COMPANY!r}),
         StringStruct('FileDescription', {DESCRIPTION!r}),
         StringStruct('FileVersion', {version!r}),
         StringStruct('InternalName', {PRODUCT!r}),
         StringStruct('OriginalFilename', 'WinMigrate.exe'),
         StringStruct('ProductName', {PRODUCT!r}),
         StringStruct('ProductVersion', {version!r})])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


def main() -> int:
    Path("winmigrate.ico").write_bytes(ico_bytes())
    Path("version_info.txt").write_text(version_info(), encoding="utf-8")
    print(f"wrote winmigrate.ico and version_info.txt for {PRODUCT} {__version__}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
