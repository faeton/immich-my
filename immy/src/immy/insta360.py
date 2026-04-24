"""Insta360 derivative-source + de-warp helpers.

Each recording on an Insta360 ONE X-series produces:
  - Two master `VID_*.insv` files (one per lens), each **2880×2880
    single-lens fisheye** (1:1). `_00_` = front hemisphere, `_10_` =
    rear, mounted back-to-back with mirrored up-axes.
  - One `LRV_*.lrv` (or `.insv` on older firmware) — the camera's own
    **in-camera stitched equirectangular** preview (2:1 aspect,
    1664×832 on X3/X4, 736×368 on X/X2).

Two derivative strategies, picked per-asset:

1. **LRV present (~87% of recordings in the real corpus).** Point
   ffmpeg at the LRV for poster + encoded_video. It's already a
   stitched 2:1 equirect, so the resulting Immich tile looks like a
   proper 360 panorama. Decoding is ~30× faster than the master.

2. **No LRV (~13%, typically older footage or cards where LRV was
   stripped).** Fall back to the master `.insv` but apply a v360
   fisheye→flat de-warp with per-lens rotation. Output is a normal-
   looking rectilinear photo centered on the lens axis — no fisheye
   porthole, no black half-hemisphere voids. Single-lens de-warp has
   no calibration drift (the X2/X3/X4/X5 stitching-seam problem is
   specific to dual-lens joins, which we never do).

Asset identity is preserved in both paths: `asset.originalPath` always
points at the real master `.insv`, so Immich's "download original"
returns the camera file untouched. Stitching across the two hemispheres
is still Insta360 Studio's job, not ours.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import Iterable

import re

from .filenames import Insta360Key, parse_insta360


# Insta360 master files are always 2880×2880 single-lens fisheye (verified
# across ONE X / X2 / X3 / X4, ~500 files in the real corpus). The two
# lens copies of a recording share a timestamp+serial but differ by lens
# code: `_00_` is one hemisphere, `_10_` is the other — mounted back-to-
# back, so their up/down axes are mirrored. v360 de-warps a single
# fisheye into a flat perspective view, then transpose rotates to
# natural viewing orientation (the camera records with "up" to the side).
#
# FOV numbers: Insta360 lenses claim ~200° per hemisphere, which lets
# their own stitcher overlap the seams. For a centered flat crop we take
# h_fov=120 / v_fov=90 — wider than a normal lens, so the tile shows
# meaningful context, but tight enough that the periphery warp stays
# below the noticeable threshold and the lens shadow/hub at the far
# edge of the circle is cropped out.
_DEWARP_COMMON = (
    "v360=input=fisheye:output=flat"
    ":ih_fov=200:iv_fov=200:h_fov=120:v_fov=90:w=1280:h=720"
)
# transpose=1 → 90° clockwise; transpose=3 → 90° cw + vertical flip.
# Mapping is empirical (see docstring above), verified on _00_ / _10_
# samples from three different trips.
_DEWARP_BY_LENS = {
    "00": f"{_DEWARP_COMMON},transpose=1",
    "10": f"{_DEWARP_COMMON},transpose=3",
}

_LENS_RE = re.compile(r"^(?:VID|LRV)_\d{8}_\d{6}_(?P<lens>\d{2})_", re.I)


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


def dewarp_vf(master_path: Path) -> str | None:
    """ffmpeg `-vf` string that de-warps a single-lens Insta360 master.

    Returned only for `VID_*.insv` masters with a recognised `_00_` or
    `_10_` lens code. Callers feed this into `extract_poster` and
    `transcode` when no LRV proxy is available — the output is a flat
    perspective crop rotated to the viewer's natural orientation.

    Non-masters, proxies, and unknown lens codes return None — the
    caller falls through to a plain pass-through encode.
    """
    hit = classify(master_path)
    if hit is None or hit[0] != "master":
        return None
    m = _LENS_RE.match(master_path.stem)
    if m is None:
        return None
    return _DEWARP_BY_LENS.get(m.group("lens"))


__all__ = ["classify", "build_proxy_index", "proxy_for", "dewarp_vf"]
