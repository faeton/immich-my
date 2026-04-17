"""Date from filename pattern (VID_/IMG_/DJI_/PXL_/MVI_ YYYYMMDD_HHMMSS)."""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from ..filenames import parse_date
from .dji_srt import _has_date
from .registry import Finding, Rule, register


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        if _has_date(row):
            continue
        fd = parse_date(row.path)
        if fd is None:
            continue
        out.append(Finding(
            rule="date-from-filename-vid-img",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch={"DateTimeOriginal": fd.dt.strftime("%Y:%m:%d %H:%M:%S")},
            reason=f"filename encodes {fd.dt.isoformat()}; EXIF missing",
        ))
    return out


register(Rule(
    name="date-from-filename-vid-img",
    confidence="high",
    propose=_propose,
))
