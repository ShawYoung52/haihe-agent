from __future__ import annotations

import struct
import zlib

import numpy as np


def encode_rgba_png(image: np.ndarray) -> bytes:
    if image.dtype != np.uint8 or image.ndim != 3 or image.shape[2] != 4:
        raise ValueError("PNG input must be an uint8 RGBA array")

    height, width, _ = image.shape
    raw_rows = bytearray()
    for y in range(height):
        raw_rows.append(0)
        raw_rows.extend(image[y].tobytes())

    return b"".join(
        [
            b"\x89PNG\r\n\x1a\n",
            _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)),
            _chunk(b"IDAT", zlib.compress(bytes(raw_rows), 6)),
            _chunk(b"IEND", b""),
        ]
    )


def _chunk(kind: bytes, data: bytes) -> bytes:
    checksum = zlib.crc32(kind)
    checksum = zlib.crc32(data, checksum)
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", checksum & 0xFFFFFFFF)

