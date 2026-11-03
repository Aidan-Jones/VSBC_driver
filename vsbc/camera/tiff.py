"""Minimal 8-bit greyscale TIFF files, NumPy only.

The camera process writes every frame with `write_gray8`: uncompressed, one
strip, readable by OpenCV, Pillow, matplotlib and pyvale. Compression would
cost CPU for little gain (a speckle pattern hardly compresses) while the Pi is
writing up to 80 frames a second. `read` reads those files directly and
anything else through Pillow.
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

_TYPES = {3: "H", 4: "I"}       # SHORT, LONG


def write_gray8(path: Path, image: np.ndarray) -> None:
    data = np.ascontiguousarray(image, dtype=np.uint8)
    if data.ndim != 2:
        raise ValueError(f"expected a 2-D greyscale image, got shape {data.shape}")
    height, width = data.shape
    body = data.tobytes()
    entries = [(256, 4, width), (257, 4, height), (258, 3, 8), (259, 3, 1), (262, 3, 1),
               (273, 4, 0), (277, 3, 1), (278, 4, height), (279, 4, len(body))]
    offset = 8 + 2 + 12 * len(entries) + 4
    ifd = struct.pack("<H", len(entries))
    for tag, kind, value in entries:
        # A SHORT sits in the first two bytes of the value field: little-endian "<I" puts it there.
        ifd += struct.pack("<HHII", tag, kind, 1, offset if tag == 273 else value)
    ifd += struct.pack("<I", 0)
    with open(path, "wb") as f:
        f.write(b"II*\x00" + struct.pack("<I", 8) + ifd + body)


def _read_plain(path: Path) -> np.ndarray | None:
    """A single-strip, uncompressed, 8-bit little-endian TIFF (what write_gray8 makes); else None."""
    with open(path, "rb") as f:
        head = f.read(8)
        if head[:4] != b"II*\x00":
            return None
        f.seek(struct.unpack("<I", head[4:])[0])
        (count,) = struct.unpack("<H", f.read(2))
        tags = {}
        for _ in range(count):
            tag, kind, n, raw = struct.unpack("<HHI4s", f.read(12))
            if n == 1 and kind in _TYPES:
                tags[tag] = struct.unpack("<" + _TYPES[kind], raw[:struct.calcsize(_TYPES[kind])])[0]
        if tags.get(259, 1) != 1 or tags.get(258, 8) != 8 or tags.get(277, 1) != 1 or 273 not in tags:
            return None
        width, height = tags[256], tags[257]
        f.seek(tags[273])
        body = f.read(width * height)
    if len(body) != width * height:
        return None
    return np.frombuffer(body, dtype=np.uint8).reshape(height, width)


def read(path: Path) -> np.ndarray:
    """Greyscale image as uint8."""
    image = _read_plain(Path(path))
    if image is not None:
        return image
    from PIL import Image

    with Image.open(path) as im:
        return np.asarray(im.convert("L"))
