"""Approximate capture time and position, good enough to sort, group and name
trips. The real capture time is immy audit's job; this module follows its
reading of QuickTime clocks (immy `capture.QUICKTIME_LOCAL_CLOCK_MAKES`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

LOCAL_CLOCK_MAKES = ("insta360", "arashi vision")  # immy capture.QUICKTIME_LOCAL_CLOCK_MAKES
EXIF_TAGS = ["-DateTimeOriginal", "-OffsetTimeOriginal", "-CreationDate", "-CreateDate", "-Make", "-Model",
             "-GPSLatitude", "-GPSLongitude", "-ImageWidth", "-ImageHeight"]

# A wall clock in a cue body: Mavic 3 `2022-01-30 12:29:38.078`, Mini `…,500`, Mavic Pro
# `2017.08.05 14:11:51`. The cue line `00:00:00,000 --> …` has no 4-digit year, so it never matches.
SRT_WALL_RE = re.compile(r"(?<!\d)(\d{4})[-.](\d{2})[-.](\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:[.,](\d{1,6}))?")
SRT_KV_RE = re.compile(r"\[\s*(latitude|longitude|longtitude)\s*:\s*(-?\d+(?:\.\d+)?)", re.I)
# GPS(a,b[,alt[M]]): an `M`-suffixed altitude means lat-first (Matrice), else lon-first (Mavic Pro, Phantom).
SRT_PAREN_RE = re.compile(r"GPS\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)"
                          r"(?:\s*,\s*-?\d+(?:\.\d+)?\s*(M)?)?", re.I)
EXIF_DT_RE = re.compile(r"^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?\s*(Z|[+-]\d{2}:?\d{2})?")


@dataclass(frozen=True)
class CaptureTime:
    time: str
    src: str
    kind: str
    utc: str | None = None
    zone: str | None = None


def _valid(lat: float, lon: float) -> bool:
    return abs(lat) <= 90 and abs(lon) <= 180 and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)


def srt_first(path: Path) -> tuple[datetime | None, tuple[float, float] | None]:
    """First wall clock and first valid fix of a DJI .SRT (the first ~400 KB is plenty)."""
    try:
        text = Path(path).read_bytes()[:400_000].decode("utf-8", "ignore")
    except OSError:
        return None, None
    wall = None
    m = SRT_WALL_RE.search(text)
    if m:
        y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
        frac = (m.group(7) or "0").ljust(6, "0")[:6]
        try:
            wall = datetime(y, mo, d, h, mi, s, int(frac))
        except ValueError:
            wall = None
    gps = None
    for block in re.split(r"\n\s*\n", text):
        kv = {k.lower().replace("longtitude", "longitude"): float(v) for k, v in SRT_KV_RE.findall(block)}
        cand = None
        if "latitude" in kv and "longitude" in kv:
            cand = (kv["latitude"], kv["longitude"])
        else:
            pm = SRT_PAREN_RE.search(block)
            if pm:
                a, b = float(pm.group(1)), float(pm.group(2))
                cand = (a, b) if pm.group(3) else (b, a)
        if cand and _valid(*cand):
            gps = cand
            break
    return wall, gps


def exif_batch(paths: list[Path]) -> dict[str, dict]:
    """One exiftool run over many files → {str(path): tags}. -n: signed decimal GPS."""
    if not paths:
        return {}
    if shutil.which("exiftool") is None:
        raise RuntimeError("exiftool not found on PATH (needed to read capture times)")
    r = subprocess.run(["exiftool", "-j", "-n", "-fast", "-api", "LargeFileSupport=1", "-charset", "filename=utf8",
                        *EXIF_TAGS, "-@", "-"],
                       input="\n".join(str(p) for p in paths), capture_output=True, text=True)
    try:
        rows = json.loads(r.stdout)
    except ValueError:
        rows = None
    if not isinstance(rows, list):
        # exit 1 with valid JSON means some files failed; only unparseable output is fatal
        raise RuntimeError(f"exiftool failed (exit {r.returncode}): {r.stderr.strip()[:300]}")
    return {row["SourceFile"]: row for row in rows if "Error" not in row}


def exif_gps(row: dict) -> tuple[float, float] | None:
    lat, lon = row.get("GPSLatitude"), row.get("GPSLongitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and _valid(float(lat), float(lon)):
        return float(lat), float(lon)
    return None


def parse_offset(raw) -> timezone | None:
    if not isinstance(raw, str) or not raw.strip():
        return None
    raw = raw.strip()
    if raw == "Z":
        return timezone.utc
    m = re.match(r"^([+-])(\d{2}):?(\d{2})$", raw)
    if not m:
        return None
    sign = 1 if m.group(1) == "+" else -1
    return timezone(sign * timedelta(hours=int(m.group(2)), minutes=int(m.group(3))))


def parse_exif_dt(raw) -> tuple[datetime, timezone | None] | None:
    if not isinstance(raw, str):
        return None
    m = EXIF_DT_RE.match(raw.strip())
    if not m:
        return None
    y, mo, d, h, mi, s = (int(x) for x in m.groups()[:6])
    if y < 1980:  # 0000:00:00 and reset camera clocks
        return None
    try:
        return datetime(y, mo, d, h, mi, s), parse_offset(m.group(7))
    except ValueError:
        return None


_TF = None


def _zone_at(lat: float, lon: float) -> str | None:
    global _TF
    if _TF is None:
        from timezonefinder import TimezoneFinder
        _TF = TimezoneFinder()
    return _TF.timezone_at(lat=lat, lng=lon)


def _iso(dt: datetime) -> str:
    return dt.replace(microsecond=0).isoformat()


def _instant(aware: datetime, src: str, gps, own_wall: bool) -> CaptureTime:
    utc = aware.astimezone(timezone.utc)
    zone = _zone_at(*gps) if gps else None
    if zone:
        local = utc.astimezone(ZoneInfo(zone)).replace(tzinfo=None)
    elif own_wall:
        local = aware.replace(tzinfo=None)
    else:
        local, zone = utc.replace(tzinfo=None), "UTC"
    return CaptureTime(_iso(local), src, "instant", utc.strftime("%Y-%m-%dT%H:%M:%SZ"), zone)


def capture_time(row: dict, srt_wall: datetime | None, mtime: float, gps) -> CaptureTime:
    if srt_wall:
        return CaptureTime(_iso(srt_wall), "srt", "wall")
    p = parse_exif_dt(row.get("DateTimeOriginal"))
    if p:
        dt, tz = p
        tz = tz or parse_offset(row.get("OffsetTimeOriginal"))
        return _instant(dt.replace(tzinfo=tz), "exif", gps, True) if tz else CaptureTime(_iso(dt), "exif", "wall")
    p = parse_exif_dt(row.get("CreationDate"))
    if p:
        dt, tz = p
        return (_instant(dt.replace(tzinfo=tz), "qt-creation", gps, True) if tz
                else CaptureTime(_iso(dt), "qt-creation", "wall"))
    p = parse_exif_dt(row.get("CreateDate"))
    if p:
        dt, tz = p
        if tz:
            return _instant(dt.replace(tzinfo=tz), "qt-create", gps, True)
        make = str(row.get("Make") or "").lower()
        if any(m in make for m in LOCAL_CLOCK_MAKES):
            return CaptureTime(_iso(dt), "qt-create", "wall")
        return _instant(dt.replace(tzinfo=timezone.utc), "qt-create", gps, False)
    return CaptureTime(_iso(datetime.fromtimestamp(mtime)), "mtime", "guess")
