"""Stable identity hashing for scenes, render settings and tasks.

A frame's identity is the tuple
    (scene_id, scene_version, frame_number, settings_hash)
where settings_hash covers *every* input that can change pixels
(resolution, supersampling, background, quality, renderer version...).
Re-submitting an edited shot therefore produces new task identities that
can never hit an old frame in the cache.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

# Bump when the rasteriser's pixel output changes so old cached frames
# can never be mistaken for output of the new renderer.
RENDERER_VERSION = "r1"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    )


def sha256_hex(value: Any) -> str:
    if not isinstance(value, (str, bytes)):
        value = canonical_json(value)
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def settings_hash(settings: dict) -> str:
    """Hash the effective render settings incl. the renderer version."""
    effective = dict(settings or {})
    effective["_renderer"] = RENDERER_VERSION
    return sha256_hex(canonical_json(effective))[:16]


def scene_version(scene_doc: dict) -> str:
    """Content-addressed scene version from the full scene document."""
    return sha256_hex(canonical_json(scene_doc))[:16]


def frame_revision(scene_id: str, version: str, frame: int, s_hash: str) -> str:
    """Human-readable frame identity (not hashed) recorded with adoptions."""
    return f"{scene_id}|{version}|{frame:08d}|{s_hash}"


def task_key(scene_id: str, version: str, frame: int, s_hash: str) -> str:
    raw = frame_revision(scene_id, version, frame, s_hash)
    return sha256_hex(raw)[:32]
