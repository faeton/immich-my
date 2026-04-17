"""Thin pyexiftool wrapper.

One process per audit (pyexiftool keeps exiftool warm in -stay_open mode).
Header-only reads (`-fast2`), numeric values (`-n`), one JSON blob per file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import exiftool


MEDIA_EXTS = {
    ".jpg", ".jpeg", ".heic", ".heif", ".png", ".tif", ".tiff",
    ".dng", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".rw2", ".orf",
    ".mp4", ".mov", ".m4v", ".avi", ".mkv", ".mts", ".m2ts",
    ".insv", ".insp", ".lrv", ".lrf",
}


@dataclass
class ExifRow:
    path: Path
    raw: dict[str, Any]

    def get(self, *keys: str) -> Any:
        for k in keys:
            if k in self.raw:
                return self.raw[k]
        return None


def has_gps(row: "ExifRow") -> bool:
    lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude")
    lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude")
    return lat is not None and lon is not None


def iter_media(folder: Path) -> Iterable[Path]:
    for p in sorted(folder.rglob("*")):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            yield p


def read_folder(folder: Path) -> list[ExifRow]:
    files = list(iter_media(folder))
    if not files:
        return []
    # exiftool does not auto-pair media with adjacent .xmp sidecars, so we
    # read both and merge sidecar XMP:* tags into the media row. This keeps
    # downstream rules (trip-timezone etc.) aware of fields written by
    # earlier passes of the same audit.
    sidecars_to_read: dict[Path, Path] = {}
    for f in files:
        side = f.with_suffix(".xmp")
        if side.is_file():
            sidecars_to_read[f] = side

    targets = [str(f) for f in files] + [str(s) for s in sidecars_to_read.values()]

    with exiftool.ExifToolHelper(
        common_args=["-G", "-n", "-fast2", "-m"],
        check_execute=False,
    ) as et:
        try:
            blobs = et.get_metadata(targets)
        except Exception:
            blobs = []

    by_path = {Path(b["SourceFile"]): b for b in blobs if "SourceFile" in b}
    rows: list[ExifRow] = []
    for f in files:
        raw = dict(by_path.get(f, {"SourceFile": str(f)}))
        side = sidecars_to_read.get(f)
        if side is not None:
            sblob = by_path.get(side, {})
            for k, v in sblob.items():
                if k.startswith("XMP:") and k not in raw:
                    raw[k] = v
        rows.append(ExifRow(path=f, raw=raw))
    return rows
