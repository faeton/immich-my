"""`photos` source adapter — batches exported from Apple Photos by osxphotos.

A batch is a directory tree of exported files plus osxphotos' JSON export
report (`osxphotos export … --report <batch>/osxphotos-report.json`). The
report is the only place osxphotos writes the Photos UUID per exported file
(checked against osxphotos 0.77.1: `--sidecar json` is exiftool-format
metadata with no UUID; the CSV report drops it). Each record looks like

    {"filename": "/Users/…/export/2026/07/IMG_1742.HEIC", "exported": true,
     "uuid": "8A7C…", "missing": false, "error": "", …}

`filename` is the path on the Mac, so staged files are matched by their path
relative to the batch root being a suffix of it (unique suffix required).

Component is inferred from the exported name, per osxphotos' defaults:
RAW extension → `raw`; `_edited` / `-edited` stem → `edited`; a video whose
stem matches an image of the same UUID → `live_video`; otherwise `original`.

The optional exiftool-format JSON sidecar (`--sidecar json`, `<name>.<ext>.json`)
carries the Photos library's own date and location, which reflect edits made
in Photos that the original's EXIF does not. When it disagrees with (or fills
in for) the file's EXIF, it wins and `taken_src` becomes `json` — which is
what makes `_rescue_sidecar` write it back out for Immich at promote time.
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from functools import lru_cache
from pathlib import Path

REPORT_NAME = "osxphotos-report.json"

from .engine import RAW_EXTS

LIVE_VIDEO_EXTS = {"mov", "mp4", "m4v"}
_EDITED_SUFFIXES = ("_edited", "-edited")


def find_report(path: Path) -> Path | None:
    """The nearest ancestor directory's `osxphotos-report.json`."""
    for parent in path.parents:
        candidate = parent / REPORT_NAME
        if candidate.is_file():
            return candidate
    return None


@lru_cache(maxsize=16)
def _load_report(report: str, mtime_ns: int) -> dict[str, list[str]]:
    """uuid-bearing records, indexed by basename → [mac filename, uuid] pairs
    flattened as "filename\\0uuid". Cached per (path, mtime) so one batch's
    report is parsed once per fingerprint pass."""
    try:
        records = json.loads(Path(report).read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    index: dict[str, list[str]] = {}
    for rec in records if isinstance(records, list) else []:
        if not isinstance(rec, dict):
            continue
        name, uuid = rec.get("filename"), rec.get("uuid")
        if not name or not uuid or rec.get("missing") or rec.get("error"):
            continue
        index.setdefault(os.path.basename(name), []).append(f"{name}\0{uuid}")
    return index


def uuid_for(path: Path) -> str | None:
    """The Photos UUID osxphotos recorded for this staged file, or None."""
    report = find_report(path)
    if report is None:
        return None
    index = _load_report(str(report), report.stat().st_mtime_ns)
    rel = path.relative_to(report.parent).as_posix()
    hits = {
        entry.split("\0", 1)[1]
        for entry in index.get(path.name, [])
        if entry.split("\0", 1)[0].replace("\\", "/").endswith("/" + rel)
    }
    return hits.pop() if len(hits) == 1 else None


def component_for(path: Path, uuid: str) -> str:
    ext = path.suffix.lower().lstrip(".")
    if ext in RAW_EXTS:
        return "raw"
    if path.stem.lower().endswith(_EDITED_SUFFIXES):
        return "edited"
    if ext in LIVE_VIDEO_EXTS:
        # A Live Photo's video half: same stem, an image beside it, same UUID.
        for sibling in path.parent.glob(path.stem + ".*"):
            if sibling != path and sibling.suffix.lower().lstrip(".") not in LIVE_VIDEO_EXTS | {"json", "xmp", "aae"}:
                if uuid_for(sibling) == uuid:
                    return "live_video"
    return "original"


def _sidecar(path: Path) -> dict | None:
    candidate = path.with_name(path.name + ".json")
    if not candidate.is_file():
        return None
    try:
        data = json.loads(candidate.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(data, list):
        data = data[0] if data and isinstance(data[0], dict) else None
    return data if isinstance(data, dict) else None


def _parse_exif_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()[:19]
    try:
        return datetime.strptime(text, "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None


# Photos stores its own rounded copy of the location: on a real 2026-10
# export, 49 of 50 sidecars differed from the file's EXIF by ~5e-7° (~6 cm),
# and an exact compare turned every one into a "correction" — an XMP that
# overrides good camera EXIF at promote. A location edit in Photos moves
# metres at least; 1e-5° is ~1 m.
_GPS_TOLERANCE_DEG = 1e-5


# Ray-Ban Meta exports: Photos' date runs 1–2 s ahead of the file's EXIF
# (7 of 50 on the first real batch). A date edit in Photos moves minutes
# or hours, not seconds.
_DATE_TOLERANCE_S = 2


def _same_wall_time(when: datetime, taken_at: object) -> bool:
    """The JSON date is whole seconds, local wall clock. EXIF-derived
    `taken_at` may carry sub-seconds (and, defensively, an offset): compare
    wall-clock time within `_DATE_TOLERANCE_S`.

    Videos legitimately differ by hours: `dates.resolve` takes the UTC
    `QuickTime:CreateDate` as wall clock, while the sidecar's
    `QuickTime:CreationDate` is the true local time — so the sidecar wins."""
    if not isinstance(taken_at, str):
        return False
    try:
        exif = datetime.fromisoformat(taken_at)
    except ValueError:
        return False
    delta = exif.replace(tzinfo=None) - when
    return abs(delta.total_seconds()) < _DATE_TOLERANCE_S + 1


def _same_place(lat: float, lon: float, cur_lat: object, cur_lon: object) -> bool:
    if not isinstance(cur_lat, (int, float)) or not isinstance(cur_lon, (int, float)):
        return False
    return (abs(lat - cur_lat) <= _GPS_TOLERANCE_DEG
            and abs(lon - cur_lon) <= _GPS_TOLERANCE_DEG)


def companion_fields(path: Path, fields: dict) -> dict:
    """Overlay the Photos library's own identity, date and location onto
    `fingerprint_fields`' output for one staged `photos` file. Returns a new
    dict; `fields` is not modified."""
    out = dict(fields)
    uuid = uuid_for(path)
    if uuid:
        out["source_uid"] = uuid
        out["component"] = component_for(path, uuid)
    sidecar = _sidecar(path)
    if not sidecar:
        return out
    when = _parse_exif_datetime(
        sidecar.get("EXIF:DateTimeOriginal") or sidecar.get("QuickTime:CreationDate")
    )
    if when is not None and not _same_wall_time(when, out.get("taken_at")):
        out["taken_at"], out["taken_src"] = when.isoformat(), "json"
    lat, lon = sidecar.get("EXIF:GPSLatitude"), sidecar.get("EXIF:GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        if sidecar.get("EXIF:GPSLatitudeRef") == "S":
            lat = -abs(lat)
        if sidecar.get("EXIF:GPSLongitudeRef") == "W":
            lon = -abs(lon)
        if not (abs(lat) < 1e-3 and abs(lon) < 1e-3):
            if not _same_place(lat, lon, out.get("gps_lat"), out.get("gps_lon")):
                # `taken_src='json'` means "the companion JSON corrected date or
                # location" (as for Takeout) — it is what makes promote write
                # the correction out as an XMP sidecar Immich reads.
                out["gps_lat"], out["gps_lon"] = float(lat), float(lon)
                out["taken_src"] = "json"
    return out
