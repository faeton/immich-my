"""Insta360 master ↔ proxy helpers.

The camera writes two files per recording: the master dual-fisheye
`VID_*.insv` (5.7K / 8K for newer models) and a low-res proxy
`LRV_*.lrv` alongside it. Both are dual-fisheye — neither is stitched
equirectangular.

For Immich derivatives (poster frame + 720p `encoded_video`), we'd
rather decode the small LRV than the multi-gigabyte master: output
looks identical (both are dual-fisheye) but encoding is ~30× faster
and the file stays browser-playable at the same 720p target.

This module only picks the *source file for derivatives*. It never
rewrites `asset.originalPath` — the Immich asset keeps pointing at the
real `.insv` master, so the "download original" button still returns
the camera file untouched. Stitching is out of scope: ffmpeg's `v360`
filter needs per-camera calibration that varies across X2/X3/X4/X5
and would produce inconsistent results. Use Insta360 Studio for real
stitching; this is just a compatibility encoder.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

from .filenames import Insta360Key, parse_insta360


def classify(path: Path) -> tuple[str, Insta360Key] | None:
    """Return ("master"|"proxy", key) or None if the filename doesn't
    match the Insta360 naming scheme.

    Role is taken from the filename prefix — `VID_*` is a master lens
    file, `LRV_*` is the low-res proxy. Extension alone is ambiguous
    because some Insta360 models ship LRV content inside an `.insv`
    container (the "LRV" in the prefix is authoritative, not the ext).
    """
    key = parse_insta360(path)
    if key is None:
        return None
    prefix = path.stem.split("_", 1)[0].upper()
    role = "master" if prefix == "VID" else "proxy" if prefix == "LRV" else None
    if role is None:
        return None
    return role, key


def build_proxy_index(paths: Iterable[Path]) -> dict[tuple[str, str], Path]:
    """Map `(timestamp, serial)` → proxy path for every LRV found.

    Two-lens recordings on some models emit a single LRV shared between
    both VID lens files, so the index is keyed by `(timestamp, serial)`
    (lens code is intentionally ignored). If multiple LRVs share a key
    — Insta360 Studio can produce this during re-exports — the first
    one by sort order wins.
    """
    index: dict[tuple[str, str], Path] = {}
    for path in sorted(paths):
        hit = classify(path)
        if hit is None:
            continue
        role, key = hit
        if role != "proxy":
            continue
        index.setdefault((key.timestamp, key.serial), path)
    return index


def proxy_for(
    master_path: Path, index: dict[tuple[str, str], Path],
) -> Path | None:
    """Return the matching LRV proxy for a master VID file, or None."""
    hit = classify(master_path)
    if hit is None:
        return None
    role, key = hit
    if role != "master":
        return None
    return index.get((key.timestamp, key.serial))


__all__ = ["classify", "build_proxy_index", "proxy_for"]
