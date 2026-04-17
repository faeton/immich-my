"""DJI companion-SRT rules."""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow, has_gps as _has_gps
from ..srt import find_sibling, parse
from .registry import Finding, Rule, register


def _has_date(row: ExifRow) -> bool:
    return any(
        row.get(k) is not None
        for k in (
            "EXIF:DateTimeOriginal",
            "QuickTime:CreateDate",
            "XMP:DateTimeOriginal",
        )
    )


def _propose_gps(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        if _has_gps(row):
            continue
        srt = find_sibling(row.path)
        if srt is None:
            continue
        tele = parse(srt)
        if tele.latitude is None or tele.longitude is None:
            continue
        patch: dict[str, str] = {
            "GPSLatitude": f"{tele.latitude:.6f}",
            "GPSLatitudeRef": "N" if tele.latitude >= 0 else "S",
            "GPSLongitude": f"{tele.longitude:.6f}",
            "GPSLongitudeRef": "E" if tele.longitude >= 0 else "W",
        }
        if tele.altitude is not None:
            patch["GPSAltitude"] = f"{tele.altitude:.2f}"
            patch["GPSAltitudeRef"] = "0" if tele.altitude >= 0 else "1"
        out.append(Finding(
            rule="dji-gps-from-srt",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch=patch,
            reason=f"sibling {srt.name} carries GPS; media has none",
        ))
    return out


def _propose_date(rows: list[ExifRow], folder: Path) -> list[Finding]:
    out: list[Finding] = []
    for row in rows:
        if _has_date(row):
            continue
        srt = find_sibling(row.path)
        if srt is None:
            continue
        tele = parse(srt)
        if tele.datetime_original is None:
            continue
        out.append(Finding(
            rule="dji-date-from-srt",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch={"DateTimeOriginal": tele.datetime_original.strftime("%Y:%m:%d %H:%M:%S")},
            reason=f"sibling {srt.name} carries wall-clock time; media has none",
        ))
    return out


register(Rule(name="dji-gps-from-srt", confidence="high", propose=_propose_gps))
register(Rule(name="dji-date-from-srt", confidence="high", propose=_propose_date))
