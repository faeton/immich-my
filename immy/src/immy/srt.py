"""DJI-style .SRT telemetry parser.

Handles two common formats:
- Newer: `[latitude: 12.345] [longitude: 67.890] [altitude: 100.0]`
- Older: `GPS(12.345,67.890,100.0)` or `GPS (lat,lon,alt)`

Also pulls the first subtitle timestamp line as capture date.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_RE_LAT_BR = re.compile(r"\[latitude[:\s]+(-?\d+\.\d+)\]", re.IGNORECASE)
_RE_LON_BR = re.compile(r"\[longitude[:\s]+(-?\d+\.\d+)\]", re.IGNORECASE)
_RE_ALT_BR = re.compile(r"\[altitude[:\s]+(-?\d+\.\d+)\]", re.IGNORECASE)
_RE_GPS_PAREN = re.compile(
    r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\)"
)
_RE_DATE = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
)


@dataclass
class SrtTelemetry:
    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    datetime_original: datetime | None = None


def parse(srt_path: Path) -> SrtTelemetry:
    text = srt_path.read_text(errors="replace")
    tele = SrtTelemetry()

    m_lat = _RE_LAT_BR.search(text)
    m_lon = _RE_LON_BR.search(text)
    m_alt = _RE_ALT_BR.search(text)
    if m_lat and m_lon:
        tele.latitude = float(m_lat.group(1))
        tele.longitude = float(m_lon.group(1))
        if m_alt:
            tele.altitude = float(m_alt.group(1))
    else:
        m_paren = _RE_GPS_PAREN.search(text)
        if m_paren:
            tele.latitude = float(m_paren.group(1))
            tele.longitude = float(m_paren.group(2))
            tele.altitude = float(m_paren.group(3))

    # Skip the SRT cue-timing line (00:00:00,000 --> 00:00:01,000) and find
    # the first wall-clock timestamp.
    for line in text.splitlines():
        if "-->" in line:
            continue
        m = _RE_DATE.search(line)
        if m:
            try:
                tele.datetime_original = datetime(
                    int(m.group(1)), int(m.group(2)), int(m.group(3)),
                    int(m.group(4)), int(m.group(5)), int(m.group(6)),
                )
            except ValueError:
                pass
            break

    return tele


def find_sibling(media_path: Path) -> Path | None:
    for suffix in (".SRT", ".srt"):
        candidate = media_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None
