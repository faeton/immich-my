"""Thin pyexiftool wrapper.

One process per audit (pyexiftool keeps exiftool warm in -stay_open mode).
Header-only reads (`-fast2`), numeric values (`-n`), one JSON blob per file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import exiftool

from .state import AUDIT_DIR


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


def _has_value(value: Any) -> bool:
    return value is not None and not (isinstance(value, str) and not value.strip())


def has_gps(row: "ExifRow") -> bool:
    """True if a GPS lat/lon tag is *present* (may be the null-island 0,0
    sensor artifact). Use for 'does this file carry a GPS tag at all'."""
    lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude")
    lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude")
    return _has_value(lat) and _has_value(lon)


def has_valid_gps(row: "ExifRow") -> bool:
    """True only for a *usable* fix — present and not null-island (0,0).

    This is the right test for 'does this file still need a position?' and
    for 'can this file serve as a GPS source?'. Cameras and exports often
    stamp 0,0 when they have no lock; treating that as located leaves the
    file unrepairable and lets it seed siblings with garbage coords."""
    lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude")
    lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude")
    if not (_has_value(lat) and _has_value(lon)):
        return False
    try:
        lat_f, lon_f = float(lat), float(lon)
    except (TypeError, ValueError):
        # Present but unparseable (e.g. a DMS string from a non-`-n` read) —
        # treat as a real fix; we can't prove it's null-island.
        return True
    return not (abs(lat_f) < 1e-3 and abs(lon_f) < 1e-3)


def _is_under_audit_dir(folder: Path, path: Path) -> bool:
    try:
        rel = path.relative_to(folder)
    except ValueError:
        return False
    return AUDIT_DIR in rel.parts


def iter_media(folder: Path) -> Iterable[Path]:
    for p in sorted(folder.rglob("*")):
        if _is_under_audit_dir(folder, p):
            continue
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            yield p


def read_folder(folder: Path) -> list[ExifRow]:
    import sys
    import time
    t_total = time.monotonic()
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
        except Exception as exc:
            # A batch failure (one unreadable file, exiftool crash) must not
            # blank metadata for every sibling in the folder. Retry per file
            # so the rest still get dates/GPS/dimensions, and log what failed.
            sys.stderr.write(
                f"  exiftool batch read failed ({exc}); retrying per file…\n"
            )
            sys.stderr.flush()
            blobs = []
            for t in targets:
                try:
                    blobs.extend(et.get_metadata([t]))
                except Exception as inner:
                    sys.stderr.write(f"    exiftool failed on {t}: {inner}\n")
                    sys.stderr.flush()

    by_path = {Path(b["SourceFile"]): b for b in blobs if "SourceFile" in b}

    # Insta360 stores Make/Model in a vendor trailer that the default
    # header-only read doesn't reach. Trailer parsing requires -ee, which
    # confuses pyexiftool's stay-open mode on large files (.insv routinely
    # 9GB), so call exiftool as a one-shot subprocess instead.
    # Insta360 stores Make/Model only in a vendor trailer at the end of
    # the file, which exiftool -ee can read but takes 10–20 s on a 9 GB
    # .insv (it parses the whole accelerometer stream to get there).
    # The model is per-camera, not per-file, so we sample exactly one
    # .insv per trip and cache the result in .audit/insta360-camera.json.
    # First run on a trip ≈ 12 s; every later run is instant.
    insta_files = [f for f in files if f.suffix.lower() in (".insv", ".lrv", ".insp")]
    if insta_files:
        import json
        import subprocess
        from .state import AUDIT_DIR
        cache_path = folder / AUDIT_DIR / "insta360-camera.json"
        camera: dict | None = None
        if cache_path.is_file():
            try:
                camera = json.loads(cache_path.read_text())
            except Exception:
                camera = None
        if not camera or not camera.get("model"):
            # Sample smallest .insv first (parse cost grows with size),
            # but try the next-smallest if the file lacks a trailer
            # (truncated / malformed). Cap attempts so a corrupt trip
            # doesn't grind through every file.
            sampled = sorted(
                (f for f in insta_files if f.suffix.lower() == ".insv"),
                key=lambda p: p.stat().st_size,
            )
            t1 = time.monotonic()
            camera = {}
            attempts = 0
            for sample in sampled[:8]:
                attempts += 1
                sys.stderr.write(
                    f"  detecting Insta360 camera from {sample.name} "
                    f"({sample.stat().st_size // (1024*1024)} MB)…\n"
                )
                sys.stderr.flush()
                try:
                    result = subprocess.run(
                        ["exiftool", "-G", "-n", "-m", "-ee", "-j",
                         "-Trailer:Model", "-Trailer:SerialNumber", str(sample)],
                        capture_output=True, text=True, check=False,
                    )
                    blobs = json.loads(result.stdout) if result.stdout.strip() else []
                except Exception:
                    blobs = []
                model = blobs[0].get("Trailer:Model") if blobs else None
                if model:
                    camera = {
                        "make": "Insta360",
                        "model": model,
                        "serial": blobs[0].get("Trailer:SerialNumber"),
                        "sampled_from": sample.name,
                    }
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_text(json.dumps(camera, indent=2))
                    break
                sys.stderr.write(
                    f"    no trailer in {sample.name}; trying next…\n"
                )
                sys.stderr.flush()
            sys.stderr.write(
                f"  Insta360 detect done in {time.monotonic() - t1:.1f}s "
                f"after {attempts} attempt(s) "
                f"(model: {camera.get('model') or 'unknown'})\n"
            )
            sys.stderr.flush()
        # When cache is hit, stay silent — read_folder is called many
        # times per audit (once per apply-pass) and the message becomes
        # noisy.

        if camera and camera.get("model"):
            for f in insta_files:
                raw = by_path.get(f)
                if raw is None:
                    continue
                if "EXIF:Model" in raw or "QuickTime:Model" in raw:
                    continue
                raw["QuickTime:Model"] = camera["model"]
                raw.setdefault("QuickTime:Make", camera.get("make", "Insta360"))

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
    sys.stderr.write(
        f"  read {len(rows)} file(s) in {time.monotonic() - t_total:.1f}s\n"
    )
    sys.stderr.flush()
    return rows
