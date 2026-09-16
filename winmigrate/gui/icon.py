"""WinMigrate's own icon, drawn in code.

A window without an icon shows Tk's feather, in the title bar and again in the
taskbar, and nothing else says "this was thrown together" as quickly. It is the
first thing anybody sees and the last thing anybody thinks to add.

It is drawn here rather than committed as a file for two reasons. A binary blob
in a repository is a thing nobody can review in a diff -- and every size can be
rendered from the same description, so the 16-pixel one in the title bar is
actually drawn at 16 pixels rather than being a photograph of a bigger one
shrunk by whoever needs it.

The mark is a chevron moving right, inside a rounded square: things going from
one place to the next. Two shapes, because an icon has to read at sixteen
pixels across, where anything finer becomes grey mush. Drawn at four times the
final size and averaged down, which is what gives it smooth edges without a
drawing library.

No dependencies: a PNG is a handful of chunks and a zlib stream, and an ICO is
a header in front of one.
"""

from __future__ import annotations

import struct
import zlib

#: The blue the window uses for its own accents, so the icon belongs to it.
BRAND = (43, 108, 176)
MARK = (255, 255, 255)

#: Drawn this many times over and averaged down. Four is the point where the
#: edges stop stepping and more stops being visible.
SUPERSAMPLE = 4


def _rounded_square(x: float, y: float, size: float) -> bool:
    """Is this point inside the rounded square? Coordinates are 0..1."""
    radius = 0.26
    inner = size - 2 * radius
    dx = abs(x - size / 2) - inner / 2
    dy = abs(y - size / 2) - inner / 2
    if dx <= 0 or dy <= 0:
        return max(dx, dy) <= radius
    return (dx * dx + dy * dy) <= radius * radius


def _chevron(x: float, y: float) -> bool:
    """A thick > pointing right, centred, in 0..1 coordinates.

    Two diagonal bands meeting at a point, which is the same shape "next" has
    on every media player anybody has ever used.
    """
    cx, cy = 0.66, 0.5
    thickness = 0.15
    reach = 0.22
    if not (cx - reach - thickness <= x <= cx + thickness / 2):
        return False
    if abs(y - cy) > reach + thickness / 2:
        return False
    # Distance from the pair of 45-degree lines that meet at (cx, cy).
    distance = abs(abs(y - cy) + (x - cx)) / (2 ** 0.5)
    return distance <= thickness / 2


def _pixel(x: float, y: float) -> tuple[int, int, int, int]:
    if not _rounded_square(x, y, 1.0):
        return (0, 0, 0, 0)
    if _chevron(x, y):
        return (*MARK, 255)
    return (*BRAND, 255)


def rgba(size: int) -> bytes:
    """The icon as raw RGBA rows, ``size`` pixels square."""
    rows = bytearray()
    step = 1.0 / (size * SUPERSAMPLE)
    for row in range(size):
        for column in range(size):
            red = green = blue = alpha = 0
            for sub_y in range(SUPERSAMPLE):
                for sub_x in range(SUPERSAMPLE):
                    x = (column * SUPERSAMPLE + sub_x + 0.5) * step
                    y = (row * SUPERSAMPLE + sub_y + 0.5) * step
                    sample = _pixel(x, y)
                    # Weighted by alpha, or the transparent edge pixels drag
                    # the colour towards black and the icon gets a dark halo.
                    red += sample[0] * sample[3]
                    green += sample[1] * sample[3]
                    blue += sample[2] * sample[3]
                    alpha += sample[3]
            samples = SUPERSAMPLE * SUPERSAMPLE
            if alpha:
                rows += bytes((red // alpha, green // alpha, blue // alpha,
                               alpha // samples))
            else:
                rows += b"\x00\x00\x00\x00"
    return bytes(rows)


def png_bytes(size: int = 64) -> bytes:
    """The icon as a PNG. Tk 8.6 reads these, which is what the window needs."""
    raw = rgba(size)
    stride = size * 4
    # Each scanline is prefixed with its filter type; 0 means "stored as is",
    # which costs a few bytes and saves needing a filter implementation.
    scanlines = b"".join(
        b"\x00" + raw[row * stride:(row + 1) * stride] for row in range(size)
    )
    return b"\x89PNG\r\n\x1a\n" + b"".join(
        _chunk(kind, payload)
        for kind, payload in (
            (b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)),
            (b"IDAT", zlib.compress(scanlines, 9)),
            (b"IEND", b""),
        )
    )


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(">I", zlib.crc32(body))


#: What Windows wants in an .ico, smallest first: the title bar takes 16, the
#: taskbar 32, and the installer and file listings the rest.
ICO_SIZES = (16, 24, 32, 48, 64, 128, 256)


def ico_bytes(sizes=ICO_SIZES) -> bytes:
    """The icon as a Windows .ico, for the frozen build's executable.

    Every entry is a PNG, which Windows has accepted inside an .ico since
    Vista and which keeps this to one renderer rather than two.
    """
    images = [png_bytes(size) for size in sizes]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = len(header) + 16 * len(images)
    directory = b""
    for size, image in zip(sizes, images):
        directory += struct.pack(
            "<BBBBHHII",
            0 if size >= 256 else size,   # 0 means 256
            0 if size >= 256 else size,
            0,                            # colours in the palette: none, it is true colour
            0,                            # reserved
            1,                            # colour planes
            32,                           # bits per pixel
            len(image),
            offset,
        )
        offset += len(image)
    return header + directory + b"".join(images)
