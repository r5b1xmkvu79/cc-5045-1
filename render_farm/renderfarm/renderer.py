"""Dependency-free 2D rasteriser.

Renders a scene document for a given frame into real PNG bytes.  Coordinates
are normalised [0, 1] with y pointing up; objects interpolate along paths
between frames, so consecutive frames genuinely differ pixel by pixel.
Supersampling (SSAA) is part of the render *settings*, hence part of the
frame identity hash.
"""

from __future__ import annotations

import math
from typing import Any

from .png import encode_png

DEFAULT_SETTINGS = {
    "width": 320,
    "height": 180,
    "ssaa": 2,
    "format": "png",
}


def _ease(p: float, easing: str) -> float:
    if easing == "linear":
        return p
    if easing == "smooth":
        return 0.5 - 0.5 * math.cos(math.pi * p)
    raise ValueError(f"unknown easing {easing!r}")


def _progress(p: float, loop: bool) -> float:
    if not loop:
        return min(max(p, 0.0), 1.0)
    t = (p * 2.0) % 2.0
    return t if t <= 1.0 else 2.0 - t


def object_position(obj: dict, frame: int, total_frames: int) -> tuple[float, float]:
    path = obj["path"]
    p = frame / (total_frames - 1) if total_frames > 1 else 0.0
    t = _ease(_progress(p, bool(path.get("loop", False))), path.get("easing", "linear"))
    sx, sy = path["start"]
    ex, ey = path["end"]
    return sx + (ex - sx) * t, sy + (ey - sy) * t


def _fill_rect(buf: bytearray, w: int, h: int, x0: int, y0: int, x1: int, y1: int,
               color: tuple[int, int, int]) -> None:
    x0, x1 = max(0, x0), min(w, x1)
    y0, y1 = max(0, y0), min(h, y1)
    r, g, b = color
    for y in range(y0, y1):
        base = (y * w + x0) * 3
        row_end = base + (x1 - x0) * 3
        i = base
        while i < row_end:
            buf[i] = r
            buf[i + 1] = g
            buf[i + 2] = b
            i += 3


def _fill_circle(buf: bytearray, w: int, h: int, cx: float, cy: float,
                 radius: float, color: tuple[int, int, int]) -> None:
    r, g, b = color
    x0, x1 = int(cx - radius - 1), int(cx + radius + 2)
    y0, y1 = int(cy - radius - 1), int(cy + radius + 2)
    r2 = radius * radius
    for y in range(max(0, y0), min(h, y1)):
        dy = y + 0.5 - cy
        for x in range(max(0, x0), min(w, x1)):
            dx = x + 0.5 - cx
            if dx * dx + dy * dy <= r2:
                i = (y * w + x) * 3
                buf[i] = r
                buf[i + 1] = g
                buf[i + 2] = b


def _as_rgb(value: Any, default: tuple[int, int, int]) -> tuple[int, int, int]:
    if not value:
        return default
    return (int(value[0]), int(value[1]), int(value[2]))


def render_frame(scene_doc: dict, frame: int, settings: dict) -> bytes:
    """Render one frame and return encoded PNG bytes."""
    width = int(settings["width"])
    height = int(settings["height"])
    ssaa = max(1, int(settings.get("ssaa", 1)))
    if width <= 0 or height <= 0:
        raise ValueError("width/height must be positive")

    total_frames = int(scene_doc["total_frames"])
    if not 0 <= frame < total_frames:
        raise ValueError(f"frame {frame} outside 0..{total_frames - 1}")

    w, h = width * ssaa, height * ssaa
    buf = bytearray(w * h * 3)

    bg = scene_doc.get("background", {})
    top = _as_rgb(bg.get("top"), (18, 26, 48))
    bottom = _as_rgb(bg.get("bottom"), (96, 140, 190))
    for y in range(h):
        f = y / max(1, h - 1)
        row = (
            int(top[0] + (bottom[0] - top[0]) * f),
            int(top[1] + (bottom[1] - top[1]) * f),
            int(top[2] + (bottom[2] - top[2]) * f),
        )
        _fill_rect(buf, w, h, 0, y, w, y + 1, row)

    ground = scene_doc.get("ground")
    if ground:
        gy = int((1.0 - float(ground["y"])) * h)
        _fill_rect(buf, w, h, 0, gy, w, h, _as_rgb(ground.get("color"), (40, 64, 44)))

    for obj in scene_doc.get("objects", []):
        color = _as_rgb(obj.get("color"), (220, 80, 80))
        nx, ny = object_position(obj, frame, total_frames)
        cx, cy = nx * w, (1.0 - ny) * h
        if obj.get("type", "circle") == "circle":
            radius = float(obj.get("radius", 0.06)) * min(w, h)
            _fill_circle(buf, w, h, cx, cy, radius, color)
        elif obj["type"] == "rect":
            rw = float(obj.get("width", 0.1)) * w
            rh = float(obj.get("height", 0.1)) * h
            _fill_rect(buf, w, h, int(cx - rw / 2), int(cy - rh / 2),
                       int(cx + rw / 2), int(cy + rh / 2), color)
        else:
            raise ValueError(f"unknown object type {obj['type']!r}")

    if ssaa > 1:
        buf = _downsample(buf, w, h, ssaa)
    return encode_png(width, height, bytes(buf), channels=3)


def _downsample(buf: bytearray, w: int, h: int, ssaa: int) -> bytearray:
    out_w, out_h = w // ssaa, h // ssaa
    out = bytearray(out_w * out_h * 3)
    area = ssaa * ssaa
    for oy in range(out_h):
        for ox in range(out_w):
            for c in range(3):
                acc = 0
                for sy in range(ssaa):
                    row = ((oy * ssaa + sy) * w + ox * ssaa) * 3 + c
                    acc += sum(buf[row + sx * 3] for sx in range(ssaa))
                out[(oy * out_w + ox) * 3 + c] = acc // area
    return out
