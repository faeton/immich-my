"""Filename pattern parsers."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


_RE_DATE_FILENAME = re.compile(
    r"(?:^|[_-])"
    r"(?P<prefix>VID|IMG|DJI|MVI|PXL)_"
    r"(?P<y>\d{4})(?P<mo>\d{2})(?P<d>\d{2})"
    r"[_-](?P<H>\d{2})(?P<M>\d{2})(?P<S>\d{2})",
    re.IGNORECASE,
)


# Insta360: VID_YYYYMMDD_HHMMSS_00_NNN.insv / LRV_YYYYMMDD_HHMMSS_01_NNN.lrv
_RE_INSTA360 = re.compile(
    r"^(?P<kind>VID|LRV)_"
    r"(?P<ts>\d{8}_\d{6})"
    r"_(?P<lens>\d{2})"
    r"_(?P<serial>\d+)$",
    re.IGNORECASE,
)


@dataclass
class FilenameDate:
    prefix: str
    dt: datetime


def parse_date(path: Path) -> FilenameDate | None:
    m = _RE_DATE_FILENAME.search(path.stem)
    if not m:
        return None
    try:
        dt = datetime(
            int(m.group("y")), int(m.group("mo")), int(m.group("d")),
            int(m.group("H")), int(m.group("M")), int(m.group("S")),
        )
    except ValueError:
        return None
    return FilenameDate(prefix=m.group("prefix").upper(), dt=dt)


@dataclass
class Insta360Key:
    timestamp: str  # "YYYYMMDD_HHMMSS"
    serial: str


def parse_insta360(path: Path) -> Insta360Key | None:
    m = _RE_INSTA360.match(path.stem)
    if not m:
        return None
    return Insta360Key(timestamp=m.group("ts"), serial=m.group("serial"))
