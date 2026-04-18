"""Per-file capture-date resolution with explicit authority ranking.

Each file exposes one or more plausible capture datetimes — EXIF tags,
a companion telemetry file, a filename pattern, the filesystem mtime.
`resolve()` picks the best one and tells callers *why*.

Authority (highest first): `exif > companion > filename > mtime`.

EXIF wins because the camera wrote it at capture time. A DJI SRT is
still ranked `companion` (below EXIF) — on MP4 the QuickTime:CreateDate
is usually the same moment the SRT frame 1 is timestamped, but if they
disagree we trust what the camera put in the header. `filename` covers
phone/app exports whose EXIF is stripped but whose name encodes the
capture instant. `mtime` is a last resort and almost always lies
(copies, downloads, archive extraction all touch it).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from .exif import ExifRow
from .filenames import parse_date as parse_filename_date
from .srt import find_sibling, parse as parse_srt


Source = Literal["exif", "companion", "filename", "mtime"]
_RANK: dict[Source, int] = {"exif": 3, "companion": 2, "filename": 1, "mtime": 0}


@dataclass(frozen=True)
class DateAuthority:
    dt: datetime
    source: Source
    raw: str  # human-readable origin, e.g. "EXIF:DateTimeOriginal" or "DJI_0001.SRT"

    @property
    def rank(self) -> int:
        return _RANK[self.source]


def _parse_exif_dt(s: object) -> datetime | None:
    if not isinstance(s, str):
        return None
    s = s.strip().replace("/", ":")
    for fmt in ("%Y:%m:%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    return None


_EXIF_DATE_KEYS = (
    # XMP sidecar wins when present — that's our deliberate override of
    # baked-in EXIF (e.g. clock-drift correction, user edits).
    "XMP:DateTimeOriginal",
    "EXIF:DateTimeOriginal",
    "QuickTime:CreateDate",
    "EXIF:CreateDate",
    "EXIF:ModifyDate",
)


def resolve(row: ExifRow) -> DateAuthority | None:
    for key in _EXIF_DATE_KEYS:
        dt = _parse_exif_dt(row.raw.get(key))
        if dt is not None:
            return DateAuthority(dt=dt, source="exif", raw=key)

    srt = find_sibling(row.path)
    if srt is not None:
        tele = parse_srt(srt)
        if tele.datetime_original is not None:
            return DateAuthority(dt=tele.datetime_original, source="companion", raw=srt.name)

    fn = parse_filename_date(row.path)
    if fn is not None:
        return DateAuthority(dt=fn.dt, source="filename", raw=row.path.name)

    try:
        mtime = row.path.stat().st_mtime
    except OSError:
        return None
    return DateAuthority(dt=datetime.fromtimestamp(mtime), source="mtime", raw="st_mtime")
