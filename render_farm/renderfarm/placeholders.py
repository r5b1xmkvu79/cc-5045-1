"""Placeholder PNGs for frames that are not yet valid results.

These are generated (never static stock images) and visually distinguish
pending/leased/failed/cancelled states so the preview clearly marks every
hole instead of silently skipping a frame.
"""

from __future__ import annotations

from .png import encode_png

_PALETTE = {
    "pending": ((52, 58, 70), (72, 80, 96)),
    "leased": ((38, 74, 110), (62, 110, 156)),
    "failed": ((120, 40, 40), (168, 66, 66)),
    "cancelled": ((84, 70, 44), (116, 98, 62)),
    "retrying": ((110, 84, 30), (150, 118, 48)),
}


def placeholder_png(width: int, height: int, status: str) -> bytes:
    base, stripe = _PALETTE.get(status, _PALETTE["pending"])
    px = bytearray(width * height * 3)
    gap = max(6, min(width, height) // 8)
    for y in range(height):
        for x in range(width):
            on = ((x + y) // gap) % 2 == 0
            r, g, b = stripe if on else base
            i = (y * width + x) * 3
            px[i], px[i + 1], px[i + 2] = r, g, b
    return encode_png(width, height, bytes(px), channels=3)
