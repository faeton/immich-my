#!/usr/bin/env python3
"""Report trips that have media files not yet ingested by `immy process`.

Drift = a media file on disk whose basename is absent from the trip's
`.audit/y_processed.yml` marker (the inventory `immy process` writes).
This is mtime-independent — it catches files *moved* into an
already-processed trip (e.g. GO2 clips dropped into 2024-03-antarctica),
which a `-newer`-than-marker check would miss because `mv` preserves the
original (older) mtime.

Exit status: 0 always; output lists NEW (never processed) and DRIFT
(processed but has un-ingested files) trips. Run `immy process` (and
`immy promote --rescan`) on the listed trips.

Usage: python3 check_drift.py [TRIPS_DIR]   (default ~/Media/Trips)
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# Extensions immy ingests as assets. LRF (DJI low-res proxy) is dropped at
# ingest, so it's never in the marker — exclude it here to avoid false drift.
MEDIA_EXT = {
    ".mp4", ".mov", ".m4v", ".avi", ".insv", ".lrv",
    ".jpg", ".jpeg", ".png", ".heic", ".heif", ".webp",
    ".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".rw2", ".insp",
}
SKIP_EXT = {".lrf"}  # dropped proxies — not assets
# RAW formats: a sibling JPG with the same stem is an in-camera preview
# that immy drops at ingest (the RAW is the asset), so it's never in the
# marker — exclude such JPGs here too, or they read as false drift.
RAW_EXT = {".dng", ".arw", ".cr2", ".cr3", ".nef", ".raf", ".rw2"}
PREVIEW_EXT = {".jpg", ".jpeg"}


def marker_files(trip: Path) -> set[str] | None:
    m = trip / ".audit" / "y_processed.yml"
    if not m.is_file():
        return None
    data = yaml.safe_load(m.read_text()) or {}
    return {os.path.basename(str(a.get("file", ""))) for a in data.get("assets", [])}


def disk_media(trip: Path) -> set[str]:
    files = [p for p in trip.rglob("*") if ".audit" not in p.parts and p.is_file()]
    # stems that have a RAW file → their JPG sibling is a dropped preview
    raw_stems = {p.with_suffix("") for p in files if p.suffix.lower() in RAW_EXT}
    out: set[str] = set()
    for p in files:
        ext = p.suffix.lower()
        if ext in SKIP_EXT or ext not in MEDIA_EXT:
            continue
        if ext in PREVIEW_EXT and p.with_suffix("") in raw_stems:
            continue  # paired RAW preview — immy drops it at ingest
        out.add(p.name)
    return out


def main() -> int:
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Media" / "Trips"
    new, drift, ok = [], [], 0
    for trip in sorted(p for p in root.iterdir() if p.is_dir()):
        on_disk = disk_media(trip)
        if not on_disk:
            continue
        marked = marker_files(trip)
        if marked is None:
            new.append((trip.name, len(on_disk)))
            continue
        missing = on_disk - marked
        if missing:
            drift.append((trip.name, sorted(missing)))
        else:
            ok += 1

    if new:
        print("NEW (never processed):")
        for name, n in new:
            print(f"  {name}  ({n} media files)")
    if drift:
        print("\nDRIFT (processed, but has un-ingested files):")
        for name, files in drift:
            sample = ", ".join(files[:4]) + (" …" if len(files) > 4 else "")
            print(f"  {name}  (+{len(files)}: {sample})")
    print(f"\n{ok} trip(s) up to date · {len(new)} new · {len(drift)} drifted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
