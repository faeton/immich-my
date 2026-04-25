"""RAW master / camera-baked JPEG preview pairing.

DJI drones (Mavic 3 Pro, Air 3, …) and Sony / Canon / Nikon / Fuji
mirrorless bodies in RAW+JPEG mode write each shot as two files
sharing a stem:
  - `IMG_0001.<DNG|ARW|CR3|NEF|RAF|RW2|ORF|CR2>` — the RAW master,
    full sensor data, full EXIF/GPS, what we want in Immich.
  - `IMG_0001.<JPG|JPEG|HEIC|HEIF>` — the in-camera JPEG preview,
    derived from the RAW by the camera's ISP. Useful as a fast
    on-disk preview, not as a separate Immich asset.

Pairing is by **shared stem in the same directory** (case-insensitive
on stem and extension). When a JPEG has a sibling RAW we drop the
JPEG from ingest — same model as the DJI MP4/LRF pair.

JPEGs without a matching RAW (regular phone photos, exported edits)
return no pair and ingest as normal.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable


_RAW_EXTS = {".dng", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".rw2", ".orf"}
_PREVIEW_EXTS = {".jpg", ".jpeg", ".heic", ".heif"}


def classify(path: Path) -> tuple[str, tuple[str, str]] | None:
    """Return ("raw"|"preview", key) or None.

    `key` is `(parent_dir_posix, stem_lower)` — the pairing identity.
    """
    ext = path.suffix.lower()
    key = (path.parent.as_posix(), path.stem.lower())
    if ext in _RAW_EXTS:
        return "raw", key
    if ext in _PREVIEW_EXTS:
        return "preview", key
    return None


def build_raw_index(paths: Iterable[Path]) -> set[tuple[str, str]]:
    """Set of `(parent, stem_lower)` keys for which a RAW exists."""
    keys: set[tuple[str, str]] = set()
    for path in paths:
        hit = classify(path)
        if hit is not None and hit[0] == "raw":
            keys.add(hit[1])
    return keys


def is_paired_preview(path: Path, raw_index: set[tuple[str, str]]) -> bool:
    """True if `path` is a JPEG/HEIC preview with a sibling RAW master
    in the index — should be skipped at ingest because the RAW is the
    real asset.
    """
    hit = classify(path)
    if hit is None or hit[0] != "preview":
        return False
    return hit[1] in raw_index
