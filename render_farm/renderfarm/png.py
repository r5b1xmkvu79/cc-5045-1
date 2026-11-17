"""Minimal dependency-free PNG codec (8-bit grayscale / RGB / RGBA).

Only the subset produced by this project's encoder is decoded:
truecolour (colour type 2/6) or grayscale (0/4), 8-bit depth, single IDAT
or multiple IDAT chunks.  Decoding validates the signature, IHDR, CRC of
every chunk, zlib stream and reconstructs scanlines through all five
PNG filter types.  This is what makes a "file exists" check insufficient:
a truncated or half-written upload fails here.
"""

from __future__ import annotations

import struct
import zlib

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

_FILTER_NONE = 0
_FILTER_SUB = 1
_FILTER_UP = 2
_FILTER_AVG = 3
_FILTER_PAETH = 4


class PNGError(ValueError):
    """Raised when a byte stream is not a valid, complete PNG image."""


def _paeth(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def encode_png(width: int, height: int, pixels: bytes, channels: int = 3) -> bytes:
    """Encode raw row-major 8-bit pixels into a single PNG byte string."""
    if channels not in (1, 3, 4):
        raise PNGError(f"unsupported channel count {channels}")
    if width <= 0 or height <= 0 or width > 65535 or height > 65535:
        raise PNGError("invalid dimensions")
    expected = width * height * channels
    if len(pixels) != expected:
        raise PNGError(
            f"pixel buffer length {len(pixels)} != {expected} "
            f"for {width}x{height}x{channels}"
        )
    colour_type = {1: 0, 3: 2, 4: 6}[channels]
    ihdr = struct.pack(">IIBBBBB", width, height, 8, colour_type, 0, 0, 0)
    raw = bytearray()
    stride = width * channels
    for y in range(height):
        raw.append(_FILTER_NONE)
        raw.extend(pixels[y * stride : (y + 1) * stride])
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 6))
        + _chunk(b"IEND", b"")
    )


def decode_png(blob: bytes) -> tuple[int, int, int, bytes]:
    """Decode PNG bytes -> (width, height, channels, raw RGB(A)/gray bytes).

    Raises PNGError on any structural or checksum problem, including a
    truncated stream (half-uploaded image).
    """
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 8:
        raise PNGError("payload too small to be a PNG")
    blob = bytes(blob)
    if blob[:8] != PNG_SIGNATURE:
        raise PNGError("bad PNG signature")

    pos = 8
    width = height = bit_depth = colour_type = None
    idat = bytearray()
    saw_ihdr = False
    while pos < len(blob):
        if pos + 8 > len(blob):
            raise PNGError("truncated chunk header")
        (length,) = struct.unpack(">I", blob[pos : pos + 4])
        tag = blob[pos + 4 : pos + 8]
        start, end = pos + 8, pos + 8 + length
        if end + 4 > len(blob):
            raise PNGError(f"truncated {tag!r} chunk")
        data = blob[start:end]
        (crc,) = struct.unpack(">I", blob[end : end + 4])
        if zlib.crc32(tag + data) & 0xFFFFFFFF != crc:
            raise PNGError(f"CRC mismatch in {tag!r} chunk")
        pos = end + 4

        if tag == b"IHDR":
            if saw_ihdr:
                raise PNGError("duplicate IHDR")
            saw_ihdr = True
            width, height, bit_depth, colour_type, compression, filt, interlace = (
                struct.unpack(">IIBBBBB", data)
            )
            if compression != 0 or filt != 0 or interlace != 0:
                raise PNGError("unsupported PNG variant")
            if bit_depth != 8 or colour_type not in (0, 2, 4, 6):
                raise PNGError(
                    f"unsupported bit depth/colour type: {bit_depth}/{colour_type}"
                )
        elif tag == b"IDAT":
            if not saw_ihdr:
                raise PNGError("IDAT before IHDR")
            idat.extend(data)
        elif tag == b"IEND":
            break

    if not saw_ihdr:
        raise PNGError("missing IHDR")
    if not idat:
        raise PNGError("missing IDAT")

    channels = {0: 1, 2: 3, 4: 2, 6: 4}[colour_type]
    stride = width * channels
    try:
        raw = zlib.decompress(bytes(idat))
    except zlib.error as exc:
        raise PNGError(f"corrupt zlib stream: {exc}") from exc
    if len(raw) != height * (stride + 1):
        raise PNGError("decompressed data has wrong length (truncated image?)")

    out = bytearray(width * height * channels)
    prev = bytearray(stride)
    for y in range(height):
        row_start = y * (stride + 1)
        filter_type = raw[row_start]
        line = raw[row_start + 1 : row_start + 1 + stride]
        recon = bytearray(stride)
        for i, value in enumerate(line):
            left = recon[i - channels] if i >= channels else 0
            up = prev[i]
            up_left = prev[i - channels] if i >= channels else 0
            if filter_type == _FILTER_NONE:
                recon[i] = value
            elif filter_type == _FILTER_SUB:
                recon[i] = (value + left) & 0xFF
            elif filter_type == _FILTER_UP:
                recon[i] = (value + up) & 0xFF
            elif filter_type == _FILTER_AVG:
                recon[i] = (value + ((left + up) >> 1)) & 0xFF
            elif filter_type == _FILTER_PAETH:
                recon[i] = (value + _paeth(left, up, up_left)) & 0xFF
            else:
                raise PNGError(f"unknown scanline filter {filter_type}")
        out[y * stride : (y + 1) * stride] = recon
        prev = recon
    return width, height, channels, bytes(out)
