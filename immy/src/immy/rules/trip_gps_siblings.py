"""Backfill GPS on position-less media from the trip's own geotagged
siblings, located by capture time.

The common case this solves: a trip where most frames carry GPS (phone,
drone with SRT, GPS-enabled camera) but a handful — a 360 cam, a
second body, a few exports — don't. Rather than pin those to a single
folder-wide anchor (`trip-gps-anchor`, which drops every gap on the trip
centroid), locate each gap from the frames that *do* have GPS.

Unlike `geotag-from-gpx`, both sides come from the same camera clock
domain (naive local EXIF time within one folder), so no timezone
alignment is needed — we match capture time against capture time
directly. Files dated only by mtime are skipped on both sides: an mtime
is not a capture time and would match nonsense. Null-island (0,0) coords
count as "no fix" on both sides (see `has_valid_gps`).

WHY NOT A FLAT TIME THRESHOLD: time gap is only a proxy for distance
*while moving*. A 90-min gap during a stationary session (a campsite, a
360-cam burst) is zero error; a 5-min gap on a highway is kilometres. So
instead of one cutoff we look at the two source fixes that *bracket* the
target in time:

- HIGH   — the nearest bracketing fix is within 5 min. Essentially
           coincident; as trustworthy as a GPX match. Auto-applied.
- MEDIUM — either the bracket is spatially tight (≤2 km apart ⇒ the
           camera was parked, so any in-bracket gap is fine), or the
           target sits between two fixes <90 min from one of them and we
           linear-interpolate along the path. Surfaced for review,
           clustered per camera-day so a whole burst is one y/n.
- skip   — bracket spans a multi-hour void, or no fix within reach.

Per-field dedup (registration order) makes this lose to the more specific
`dji-gps-from-srt` / `geotag-from-gpx` and win over the folder-wide
`trip-gps-anchor`.
"""

from __future__ import annotations

from bisect import bisect_left
from math import asin, cos, radians, sin, sqrt
from pathlib import Path

from ..dates import resolve as resolve_date
from ..exif import ExifRow, has_valid_gps
from .registry import Finding, Rule, register

HIGH_SECONDS = 5 * 60
MEDIUM_SECONDS = 90 * 60
# Don't bracket across a gap this wide — two fixes hours apart say nothing
# about a point between them, even if they happen to be near each other.
MAX_BRACKET_SECONDS = 6 * 3600
# Bracket endpoints this close ⇒ the camera didn't move; the in-bracket
# time gap is irrelevant and we trust the location regardless.
LOCAL_AREA_KM = 2.0
# Need a real spread of source points before trusting nearest-in-time; a
# couple of geotagged frames is not a track.
MIN_SOURCE_POINTS = 5


def _km(a_lat: float, a_lon: float, b_lat: float, b_lon: float) -> float:
    r = 6371.0
    dlat, dlon = radians(b_lat - a_lat), radians(b_lon - a_lon)
    h = sin(dlat / 2) ** 2 + cos(radians(a_lat)) * cos(radians(b_lat)) * sin(dlon / 2) ** 2
    return 2 * r * asin(sqrt(h))


def _coords(row: ExifRow) -> tuple[float, float] | None:
    lat = row.get("Composite:GPSLatitude", "EXIF:GPSLatitude", "XMP:GPSLatitude")
    lon = row.get("Composite:GPSLongitude", "EXIF:GPSLongitude", "XMP:GPSLongitude")
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _capture_dt(row: ExifRow):
    authority = resolve_date(row)
    if authority is None or authority.source == "mtime":
        return None
    return authority.dt


def _camera_day(row: ExifRow, dt) -> str:
    model = row.get("EXIF:Model", "QuickTime:Model", "XMP:Model",
                    "EXIF:Make", "QuickTime:Make")
    cam = str(model).strip() if model else "cam"
    return f"siblings-gps:{cam}:{dt.date().isoformat()}"


def _interpolate(before, after, t) -> tuple[float, float]:
    span = (after[0] - before[0]).total_seconds()
    if span <= 0:
        return before[1], before[2]
    frac = (t - before[0]).total_seconds() / span
    lat = before[1] + frac * (after[1] - before[1])
    lon = before[2] + frac * (after[2] - before[2])
    return lat, lon


def _patch(lat: float, lon: float) -> dict[str, str]:
    return {
        "GPSLatitude": f"{lat:.6f}",
        "GPSLatitudeRef": "N" if lat >= 0 else "S",
        "GPSLongitude": f"{lon:.6f}",
        "GPSLongitudeRef": "E" if lon >= 0 else "W",
    }


def _locate(points, t):
    """Return (lat, lon, confidence, basis) for capture time `t`, or None
    if no source fix is close enough to trust. `points` is sorted by dt."""
    i = bisect_left([p[0] for p in points], t)
    before = points[i - 1] if i > 0 else None
    after = points[i] if i < len(points) else None

    if before and after:
        nearest_gap = min((t - before[0]).total_seconds(),
                          (after[0] - t).total_seconds())
        span = (after[0] - before[0]).total_seconds()
        if span <= MAX_BRACKET_SECONDS:
            d_km = _km(before[1], before[2], after[1], after[2])
            if nearest_gap <= HIGH_SECONDS:
                # Coincident with a real fix → snap to that endpoint's actual
                # coords, never the interpolated point: on a spatially wide
                # bracket, interpolating would inject km of error into a tier
                # we auto-apply.
                near = before if (t - before[0]) <= (after[0] - t) else after
                return near[1], near[2], "high", f"nearest fix Δ{int(nearest_gap // 60)}m"
            lat, lon = _interpolate(before, after, t)
            if d_km <= LOCAL_AREA_KM:
                return lat, lon, "medium", (
                    f"stationary bracket {d_km:.1f}km over {int(span // 60)}m")
            if nearest_gap <= MEDIUM_SECONDS:
                return lat, lon, "medium", (
                    f"interpolated, {d_km:.1f}km bracket, Δ{int(nearest_gap // 60)}m")
            return None
        # Bracket straddles a void — fall through to nearest-only.

    best = None
    if before and after:
        best = before if (t - before[0]) <= (after[0] - t) else after
    else:
        best = before or after
    if best is None:
        return None
    delta = abs((best[0] - t).total_seconds())
    if delta <= HIGH_SECONDS:
        conf = "high"
    elif delta <= MEDIUM_SECONDS:
        conf = "medium"
    else:
        return None
    return best[1], best[2], conf, f"nearest fix Δ{int(delta // 60)}m (no bracket)"


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    points: list[tuple] = []  # (dt, lat, lon), sorted by dt
    for row in rows:
        if not has_valid_gps(row):
            continue
        coords = _coords(row)
        dt = _capture_dt(row)
        if coords is None or dt is None:
            continue
        points.append((dt, coords[0], coords[1]))
    if len(points) < MIN_SOURCE_POINTS:
        return []
    points.sort(key=lambda p: p[0])

    out: list[Finding] = []
    for row in rows:
        if has_valid_gps(row):
            continue
        dt = _capture_dt(row)
        if dt is None:
            continue
        located = _locate(points, dt)
        if located is None:
            continue
        lat, lon, confidence, basis = located
        out.append(Finding(
            rule="trip-gps-from-siblings",
            confidence=confidence,
            path=row.path,
            action="write_xmp",
            patch=_patch(lat, lon),
            reason=f"{basis} ({lat:+.4f}, {lon:+.4f})",
            # Cluster MEDIUM findings per camera-day so a whole burst is a
            # single y/n instead of one prompt per frame. HIGH is
            # auto-applied and ignores the group.
            group=_camera_day(row, dt) if confidence == "medium" else "",
        ))
    return out


# Rule-level confidence is documentary only (the CLI tiers on each
# Finding.confidence); this rule emits both high and medium.
register(Rule(name="trip-gps-from-siblings", confidence="medium", propose=_propose))
