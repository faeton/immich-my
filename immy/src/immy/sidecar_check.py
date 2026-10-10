"""Check an XMP sidecar against the original it describes, and correct it.

A sidecar overrides the file: Immich reads a registered sidecar's
`DateTimeOriginal` and GPS over the original's own. So a wrong sidecar
silently breaks a good file. Two bugs did exactly that (fixed in promote
2026-10-02, but the sidecars they wrote stayed):

- **Hemisphere dropped.** GPS written as `abs()`: Fiji (−17.8, 177.4) became
  17.8 N, Las Vegas (36.1, −115.2) became 115.2 E in China. The time zone
  Immich derives from the position is then wrong too.
- **UTC clock, no offset.** A video's QuickTime `CreateDate` is UTC. Written
  to the sidecar without an offset, Immich reads it as local wall time: a
  Las Vegas clip at 15:08 shows 23:08; a French Polynesia one lands on the
  next day.

`plan()` is the single rule set. Promote applies it before writing a sidecar
(`correct_patch`), and `immy sidecars check` applies it to every registered
sidecar in the library. What it trusts, in order: the original's own GPS and
its own dated-with-offset capture time; then, for what the file lacks, the
user's other shots around the same instant (a position that is the mirror
image of theirs is a dropped sign; their time zone dates a bare UTC clock).
A sidecar value that differs from the file for real (a location edited in
Photos) is not a bug and is left alone.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Same position, as far as GPS rounding goes (~10 m).
_SAME_DEG = 1e-4
# A sidecar date this close to the true local time is that time.
_CLOCK_SLACK_S = 90
# Mirror test against the user's other shots: the stored point is far from
# them and its mirror image is close.
MIRROR_FAR_KM = 300
MIRROR_NEAR_KM = 60
VIDEO_EXTS = frozenset({"mov", "mp4", "m4v", "insv", "lrv", "3gp", "mts", "avi"})


@dataclass(frozen=True)
class FileFacts:
    """What an original says about itself (exiftool `-j -n`)."""

    lat: float | None = None
    lon: float | None = None
    local: datetime | None = None   # capture time with its real offset
    utc: datetime | None = None     # capture instant known only in UTC
    video: bool = False
    read: bool = True               # False: exiftool couldn't read the file
    wall: datetime | None = None    # a still's DateTimeOriginal with no offset


@dataclass(frozen=True)
class SidecarFacts:
    dto: str | None = None          # DateTimeOriginal as written
    lat: float | None = None
    lon: float | None = None


@dataclass(frozen=True)
class Hint:
    """The user's other shots around this one (Immich DB): the nearest one in
    time with GPS of its own, and the time zone most of them carry."""

    lat: float | None = None
    lon: float | None = None
    zone: object | None = None      # tzinfo


@dataclass
class Repair:
    patch: dict[str, object] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


# --- reading --------------------------------------------------------------------


def _parse_dt(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    s = value.strip().replace("Z", "+00:00")
    m = re.match(r"(\d{4})[:-](\d\d)[:-](\d\d)[ T](\d\d):(\d\d):(\d\d)(?:\.\d+)?([+-]\d\d:?\d\d)?$", s)
    if not m:
        return None
    y, mo, d, h, mi, se, off = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(se))
    except ValueError:
        return None
    if off:
        sign = -1 if off[0] == "-" else 1
        hh, mm = int(off[1:3]), int(off[-2:])
        dt = dt.replace(tzinfo=timezone(sign * timedelta(hours=hh, minutes=mm)))
    return dt


def _num(v) -> float | None:
    return float(v) if isinstance(v, (int, float)) else None


def file_facts(exif: dict, path: str) -> FileFacts:
    """From one exiftool `-j -n` record of the original. Tags used:
    GPSLatitude/GPSLongitude (signed with -n), DateTimeOriginal +
    OffsetTimeOriginal (stills), CreationDate (QuickTime Keys: local with
    offset on Apple, `Z` UTC on Ray-Ban Meta), CreateDate (QuickTime: UTC)."""
    if not exif:
        return FileFacts(read=False)
    video = path.rsplit(".", 1)[-1].lower() in VIDEO_EXTS
    lat, lon = _num(exif.get("GPSLatitude")), _num(exif.get("GPSLongitude"))
    if lat is not None and lon is not None and abs(lat) < 1e-3 and abs(lon) < 1e-3:
        lat = lon = None                                     # null island: no fix
    local = utc = None
    cd = exif.get("CreationDate")
    if isinstance(cd, str):
        dt = _parse_dt(cd)
        if dt is not None and dt.tzinfo is not None:
            if cd.strip().endswith("Z"):
                utc = dt                                     # Meta: UTC, not local
            else:
                local = dt
    wall = None
    if local is None:
        dto = _parse_dt(exif.get("DateTimeOriginal"))
        off = exif.get("OffsetTimeOriginal")
        if dto is not None and dto.tzinfo is None and isinstance(off, str):
            dto = _parse_dt(dto.strftime("%Y:%m:%d %H:%M:%S") + off)
        if dto is not None and dto.tzinfo is not None:
            local = dto
        elif dto is not None:
            wall = dto
    if utc is None and video and not _local_clock_camera(exif, path):
        ca = _parse_dt(exif.get("CreateDate"))
        if ca is not None and ca.year > 1970:
            utc = ca.replace(tzinfo=timezone.utc) if ca.tzinfo is None else ca
    return FileFacts(lat, lon, local, utc, video, wall=wall)


def _local_clock_camera(exif: dict, path: str) -> bool:
    """Cameras whose QuickTime `CreateDate` is local wall time, not UTC
    (Insta360; `capture.QUICKTIME_LOCAL_CLOCK_MAKES`). Their sidecar dates
    are never read as a UTC clock."""
    from . import insta360
    from .capture import QUICKTIME_LOCAL_CLOCK_MAKES
    make = exif.get("Make")
    if isinstance(make, str) and make.lower().startswith(QUICKTIME_LOCAL_CLOCK_MAKES):
        return True
    return insta360.classify(Path(path)) is not None


_XMP_DTO = re.compile(r"<exif:DateTimeOriginal>([^<]+)<|exif:DateTimeOriginal=['\"]([^'\"]+)['\"]")
_XMP_GPS = r"<exif:{tag}>(\d+),([\d.]+)([NSEW])<|exif:{tag}=['\"](\d+),([\d.]+)([NSEW])['\"]"


def read_sidecar(text: str) -> SidecarFacts:
    """DateTimeOriginal and GPS from an XMP sidecar's text (exiftool writes
    GPS as `17,48.18S`; the letter is the hemisphere)."""
    m = _XMP_DTO.search(text)
    dto = (m.group(1) or m.group(2)) if m else None
    coords = []
    for tag in ("GPSLatitude", "GPSLongitude"):
        g = re.search(_XMP_GPS.format(tag=tag), text)
        if not g:
            coords.append(None)
            continue
        deg, mins, hemi = (g.group(1), g.group(2), g.group(3)) if g.group(1) else (g.group(4), g.group(5), g.group(6))
        v = int(deg) + float(mins) / 60
        coords.append(-v if hemi in "SW" else v)
    return SidecarFacts(dto, coords[0], coords[1])


# --- the rules ------------------------------------------------------------------


def km(lat1, lon1, lat2, lon2) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(min(1.0, math.sqrt(a)))


def _lost_signs(lat, lon) -> list[tuple[float, float]]:
    """What (lat, lon) was if `abs()` dropped a minus: only a positive value
    can have lost one."""
    out = []
    if lat > 0:
        out.append((-lat, lon))
    if lon > 0:
        out.append((lat, -lon))
    if lat > 0 and lon > 0:
        out.append((-lat, -lon))
    return out


def _mirror_of(lat, lon, ref_lat, ref_lon) -> tuple[float, float] | None:
    """The lost-sign version of (lat, lon) that lands near the reference,
    when (lat, lon) itself is far from it. Near the equator or a zero
    meridian the mirror is close to the point itself: nothing is decided
    there."""
    if km(lat, lon, ref_lat, ref_lon) < MIRROR_FAR_KM:
        return None
    candidates = [p for p in _lost_signs(lat, lon) if km(lat, lon, *p) >= MIRROR_FAR_KM]
    if not candidates:
        return None
    best = min(candidates, key=lambda p: km(p[0], p[1], ref_lat, ref_lon))
    return best if km(best[0], best[1], ref_lat, ref_lon) <= MIRROR_NEAR_KM else None


def _xmp_datetime(dt: datetime) -> str:
    stamp = dt.strftime("%Y:%m:%d %H:%M:%S")
    off = dt.utcoffset()
    if off is None:
        return stamp
    mins = int(off.total_seconds() // 60)
    sign = "-" if mins < 0 else "+"
    return f"{stamp}{sign}{abs(mins) // 60:02d}:{abs(mins) % 60:02d}"


def _zone_at(lat, lon):
    from .dedup.engine import zone_at
    return zone_at(lat, lon)


def plan(file: FileFacts, side: SidecarFacts, hint: Hint | None = None) -> Repair | None:
    """What the sidecar should say instead, or None when it is right (or
    when the original couldn't be read: no evidence, no change)."""
    if not file.read:
        return None
    r = Repair()
    lat, lon = side.lat, side.lon
    # Position.
    if lat is not None and lon is not None:
        if file.lat is not None and file.lon is not None:
            # The file's own point with its minus signs dropped, and far
            # enough from it to matter (not a metre-scale edit at 0°).
            lost = (abs(abs(file.lat) - lat) < _SAME_DEG and abs(abs(file.lon) - lon) < _SAME_DEG
                    and (file.lat < 0 or file.lon < 0))
            if lost and km(lat, lon, file.lat, file.lon) >= MIRROR_FAR_KM:
                lat, lon = file.lat, file.lon
                r.reasons.append("gps-sign")
        elif hint is not None and hint.lat is not None and hint.lon is not None:
            flipped = _mirror_of(lat, lon, hint.lat, hint.lon)
            if flipped is not None:
                lat, lon = flipped
                r.reasons.append("gps-sign")
        if r.reasons:
            r.patch.update({"GPSLatitude": lat, "GPSLatitudeRef": "N" if lat >= 0 else "S",
                            "GPSLongitude": lon, "GPSLongitudeRef": "E" if lon >= 0 else "W"})
    elif file.lat is not None:
        lat, lon = file.lat, file.lon
    # Clock. The bug's signature is exact: a date with no offset that is the
    # file's own UTC instant written as wall time. Anything else without an
    # offset is either right (a still's local time) or a deliberate fix (a
    # camera clock corrected in Photos) and is left alone.
    dt = _parse_dt(side.dto) if side.dto else None
    if (dt is not None and dt.tzinfo is None and file.utc is not None
            and abs((dt - file.utc.astimezone(timezone.utc).replace(tzinfo=None)).total_seconds()) < 3):
        if file.local is not None:
            # The file knows its own local time and offset.
            if abs((dt - file.local.replace(tzinfo=None)).total_seconds()) > _CLOCK_SLACK_S:
                r.patch["DateTimeOriginal"] = _xmp_datetime(file.local)
                r.reasons.append("clock")
        else:
            zone = (_zone_at(lat, lon) if lat is not None else None) or (hint.zone if hint else None)
            if zone is not None:
                local = file.utc.astimezone(zone)
                if local.utcoffset():
                    r.patch["DateTimeOriginal"] = _xmp_datetime(local)
                    r.reasons.append("clock-utc")
    return r if r.patch else None


def agrees(file: FileFacts, patch: dict[str, object]) -> bool:
    """Would a sidecar carrying `patch` still be right for this asset? For an
    asset sharing the sidecar with the one being repaired (a Live Photo's
    still and video): every changed field must match what this file says
    about itself, where it says anything."""
    if not file.read:
        return False
    new = _parse_dt(patch["DateTimeOriginal"]) if "DateTimeOriginal" in patch else None
    if new is not None:
        own = file.local or file.utc
        if new.tzinfo is None or (own is not None
                                  and abs((new - own).total_seconds()) > _CLOCK_SLACK_S):
            return False
        # A still that only knows its wall clock: the new local time must
        # read the same.
        if own is None and file.wall is not None and abs(
                (new.replace(tzinfo=None) - file.wall).total_seconds()) > _CLOCK_SLACK_S:
            return False
    if "GPSLatitude" in patch and file.lat is not None and file.lon is not None:
        if km(float(patch["GPSLatitude"]), float(patch["GPSLongitude"]), file.lat, file.lon) > 1:
            return False
    return True


def describes(file: FileFacts, side: SidecarFacts) -> bool | None:
    """Does this sidecar describe this file? True/False, or None when the
    file says nothing to compare with. A date with an offset is compared as
    an instant; one without, as the file's wall clock (or its UTC clock,
    the old bug's form)."""
    if not file.read:
        return None
    verdicts = []
    dt = _parse_dt(side.dto) if side.dto else None
    if dt is not None:
        if dt.tzinfo is not None:
            own = file.local or file.utc
            if own is not None:
                verdicts.append(abs((dt - own).total_seconds()) <= _CLOCK_SLACK_S)
        else:
            utc = file.utc or file.local
            clocks = [c for c in (file.local.replace(tzinfo=None) if file.local else None, file.wall,
                                  utc.astimezone(timezone.utc).replace(tzinfo=None) if utc else None)
                      if c is not None]
            if clocks:
                verdicts.append(any(abs((dt - c).total_seconds()) <= _CLOCK_SLACK_S for c in clocks))
    if side.lat is not None and file.lat is not None:
        same = km(side.lat, side.lon, file.lat, file.lon) <= 1
        lost = any(km(a, b, file.lat, file.lon) <= 1 for a, b in _lost_signs(side.lat, side.lon))
        verdicts.append(same or lost)
    return all(verdicts) if verdicts else None


def split_roles(side: SidecarFacts, files: list[tuple[str, FileFacts]]) -> dict[str, str] | None:
    """Assets sharing one sidecar by stem (`IMG_1.xmp` for both `IMG_1.HEIC`
    and an unrelated `IMG_1.MOV`): `asset → "copy"` for each one the sidecar
    describes (it gets its own copy), `"own"` for each it doesn't (it gets a
    sidecar of its own metadata). None unless every asset gives a clear
    answer and at least one is described."""
    verdict = {a: describes(f, side) for a, f in files}
    if any(v is None for v in verdict.values()) or not any(verdict.values()):
        return None
    if any(own_patch(f) is None for a, f in files if not verdict[a]):
        return None                     # nothing to give that file of its own
    return {a: "copy" if v else "own" for a, v in verdict.items()}


def own_patch(file: FileFacts, hint: Hint | None = None) -> dict[str, object] | None:
    """A sidecar that says only what this file says about itself, in a form
    Immich can't misread: the capture time with an explicit offset (a UTC
    instant goes out in the zone at its GPS or of the shots around it, else
    as `+00:00`), a still's offset-less wall clock as is, and GPS signed.
    None when the file says nothing (no sidecar can be made to agree)."""
    if not file.read:
        return None
    patch: dict[str, object] = {}
    if file.local is not None:
        patch["DateTimeOriginal"] = _xmp_datetime(file.local)
    elif file.utc is not None:
        zone = (_zone_at(file.lat, file.lon) if file.lat is not None else None) or (hint.zone if hint else None)
        patch["DateTimeOriginal"] = _xmp_datetime(file.utc.astimezone(zone or timezone.utc))
    elif file.wall is not None:
        patch["DateTimeOriginal"] = file.wall.strftime("%Y:%m:%d %H:%M:%S")
    if file.lat is not None and file.lon is not None:
        patch.update({"GPSLatitude": file.lat, "GPSLatitudeRef": "N" if file.lat >= 0 else "S",
                      "GPSLongitude": file.lon, "GPSLongitudeRef": "E" if file.lon >= 0 else "W"})
    return patch or None


def correct_patch(patch: dict[str, object], file: FileFacts) -> dict[str, object]:
    """Promote's guard: the sidecar `patch` it is about to write, run through
    the same rules against the original's own facts."""
    dto = patch.get("DateTimeOriginal")
    side = SidecarFacts(
        dto=dto if isinstance(dto, str) else None,
        lat=patch.get("GPSLatitude") if isinstance(patch.get("GPSLatitude"), (int, float)) else None,
        lon=patch.get("GPSLongitude") if isinstance(patch.get("GPSLongitude"), (int, float)) else None,
    )
    fix = plan(file, side)
    return {**patch, **fix.patch} if fix else patch


def zone_fix(file: FileFacts, shown: datetime, hint: Hint | None) -> dict[str, object] | None:
    """A video with no sidecar and no GPS that Immich shows on the UTC clock
    (`shown` is its wall time now): the true local time, from the file's own
    offset (Apple) or the zone the user's shots around it carry. None when
    the file's clock isn't a known UTC instant, or nothing gives a zone."""
    if not file.read or file.lat is not None or file.utc is None:
        return None
    utc = file.utc.astimezone(timezone.utc).replace(tzinfo=None)
    if abs((shown - utc).total_seconds()) > 3:
        return None                     # Immich isn't showing the UTC clock
    if file.local is not None:
        local = file.local
    elif hint is not None and hint.zone is not None:
        local = file.utc.astimezone(hint.zone)
    else:
        return None
    if not local.utcoffset():
        return None                     # UTC really is the local clock
    return {"DateTimeOriginal": _xmp_datetime(local)}


# --- the library (Immich DB) ----------------------------------------------------

# Registered sidecars of one owner's live assets.
SIDECARS_SQL = """
SELECT a.id, a."originalPath", f.path, a."fileCreatedAt"
FROM asset a JOIN asset_file f ON f."assetId" = a.id AND f.type = 'sidecar'
WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL
"""

# Videos with no sidecar and no position that Immich shows on the UTC clock.
UTC_CLOCK_SQL = """
SELECT a.id, a."originalPath", a."localDateTime" AT TIME ZONE 'UTC', a."fileCreatedAt"
FROM asset a JOIN asset_exif e ON e."assetId" = a.id
WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL AND a.type = 'VIDEO'
  AND e.latitude IS NULL
  AND coalesce(e."timeZone", 'UTC') IN ('UTC', 'UTC+0', 'Etc/UTC', 'UTC+00:00')
  AND NOT EXISTS (SELECT 1 FROM asset_file f WHERE f."assetId" = a.id AND f.type = 'sidecar')
"""

REGISTER_SIDECAR_SQL = """
INSERT INTO asset_file ("assetId", type, path) VALUES (%(asset)s, 'sidecar', %(path)s)
ON CONFLICT ("assetId", type, "isEdited") DO UPDATE SET path = EXCLUDED.path
"""

# Sidecar files more than one user's assets point at: repaired by nobody
# (each run sees one owner; a fix must be right for every asset it touches).
SHARED_OWNERS_SQL = """
SELECT f.path FROM asset_file f JOIN asset a ON a.id = f."assetId"
WHERE f.type = 'sidecar' AND a."deletedAt" IS NULL AND f.path = ANY(%(paths)s)
GROUP BY f.path HAVING count(DISTINCT a."ownerId") > 1
"""

# For each (asset, instant): the nearest-in-time other shot with its own GPS
# (no sidecar, so not suspect itself) within 14 h, and the time zone most
# shots within 3 h carry.
HINTS_SQL = """
WITH q AS (
  SELECT * FROM unnest(%(ids)s::uuid[], %(ts)s::timestamptz[]) AS q(id, t)
)
SELECT q.id, n.lat, n.lon, z.votes
FROM q
LEFT JOIN LATERAL (
  SELECT e.latitude AS lat, e.longitude AS lon
  FROM asset a JOIN asset_exif e ON e."assetId" = a.id
  WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL AND a.id <> q.id
    AND a."fileCreatedAt" BETWEEN q.t - interval '14 hours' AND q.t + interval '14 hours'
    AND e.latitude IS NOT NULL
    AND NOT (abs(e.latitude) < 0.001 AND abs(e.longitude) < 0.001)
    AND NOT EXISTS (SELECT 1 FROM asset_file s WHERE s."assetId" = a.id AND s.type = 'sidecar')
  ORDER BY abs(extract(epoch FROM a."fileCreatedAt" - q.t))
  LIMIT 1) n ON true
LEFT JOIN LATERAL (
  -- A shot votes when its zone is evidence: placed by its own GPS (so a
  -- UTC there is real, Lisbon in winter), or an offset from its own file.
  -- A bare UTC on a shot with no position only means "unknown". Shots with
  -- a sidecar vote once it has been checked; `exclude` holds the ones not
  -- verified yet (a mirrored GPS would vote for the mirror's zone).
  SELECT json_agg(json_build_array(v.tz, v.n)) AS votes
  FROM (
    SELECT e."timeZone" AS tz, count(*) AS n
    FROM asset a JOIN asset_exif e ON e."assetId" = a.id
    WHERE a."ownerId" = %(owner)s AND a."deletedAt" IS NULL AND a.id <> q.id
      AND a."fileCreatedAt" BETWEEN q.t - interval '3 hours' AND q.t + interval '3 hours'
      AND e."timeZone" IS NOT NULL
      AND ((e.latitude IS NOT NULL AND NOT (abs(e.latitude) < 0.001 AND abs(e.longitude) < 0.001))
           OR e."timeZone" NOT IN ('UTC', 'UTC+0', 'Etc/UTC', 'UTC+00:00'))
      AND NOT (a.id = ANY(%(exclude)s::uuid[]))
    GROUP BY 1) v) z ON true
"""


def hints(conn, owner_id: str, wanted: dict[str, datetime],
          exclude: list[str] | tuple = ()) -> dict[str, Hint]:
    """`asset id → Hint` for the assets in `wanted` (id → true instant).
    `exclude`: assets whose zone isn't evidence yet (unverified sidecars)."""
    from .takeout_redate import parse_zone
    if not wanted:
        return {}
    ids = sorted(wanted)
    with conn.cursor() as cur:
        cur.execute(HINTS_SQL, {"owner": owner_id, "ids": ids, "ts": [wanted[i] for i in ids],
                                "exclude": sorted(set(exclude))})
        rows = cur.fetchall()
    return {str(a): Hint(_num(la), _num(lo), _vote(votes, wanted[str(a)]))
            for a, la, lo, votes in rows}


def _vote(votes, at: datetime):
    """The zone of a clear (two-thirds) majority of the shots around `at`,
    counted by their offset at that moment: `Pacific/Honolulu` and `UTC-10`
    are one vote. The majority's most common spelling is returned (an IANA
    name keeps DST right). A UTC majority, or none, is None: no change."""
    from .takeout_redate import parse_zone
    by_offset: dict[timedelta, list[tuple[int, object]]] = {}
    total = 0
    for tz, n in (votes or []):
        if tz in ("UTC", "UTC+0", "Etc/UTC", "UTC+00:00"):
            off, zone = timedelta(0), None
        else:
            zone = parse_zone(tz)
            if zone is None:
                continue
            off = at.astimezone(zone).utcoffset()
        by_offset.setdefault(off, []).append((int(n), zone))
        total += int(n)
    if not total:
        return None
    off, group = max(by_offset.items(), key=lambda kv: sum(n for n, _ in kv[1]))
    if sum(n for n, _ in group) * 3 < total * 2 or not off:
        return None
    return max(((n, z) for n, z in group if z is not None), key=lambda nz: nz[0])[1]


def needs_hint(file: FileFacts, side: SidecarFacts) -> bool:
    """The file can't settle it alone: sidecar-only GPS, or a bare-UTC clock
    with no position anywhere."""
    gps_only = side.lat is not None and file.lat is None
    dt = _parse_dt(side.dto) if side.dto else None
    utc_clock = (file.local is None and file.utc is not None and dt is not None
                 and dt.tzinfo is None)
    return gps_only or utc_clock


def sidecar_text(path: Path) -> str | None:
    try:
        return path.read_text(errors="replace")
    except OSError:
        return None


__all__ = [
    "FileFacts", "SidecarFacts", "Hint", "Repair", "file_facts", "read_sidecar",
    "plan", "agrees", "describes", "split_roles", "own_patch", "correct_patch", "hints", "needs_hint", "sidecar_text", "km",
    "SIDECARS_SQL", "SHARED_OWNERS_SQL", "HINTS_SQL", "UTC_CLOCK_SQL", "REGISTER_SIDECAR_SQL", "zone_fix", "VIDEO_EXTS", "MIRROR_FAR_KM", "MIRROR_NEAR_KM",
]
