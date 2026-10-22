"""Place shots that have no GPS from your own geotagged shots around them.

The case: Meta glasses have no GPS; the Meta AI app used to write the
phone's position into each photo and video, and stopped on 2026-02-26
(nothing in Photos.app either). The phone is in your pocket, so the iPhone
shots around a glasses shot place it. `trip_gps_siblings` does this inside
one trip folder from local files; this does it library-wide from Immich's
own table, with the same confidence policy (`_locate`).

Which clock to match on:
- a video knows its instant (Meta: `CreationDate` with `Z`), so it is matched
  on the instant;
- a photo knows only its wall clock (EXIF, no offset; Immich shows it with no
  zone), the phone's own clock, so it is matched on wall clock.

A photo with no zone also gets its offset, from the shot that placed it:
Immich stores that shot's wall clock (`localDateTime`) and instant
(`fileCreatedAt`), and their difference is the offset there and then. The
two shots bracketing it must agree, so a border or a time-zone change in
between leaves the date alone.
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .rules.trip_gps_siblings import _locate

# Shots that place a target: own GPS (null island is no fix) and a known
# zone, so their wall clock and offset mean something.
POINTS_SQL = """
SELECT a."localDateTime", a."fileCreatedAt", e.latitude, e.longitude
FROM asset a JOIN asset_exif e ON e."assetId" = a.id
WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL
  AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
  AND NOT (abs(e.latitude) < 0.001 AND abs(e.longitude) < 0.001)
  AND e."timeZone" IS NOT NULL AND e."timeZone" NOT IN ('UTC', 'UTC+0', 'Etc/UTC')
  AND a."fileCreatedAt" BETWEEN %(since)s - interval '1 day' AND %(until)s + interval '1 day'
"""

# Shots to place: no GPS, from these camera models.
TARGETS_SQL = """
SELECT a.id, a."originalPath", a.type, a."localDateTime", a."fileCreatedAt", e."timeZone",
       (SELECT f.path FROM asset_file f WHERE f."assetId" = a.id AND f.type = 'sidecar')
FROM asset a JOIN asset_exif e ON e."assetId" = a.id
WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL
  AND e.latitude IS NULL AND e.model ILIKE ANY(%(models)s)
  AND a."fileCreatedAt" BETWEEN %(since)s AND %(until)s
"""

_NO_ZONE = (None, "", "UTC", "UTC+0", "Etc/UTC")

# Library-wide, `_locate`'s medium tier reaches too far: interpolating across a
# flight (brackets of 1,000+ km) or taking the nearest fix 90 min away while
# moving. Kept: a still bracket, interpolation over ≤ 25 km, nearest ≤ 20 min.
# On the 2026 Meta backlog this held back 239 of 1,655 placements.
MAX_INTERPOLATED_KM = 25.0
MAX_NEAREST_ONLY_S = 20 * 60


@dataclass(frozen=True)
class Point:
    wall: datetime          # naive wall clock
    instant: datetime       # aware
    lat: float
    lon: float

    @property
    def offset(self) -> timedelta:
        return self.wall - self.instant.astimezone(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class Target:
    id: str
    path: str
    video: bool
    wall: datetime          # naive (Immich's localDateTime)
    instant: datetime       # aware (fileCreatedAt)
    zone: str | None
    sidecar: str | None


@dataclass
class Placement:
    target: Target
    lat: float
    lon: float
    confidence: str         # "high" | "medium"
    basis: str
    dto: str | None         # new DateTimeOriginal (with offset), or None to keep


def point(local_dt: datetime, instant: datetime, lat: float, lon: float) -> Point:
    """From Immich's columns: `localDateTime` is the wall clock stored as if UTC."""
    return Point(local_dt.astimezone(timezone.utc).replace(tzinfo=None) if local_dt.tzinfo else local_dt,
                 instant if instant.tzinfo else instant.replace(tzinfo=timezone.utc), lat, lon)


def _fmt(wall: datetime, offset: timedelta) -> str:
    mins = int(offset.total_seconds() // 60)
    sign = "-" if mins < 0 else "+"
    return f"{wall.strftime('%Y:%m:%d %H:%M:%S')}{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def _bracket(keyed: list[tuple[datetime, Point]], t: datetime) -> list[Point]:
    i = bisect_left([k for k, _ in keyed], t)
    return [p for _, p in keyed[max(0, i - 1):i + 1]]


def place(targets: list[Target], points: list[Point]) -> list[Placement]:
    """Where each target was, and (when it has no zone) its date with the
    offset there. Targets nothing places within `_locate`'s reach are left out."""
    by_wall = sorted(((p.wall, p) for p in points), key=lambda kp: kp[0])
    by_instant = sorted(((p.instant.astimezone(timezone.utc).replace(tzinfo=None), p) for p in points),
                        key=lambda kp: kp[0])
    track_wall = [(k, p.lat, p.lon) for k, p in by_wall]
    track_instant = [(k, p.lat, p.lon) for k, p in by_instant]
    out = []
    for t in targets:
        # A photo with no zone only knows its wall clock; anything else knows its instant.
        on_wall = not t.video and t.zone in _NO_ZONE
        key = t.wall if on_wall else t.instant.astimezone(timezone.utc).replace(tzinfo=None)
        found = _locate(track_wall if on_wall else track_instant, key)
        if found is None:
            continue
        lat, lon, confidence, basis = found
        if not _close_enough(basis):
            continue
        dto = None
        if t.zone in _NO_ZONE:
            offsets = {p.offset for p in _bracket(by_wall if on_wall else by_instant, key)}
            if len(offsets) == 1:
                off = offsets.pop()
                wall = t.wall if on_wall else (key + off)
                dto = _fmt(wall, off)
        out.append(Placement(t, lat, lon, confidence, basis, dto))
    return out


def _close_enough(basis: str) -> bool:
    """`_locate`'s basis text says how a medium placement was made."""
    import re
    if "interpolated" in basis:
        m = re.search(r"([\d.]+)km", basis)
        return m is not None and float(m.group(1)) <= MAX_INTERPOLATED_KM
    if "no bracket" in basis:
        m = re.search(r"Δ(\d+)m", basis)
        return m is not None and int(m.group(1)) * 60 <= MAX_NEAREST_ONLY_S
    return True


def patch(p: Placement) -> dict[str, object]:
    """Signed decimal degrees: exiftool writes XMP's `4,38.75S` form."""
    out: dict[str, object] = {"GPSLatitude": f"{p.lat:.6f}", "GPSLongitude": f"{p.lon:.6f}"}
    if p.dto:
        out["DateTimeOriginal"] = p.dto
    return out
