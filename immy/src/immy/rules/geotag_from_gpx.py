"""Time-match GPS-less media against any `.gpx` track in the trip folder.

Common case: user carries a phone or GPS logger that emits GPX while
the camera doesn't record position. Drop the `.gpx` next to the photos
and every frame within 5 min of a track point gets its coords written
to XMP. Same semantics as `exiftool -geotag`, just surfaced as a
finding so the state/log pipeline tracks it.

HIGH because GPX is an explicit signal — the user put that file there
on purpose, and timestamp-nearest is a well-understood matcher. Loses
the per-field dedup to `dji-gps-from-srt` (more specific per-frame
data) but wins over `trip-gps-anchor` (folder-wide constant).

Timezone alignment: GPX times are UTC (spec). EXIF dates are usually
naive local time. To align, we need an offset per image:

1. `EXIF:OffsetTimeOriginal` wins (iPhone, modern mirrorless).
2. A `±HH:MM` suffix on the XMP date string (written by `trip-timezone`).
3. `timezone:` in folder notes → treat naive EXIF as that zone.
4. Nothing → skip the file, don't guess.

No file outside the track window gets flagged. A single GPX that
covers 2 h of a trip won't spuriously tag photos from the other day.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..dates import resolve as resolve_date
from ..exif import ExifRow, has_gps
from ..notes import parse_frontmatter, resolve as resolve_notes
from .registry import Finding, Rule, register


THRESHOLD_SECONDS = 5 * 60

_GPX_NS = {"gpx": "http://www.topografix.com/GPX/1/1",
           "gpx10": "http://www.topografix.com/GPX/1/0"}


def _parse_gpx_time(raw: str) -> datetime | None:
    if not raw:
        return None
    raw = raw.strip()
    # Handle "Z" and "+00:00"/fractional seconds.
    base = raw.rstrip("Z")
    if "." in base:
        base = base.split(".")[0]
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            dt = datetime.strptime(base, fmt)
        except ValueError:
            continue
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    return None


def _parse_gpx(path: Path) -> list[tuple[datetime, float, float]]:
    try:
        tree = ET.parse(path)
    except ET.ParseError:
        return []
    points: list[tuple[datetime, float, float]] = []
    # Try the two common GPX namespaces; then fall back to tag-name match
    # for files written without a namespace declaration.
    matched_any = False
    for ns_prefix, ns_uri in _GPX_NS.items():
        for trkpt in tree.iterfind(f".//{{{ns_uri}}}trkpt"):
            matched_any = True
            t = trkpt.find(f"{{{ns_uri}}}time")
            ts = _parse_gpx_time(t.text) if t is not None and t.text else None
            lat, lon = trkpt.get("lat"), trkpt.get("lon")
            if ts is None or lat is None or lon is None:
                continue
            try:
                points.append((ts, float(lat), float(lon)))
            except ValueError:
                continue
    if not matched_any:
        for trkpt in tree.iterfind(".//trkpt"):
            t = trkpt.find("time")
            ts = _parse_gpx_time(t.text) if t is not None and t.text else None
            lat, lon = trkpt.get("lat"), trkpt.get("lon")
            if ts is None or lat is None or lon is None:
                continue
            try:
                points.append((ts, float(lat), float(lon)))
            except ValueError:
                continue
    return sorted(points)


def _parse_offset(s: str) -> timezone | None:
    # "+04:00" / "-05:30"
    if len(s) != 6 or s[0] not in "+-" or s[3] != ":":
        return None
    try:
        sign = 1 if s[0] == "+" else -1
        h, m = int(s[1:3]), int(s[4:6])
    except ValueError:
        return None
    return timezone(sign * timedelta(hours=h, minutes=m))


def _row_tz(row: ExifRow, notes_tz: ZoneInfo | None) -> object | None:
    """Return whatever tzinfo to attach to a naive EXIF datetime for this
    row. `tzinfo`-compatible objects only (ZoneInfo or fixed-offset
    timezone)."""
    raw_offset = row.get("EXIF:OffsetTimeOriginal", "XMP:OffsetTimeOriginal")
    if isinstance(raw_offset, str):
        tz = _parse_offset(raw_offset.strip())
        if tz is not None:
            return tz
    raw_dt = row.get("XMP:DateTimeOriginal", "EXIF:DateTimeOriginal", "QuickTime:CreateDate")
    if isinstance(raw_dt, str) and len(raw_dt) >= 25:
        tz = _parse_offset(raw_dt[-6:])
        if tz is not None:
            return tz
    return notes_tz


def _nearest(points: list[tuple[datetime, float, float]], target: datetime):
    # Folders are small; linear scan is fine. Upgrade to bisect if a real
    # audit ever times out here.
    best = min(points, key=lambda p: abs((p[0] - target).total_seconds()))
    delta = abs((best[0] - target).total_seconds())
    return best, delta


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    gpx_files = list(folder.rglob("*.gpx"))
    if not gpx_files:
        return []
    points: list[tuple[datetime, float, float]] = []
    for g in gpx_files:
        points.extend(_parse_gpx(g))
    if not points:
        return []

    notes_tz: ZoneInfo | None = None
    notes_path = resolve_notes(folder)
    if notes_path is not None:
        fm = parse_frontmatter(notes_path)
        tz_name = fm.get("timezone")
        if isinstance(tz_name, str) and tz_name.strip():
            try:
                notes_tz = ZoneInfo(tz_name.strip())
            except ZoneInfoNotFoundError:
                notes_tz = None

    out: list[Finding] = []
    for row in rows:
        if has_gps(row):
            continue
        authority = resolve_date(row)
        if authority is None or authority.source == "mtime":
            continue
        tz = _row_tz(row, notes_tz)
        if tz is None:
            continue
        dt_aware = authority.dt.replace(tzinfo=tz)
        dt_utc = dt_aware.astimezone(timezone.utc)
        best, delta = _nearest(points, dt_utc)
        if delta > THRESHOLD_SECONDS:
            continue
        _, lat, lon = best
        patch = {
            "GPSLatitude": f"{lat:.6f}",
            "GPSLatitudeRef": "N" if lat >= 0 else "S",
            "GPSLongitude": f"{lon:.6f}",
            "GPSLongitudeRef": "E" if lon >= 0 else "W",
        }
        out.append(Finding(
            rule="geotag-from-gpx",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch=patch,
            reason=f"nearest GPX track point Δ{int(delta)}s ({lat:+.4f}, {lon:+.4f})",
        ))
    return out


register(Rule(name="geotag-from-gpx", confidence="high", propose=_propose))
