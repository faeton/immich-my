#!/usr/bin/env python3
"""
Remove orphaned JPG-twin asset records left behind by older `immy process`
runs that ingested DJI .DNG+.JPG and Sony .ARW+.JPG pairs as two separate
assets each.

Effective from the `raw_mod.is_paired_preview` filter (see
`immy/src/immy/process.py`), the JPG twin of a sibling RAW is no longer
ingested. This tool cleans up the offline asset records + derivatives
that the JPG twins produced in prior runs, so a re-run of `immy process`
yields a consistent state.

Per trip:
  1. Walk `.audit/offline/*.yml`. For each yml whose `asset.original_path`
     ends in `.jpg/.jpeg/.heic/.heif` AND has a same-stem RAW sibling on
     disk in the same directory → mark for removal.
  2. Refuse to touch any yml with `synced: true` (out of scope; user
     said no-promote).
  3. For each marked asset, delete:
        .audit/offline/<sha1>.yml
        .audit/derivatives/_posters/<asset-id>.jpg
        .audit/derivatives/thumbs/**/<asset-id>*
        .audit/derivatives/encoded-video/**/<asset-id>*   (no-op for images)
  4. If anything was removed, delete `.audit/y_processed.yml` so the
     trip is no longer considered "fully processed" and the offline
     re-run picks it up.

Default is dry-run; pass `--apply` to actually delete.

Usage:
    tools/raw-pair-clean.py <trip-folder> [<trip-folder> …]
    tools/raw-pair-clean.py <trip-folder> --apply
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml


RAW_EXTS = {".dng", ".cr2", ".cr3", ".arw", ".nef", ".raf", ".rw2", ".orf"}
PREVIEW_EXTS = {".jpg", ".jpeg", ".heic", ".heif"}


def build_raw_stem_index(trip: Path) -> set[tuple[Path, str]]:
    """(parent_dir, stem_lower) for every RAW file on disk under `trip`."""
    idx: set[tuple[Path, str]] = set()
    for p in trip.rglob("*"):
        if not p.is_file() or ".audit" in p.parts:
            continue
        if p.suffix.lower() in RAW_EXTS:
            idx.add((p.parent, p.stem.lower()))
    return idx


def resolve_local(original_path: str, trip: Path) -> Path | None:
    """Map the recorded `/mnt/external/originals/<trip>/<rel>` to the
    local trip path. Returns None if it doesn't look like a path under
    this trip.
    """
    needle = f"/{trip.name}/"
    i = original_path.find(needle)
    if i == -1:
        return None
    rel = original_path[i + len(needle):]
    return trip / rel


def find_paired_jpg_assets(trip: Path) -> tuple[list[dict], list[Path]]:
    """Return (paired_records, synced_warnings).

    Each `paired_records` entry has keys: yml, asset_id, local_path.
    `synced_warnings` is a list of yml paths that had `synced: true` —
    not removed; user must clean those via the Immich API separately.
    """
    offline_dir = trip / ".audit" / "offline"
    if not offline_dir.is_dir():
        return [], []

    raw_index = build_raw_stem_index(trip)

    paired: list[dict] = []
    synced: list[Path] = []
    for yml in sorted(offline_dir.glob("*.yml")):
        try:
            doc = yaml.safe_load(yml.read_text())
        except Exception as e:
            print(f"  [WARN] could not parse {yml.name}: {e}", file=sys.stderr)
            continue
        if not isinstance(doc, dict):
            continue
        asset = doc.get("asset") or {}
        original_path = asset.get("original_path") or ""
        asset_id = asset.get("id") or ""
        if not original_path or not asset_id:
            continue

        local = resolve_local(original_path, trip)
        if local is None:
            continue
        if local.suffix.lower() not in PREVIEW_EXTS:
            continue

        key = (local.parent, local.stem.lower())
        if key not in raw_index:
            continue

        if doc.get("synced") is True:
            synced.append(yml)
            continue

        paired.append({
            "yml": yml,
            "asset_id": asset_id,
            "local_path": local,
        })
    return paired, synced


def derivative_targets(trip: Path, asset_id: str) -> list[Path]:
    deriv = trip / ".audit" / "derivatives"
    targets: list[Path] = []

    poster = deriv / "_posters" / f"{asset_id}.jpg"
    if poster.exists():
        targets.append(poster)

    for sub in ("thumbs", "encoded-video"):
        base = deriv / sub
        if not base.is_dir():
            continue
        for p in base.rglob(f"{asset_id}*"):
            if p.is_file():
                targets.append(p)
    return targets


def filter_y_processed(trip: Path, removed_local_paths: set[Path]) -> None:
    """Delete the y_processed marker entirely if anything changed.

    `process.is_trip_fully_cached` keys off marker presence + count + mtimes;
    deleting it forces a full re-scan on the next `immy process` run.
    """
    marker = trip / ".audit" / "y_processed.yml"
    if marker.exists() and removed_local_paths:
        marker.unlink()


def process_trip(trip: Path, apply: bool) -> dict:
    if not trip.is_dir():
        print(f"[SKIP] {trip} — not a directory")
        return {"trip": trip.name, "paired": 0, "synced": 0}

    paired, synced = find_paired_jpg_assets(trip)

    print(f"\n{trip.name}/")
    print(f"  paired JPG assets to remove: {len(paired)}")
    if synced:
        print(f"  [WARN] {len(synced)} synced ymls present — NOT removing "
              f"(use Immich API): {[y.name for y in synced[:3]]}"
              + (" …" if len(synced) > 3 else ""))

    if not paired:
        return {"trip": trip.name, "paired": 0, "synced": len(synced)}

    total_files = 0
    bytes_freed = 0
    for rec in paired:
        targets = [rec["yml"]] + derivative_targets(trip, rec["asset_id"])
        for t in targets:
            try:
                bytes_freed += t.stat().st_size
            except OSError:
                pass
            total_files += 1
            if apply:
                try:
                    t.unlink()
                except OSError as e:
                    print(f"    [ERR] could not delete {t}: {e}", file=sys.stderr)

    print(f"  files {'removed' if apply else 'would remove'}: "
          f"{total_files} ({bytes_freed / 1e6:.1f} MB)")

    if apply:
        filter_y_processed(trip, {rec["local_path"] for rec in paired})
        marker = trip / ".audit" / "y_processed.yml"
        print(f"  y_processed.yml: {'deleted' if not marker.exists() else 'still present'}")
    else:
        print(f"  y_processed.yml: would be deleted (forces re-scan)")

    return {"trip": trip.name, "paired": len(paired), "synced": len(synced)}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("trips", nargs="+", type=Path, help="trip folders to clean")
    p.add_argument("--apply", action="store_true",
                   help="actually delete (default: dry-run)")
    args = p.parse_args()

    if not args.apply:
        print("[dry-run] use --apply to actually delete\n")

    grand_paired = 0
    grand_synced = 0
    for trip in args.trips:
        r = process_trip(trip.resolve(), args.apply)
        grand_paired += r["paired"]
        grand_synced += r["synced"]

    print(f"\nTOTAL paired JPG assets: {grand_paired}"
          + (f"  (synced/promoted skipped: {grand_synced})" if grand_synced else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
