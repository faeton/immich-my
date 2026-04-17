"""Trip-wide IANA timezone → timezone-suffixed XMP DateTimeOriginal.

Reads `timezone:` from the folder notes file front-matter (e.g.
`Indian/Mauritius`). For each media file whose DateTimeOriginal is known,
computes the UTC offset at that instant via `zoneinfo` and writes
`XMP:DateTimeOriginal = <date> <time><+HH:MM>` — XMP-exif stores the
offset inline with the datetime rather than as a separate OffsetTime*
field, which XMP schema lacks.

HIGH because the zone is explicit user intent. Depends on DateTimeOriginal
being present — for DJI MP4s with SRT companions, the CLI's second apply
pass picks them up after dji-date-from-srt writes the date.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..exif import ExifRow
from ..notes import parse_frontmatter, resolve
from .registry import Finding, Rule, register


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


def _fmt_offset(td: timedelta) -> str:
    total = int(td.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hh, mm = divmod(total // 60, 60)
    return f"{sign}{hh:02d}:{mm:02d}"


def _has_offset_suffix(dt_str: str) -> bool:
    tail = dt_str.strip()[-6:]
    return len(tail) == 6 and tail[0] in "+-" and tail[3] == ":"


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    tz_name = fm.get("timezone")
    if not isinstance(tz_name, str) or not tz_name.strip():
        return []
    try:
        zone = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        return []

    out: list[Finding] = []
    for row in rows:
        raw = row.get("XMP:DateTimeOriginal", "EXIF:DateTimeOriginal", "QuickTime:CreateDate")
        dt = _parse_exif_dt(raw)
        if dt is None:
            continue
        # Already tz-annotated in XMP sidecar → skip.
        if isinstance(raw, str) and _has_offset_suffix(raw):
            continue
        offset = dt.replace(tzinfo=zone).utcoffset()
        if offset is None:
            continue
        offset_str = _fmt_offset(offset)
        new_value = f"{dt.strftime('%Y:%m:%d %H:%M:%S')}{offset_str}"
        out.append(Finding(
            rule="trip-timezone",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch={"DateTimeOriginal": new_value},
            reason=f"{tz_name} → {offset_str} at {dt.isoformat()}",
        ))
    return out


register(Rule(name="trip-timezone", confidence="high", propose=_propose))
