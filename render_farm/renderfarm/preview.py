"""Assemble a time-ordered preview for one submission.

Formal frames are pulled from the scheduler and fully decoded before use;
any frame that is not a valid result is replaced by a generated
status placeholder, so the preview index always equals the requested frame
range (a missing frame is visibly marked, never skipped).

Usage:
    python3 -m renderfarm.preview --server http://127.0.0.1:8765 \
        --submission sub-xxxx --out ./preview.png
"""

from __future__ import annotations

import argparse
import json
import os
import urllib.request

from .png import decode_png, encode_png
from .placeholders import placeholder_png

PAD = 6
LABEL_H = 22
LABEL_BG = (12, 15, 22)
LABEL_FG = (210, 220, 235)
MARK_COLORS = {
    "succeeded": (67, 209, 122),
    "pending": (120, 150, 200),
    "leased": (90, 140, 220),
    "failed": (230, 110, 110),
    "cancelled": (230, 180, 90),
}


def _get(base: str, path: str):
    req = urllib.request.Request(base.rstrip("/") + path)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.status, dict(resp.headers), resp.read()


def build_preview(base_url: str, submission_id: str, out_path: str) -> dict:
    _, _, body = _get(base_url, f"/api/submissions/{submission_id}")
    detail = json.loads(body)
    sub, tasks = detail["submission"], detail["tasks"]
    tasks = sorted(tasks, key=lambda t: t["frame"])
    if not tasks:
        raise RuntimeError("submission has no frames")

    tiles = []
    marked = []
    for t in tasks:
        frame = t["frame"]
        _, headers, blob = _get(
            base_url, f"/api/submissions/{submission_id}/frames/{frame}")
        status = headers.get("X-Frame-Status", t["status"])
        w, h, _, raw = decode_png(blob)  # full decode, not existence check
        if status != "succeeded":
            blob = placeholder_png(w, h, status)
            _, _, _, raw = decode_png(blob)
            marked.append((frame, status))
        tiles.append((frame, status, w, h, blob))

    tw = max(x[2] for x in tiles)
    th = max(x[3] for x in tiles)
    cols = min(4, len(tiles))
    rows = (len(tiles) + cols - 1) // cols
    sheet_w = cols * tw + (cols + 1) * PAD
    sheet_h = rows * (th + LABEL_H) + (rows + 1) * PAD
    sheet = bytearray([v for v in LABEL_BG for _ in range(sheet_w * sheet_h)])

    def put(x0: int, y0: int, src: bytes, sw: int, sh: int) -> None:
        for y in range(sh):
            for x in range(sw):
                i = ((y0 + y) * sheet_w + (x0 + x)) * 3
                j = (y * sw + x) * 3
                sheet[i:i + 3] = src[j:j + 3]

    def label(x0: int, y0: int, frame: int, status: str) -> None:
        # status colour block, then the frame number drawn with 5x7 glyphs (2x scale)
        color = MARK_COLORS.get(status, LABEL_FG)
        for y in range(3, 15):
            for x in range(3, 13):
                idx = ((y0 + y) * sheet_w + (x0 + x)) * 3
                sheet[idx:idx + 3] = bytes(color)
        text = f"{frame:03d}"
        scale = 2
        tx = x0 + 22
        for i, ch in enumerate(text):
            for gy, bits in enumerate(_GLYPHS[ch]):
                for gx in range(5):
                    if bits & (1 << (4 - gx)):
                        for sy in range(scale):
                            for sx in range(scale):
                                px = tx + i * (7 * scale) + gx * scale + sx
                                py = y0 + 4 + gy * scale + sy
                                if px < sheet_w and py < sheet_h:
                                    idx = (py * sheet_w + px) * 3
                                    sheet[idx:idx + 3] = bytes(LABEL_FG)

    for k, (frame, status, w, h, blob) in enumerate(tiles):
        r, c = divmod(k, cols)
        x0 = PAD + c * (tw + PAD)
        y0 = PAD + r * (th + LABEL_H + PAD)
        _, _, _, raw = decode_png(blob)
        put(x0, y0, raw, w, h)
        label(x0, y0 + th + 1, frame, status)

    with open(out_path, "wb") as fh:
        fh.write(encode_png(sheet_w, sheet_h, bytes(sheet), channels=3))
    return {
        "out": os.path.abspath(out_path),
        "frames": len(tiles),
        "marked_unfinished": marked,
        "submission": sub,
    }


# 5x7 bitmap glyphs encoded as one 5-bit mask per row (MSB = leftmost).
_GLYPHS = {
    "0": [0b01110, 0b10001, 0b10011, 0b10101, 0b11001, 0b10001, 0b01110],
    "1": [0b00100, 0b01100, 0b00100, 0b00100, 0b00100, 0b00100, 0b01110],
    "2": [0b01110, 0b10001, 0b00001, 0b00010, 0b00100, 0b01000, 0b11111],
    "3": [0b11110, 0b00001, 0b00001, 0b01110, 0b00001, 0b00001, 0b11110],
    "4": [0b00010, 0b00110, 0b01010, 0b10010, 0b11111, 0b00010, 0b00010],
    "5": [0b11111, 0b10000, 0b11110, 0b00001, 0b00001, 0b10001, 0b01110],
    "6": [0b00110, 0b01000, 0b10000, 0b11110, 0b10001, 0b10001, 0b01110],
    "7": [0b11111, 0b00001, 0b00010, 0b00100, 0b01000, 0b01000, 0b01000],
    "8": [0b01110, 0b10001, 0b10001, 0b01110, 0b10001, 0b10001, 0b01110],
    "9": [0b01110, 0b10001, 0b10001, 0b01111, 0b00001, 0b00010, 0b01100],
}


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a time-ordered preview sheet")
    parser.add_argument("--server", default="http://127.0.0.1:8765")
    parser.add_argument("--submission", required=True)
    parser.add_argument("--out", default="preview.png")
    args = parser.parse_args()
    info = build_preview(args.server, args.submission, args.out)
    print(json.dumps(info, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
