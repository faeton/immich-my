"""Capture-time resolution — ONE model shared by ingest and everything
that reasons about capture times (audit rules, `backfill-dates`).

`capture_time(row)` is what `immy process` stores: the instant
(`fileCreatedAt` / `dateTimeOriginal`), the wall clock (`localDateTime`)
and the `timeZone`, chosen together from a separate `.xmp` SIDECAR first,
then the embedded tags, with QuickTime `CreateDate` read as UTC except for
`QUICKTIME_LOCAL_CLOCK_MAKES`. A rule that compares or rewrites capture
times must work in this space, or what it proposes is not what ingest
will store (final review 2026-10, clock-drift-by-camera).

Kept free of heavy imports (no psycopg / ML) so the audit rules can use it
without loading `process`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import insta360 as insta360_mod
from .exif import ExifRow


def _str(raw: Any) -> str | None:
    if raw is None:
        return None
    s = str(raw).strip()
    return s or None


def _parse_exif_datetime(raw: Any) -> datetime | None:
    """Parse ExifTool-style `YYYY:MM:DD HH:MM:SS[±HH:MM]`. Returns tz-aware
    datetime when a zone is present, else naive (caller anchors to UTC).

    Rejects plausibly-valid-but-nonsensical dates: cameras sometimes emit
    `0000:00:00 00:00:00` (a literal placeholder, not a real moment), and
    a few write "1904:01:01" as the Mac epoch. We treat anything before
    1970 or after 2100 as missing so `_best_datetime` keeps looking and
    the filename-date rule can fire cleanly.
    """
    if not isinstance(raw, str) or len(raw) < 19:
        return None
    s = raw.strip()
    tz = None
    # Optional ±HH:MM suffix (ExifTool's OffsetTime).
    if len(s) >= 25 and s[-6] in "+-" and s[-3] == ":":
        sign = 1 if s[-6] == "+" else -1
        try:
            hours = int(s[-5:-3])
            minutes = int(s[-2:])
        except ValueError:
            return None
        tz = timezone(sign * timedelta(hours=hours, minutes=minutes))
        s = s[:-6]
    try:
        dt = datetime.strptime(s.strip(), "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    if dt.year < 1970 or dt.year > 2100:
        return None
    return dt.replace(tzinfo=tz) if tz is not None else dt


_OFFSET_RE = re.compile(r"^(?:UTC|GMT)?\s*([+-])(\d{1,2})(?::?(\d{2}))?$", re.IGNORECASE)


def _parse_offset(raw: Any) -> timezone | None:
    """`+02:00` / `-0530` / `UTC+2` / `UTC+5:30` / `Z` / `UTC` → fixed tz.
    Anything else (IANA names, garbage) → None."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if s.upper() in ("Z", "UTC", "GMT"):
        return timezone.utc
    m = _OFFSET_RE.match(s)
    if m is None:
        return None
    hours, minutes = int(m.group(2)), int(m.group(3) or 0)
    if hours > 14 or minutes > 59:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def _zone(tz_name: str | None) -> tzinfo | None:
    """A zone string as written to `asset_exif.timeZone` → tzinfo: an
    offset (any `_parse_offset` form) or an IANA name. Unknown → None."""
    if not tz_name:
        return None
    off = _parse_offset(tz_name)
    if off is not None:
        return off
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError):
        return None


def _immich_time_zone(off: timezone) -> str:
    """Fixed offset → Immich's own `timeZone` spelling (`UTC`, `UTC+2`,
    `UTC+5:30`, `UTC-3`) — what its metadata extraction writes, and what
    the web UI (luxon) accepts as a zone; a bare `+02:00` it does not."""
    total = int(off.utcoffset(None).total_seconds()) // 60
    if total == 0:
        return "UTC"
    sign = "+" if total > 0 else "-"
    h, m = divmod(abs(total), 60)
    return f"UTC{sign}{h}" + (f":{m:02d}" if m else "")


# Makers whose `QuickTime:CreateDate` is the camera's LOCAL wall clock, not
# UTC as the QuickTime spec says. Each entry is backed by real files
# (CreateDate == the local time in the filename while the file mtime / GPS
# is the true UTC instant) — see the AUDIT-2026-10 Task 5 report. DJI and
# GoPro were checked and DO write UTC. Unknown makers stay UTC.
QUICKTIME_LOCAL_CLOCK_MAKES = ("insta360", "arashi vision")


def _quicktime_clock_is_local(row: ExifRow) -> bool:
    make = _str(row.get("EXIF:Make", "QuickTime:Make"))
    if make is not None and make.lower().startswith(QUICKTIME_LOCAL_CLOCK_MAKES):
        return True
    # Insta360 headers carry no Make (it lives in a vendor trailer exif.py
    # only reads for .insv/.lrv/.insp); a GO 2 `.mp4` is known by its name.
    return insta360_mod.classify(row.path) is not None


def _offset_tag(raw: Any) -> timezone | None:
    """An offset tag: `+02:00`-style text, or minutes as `-n` reports
    `QuickTime:TimeZone` (GoPro: 240 == +04:00)."""
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        minutes = int(raw)
        if abs(minutes) > 14 * 60:
            return None
        return timezone(timedelta(minutes=minutes))
    return _parse_offset(raw)


def _dated(dt: datetime, offset_raw: Any) -> tuple[datetime, str | None]:
    """A chosen date plus ITS OWN offset: inline if the value carries one,
    else the offset tag that belongs to the same field."""
    if dt.tzinfo is None:
        off = _offset_tag(offset_raw)
        if off is None:
            return dt, None
        dt = dt.replace(tzinfo=off)
    return dt, _immich_time_zone(dt.tzinfo)


def _capture(row: ExifRow) -> tuple[datetime, str | None] | None:
    """`(datetime, timeZone)` — see `_capture_sourced`."""
    captured = _capture_sourced(row)
    return None if captured is None else (captured[0], captured[1])


def _capture_sourced(row: ExifRow) -> tuple[datetime, str | None, str] | None:
    """Capture time and the `asset_exif.timeZone` that belongs to it,
    selected together so a losing tag never lends its offset to the winner.

    Order: SIDECAR `XMP:DateTimeOriginal` (immy's rule fixes / the user's
    edits win over the file; a naive one keeps the camera's embedded
    `OffsetTimeOriginal`, e.g. a clock-drift fix) → `EXIF:DateTimeOriginal`
    (+ `OffsetTimeOriginal`) → embedded `XMP:DateTimeOriginal` (inline
    offset only) → QuickTime: a `CreationDate` carrying an explicit offset
    (Apple Keys), else `CreateDate` (+ `QuickTime:TimeZone`) — UTC per the
    spec unless the maker is in `QUICKTIME_LOCAL_CLOCK_MAKES` →
    `EXIF:CreateDate` (+ `OffsetTimeDigitized`).

    The datetime is tz-aware whenever the absolute instant is known; naive
    means wall clock, interpreted in the returned zone if there is one.
    Offsets use Immich's `UTC±H[:MM]` spelling; IANA names pass through.
    The third element names the winning tag (`sidecar XMP:DateTimeOriginal`
    for the separate .xmp)."""
    embedded_offset = row.get("EXIF:OffsetTimeOriginal")
    for dt, offset_raw, label in (
        (_parse_exif_datetime(row.sidecar_get("XMP:DateTimeOriginal")), embedded_offset,
         SIDECAR_SOURCE),
        (_parse_exif_datetime(row.get("EXIF:DateTimeOriginal")), embedded_offset,
         "EXIF:DateTimeOriginal"),
        (_parse_exif_datetime(row.get("XMP:DateTimeOriginal")), None, "XMP:DateTimeOriginal"),
    ):
        if dt is not None:
            return (*_dated(dt, offset_raw), label)

    for key in ("QuickTime:CreationDate", "Keys:CreationDate"):
        cd = _parse_exif_datetime(row.get(key))
        if cd is not None and cd.tzinfo is not None:
            return (*_dated(cd, None), key)

    qt = _parse_exif_datetime(row.get("QuickTime:CreateDate"))
    if qt is not None:
        tz_raw = row.get("QuickTime:TimeZone")
        off = _offset_tag(tz_raw)
        tz_name = _immich_time_zone(off) if off is not None else _str(tz_raw)
        if qt.tzinfo is None and not _quicktime_clock_is_local(row):
            qt = qt.replace(tzinfo=timezone.utc)
        return qt, tz_name, "QuickTime:CreateDate"

    dt = _parse_exif_datetime(row.get("EXIF:CreateDate"))
    if dt is not None:
        return (*_dated(dt, row.get("EXIF:OffsetTimeDigitized")), "EXIF:CreateDate")
    return None


def _best_datetime(row: ExifRow) -> datetime | None:
    """`_capture`'s datetime alone (tz-aware when the instant is known)."""
    captured = _capture(row)
    return captured[0] if captured is not None else None


def _compute_instant(
    dt: datetime, kind: str, tz_name: str | None,
) -> tuple[datetime, datetime]:
    """Return `(local_date_time, date_time_original_utc)`.

    `local_date_time` is the naive wall-clock Immich sorts the timeline by
    (store it as those numbers +00:00); `date_time_original_utc` is the
    absolute instant.

    - kind="utc": `dt` is an absolute instant (naive = UTC numbers).
      localDateTime is that instant rendered in `tz_name`; with no zone, an
      aware `dt` keeps its own wall clock, a naive one its UTC numbers.
    - kind="local": `dt` is the wall clock the user saw. That IS
      localDateTime; the absolute instant comes from interpreting it in
      `tz_name` (or treating the wall numbers as UTC if no zone is known).

    `tz_name` is an IANA name or a fixed offset (`+02:00`, `UTC+2`).
    """
    zone = _zone(tz_name)
    if kind == "utc":
        abs_utc = (
            dt.astimezone(timezone.utc) if dt.tzinfo is not None
            else dt.replace(tzinfo=timezone.utc)
        )
        if zone is not None:
            local = abs_utc.astimezone(zone).replace(tzinfo=None)
        elif dt.tzinfo is not None:
            local = dt.replace(tzinfo=None)
        else:
            local = abs_utc.replace(tzinfo=None)
        return local, abs_utc

    local = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    if zone is not None:
        abs_utc = local.replace(tzinfo=zone).astimezone(timezone.utc)
    else:
        abs_utc = local.replace(tzinfo=timezone.utc)
    return local, abs_utc


SIDECAR_SOURCE = "sidecar XMP:DateTimeOriginal"


@dataclass(frozen=True)
class CaptureTime:
    """What ingest stores for one file's capture time."""

    instant: datetime          # tz-aware UTC: fileCreatedAt / dateTimeOriginal
    local: datetime            # naive wall clock: localDateTime
    time_zone: str | None      # asset_exif.timeZone (the zone `local` is in)
    source: str                # winning tag, e.g. SIDECAR_SOURCE, "QuickTime:CreateDate"
    # True when the file itself pins the instant (an offset, or a UTC-by-
    # spec QuickTime date). False: a bare wall clock whose instant depends
    # on the zone it is read in (none known → its numbers taken as UTC).
    absolute: bool
    own_zone: bool             # time_zone came from the file, not `fallback_zone`


def capture_time(row: ExifRow, *, fallback_zone: str | None = None) -> CaptureTime | None:
    """The capture time `immy process` stores for `row` (None: no capture
    tag; ingest then falls back to the file mtime).

    `fallback_zone` (IANA or offset) is only used when the file carries no
    zone of its own — a file's recorded offset always beats a trip-level
    guess. Ingest passes none, so a bare wall clock's numbers are UTC."""
    captured = _capture_sourced(row)
    if captured is None:
        return None
    dt, tz_name, source = captured
    zone = tz_name or fallback_zone
    absolute = dt.tzinfo is not None
    local, instant = _compute_instant(dt, "utc" if absolute else "local", zone)
    return CaptureTime(
        instant=instant, local=local, time_zone=zone, source=source,
        absolute=absolute, own_zone=tz_name is not None,
    )


def format_exif_datetime(instant: datetime, zone: tzinfo | None) -> str:
    """`YYYY:MM:DD HH:MM:SS` for an XMP DateTimeOriginal: rendered in `zone`
    with its offset inline (so ingest and Immich read the same instant), or
    naive UTC numbers when no zone is known."""
    if zone is None:
        return instant.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    local = instant.astimezone(zone)
    off = local.utcoffset() or timedelta(0)
    total = int(off.total_seconds()) // 60
    sign = "+" if total >= 0 else "-"
    h, m = divmod(abs(total), 60)
    return local.strftime("%Y:%m:%d %H:%M:%S") + f"{sign}{h:02d}:{m:02d}"
