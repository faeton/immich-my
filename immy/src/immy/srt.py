"""DJI-style .SRT telemetry parser.

A DJI clip's `.SRT` is one subtitle cue per frame; each cue's payload
carries that frame's wall-clock time plus a flat list of `key: value`
telemetry fields. Three field dialects appear in the wild:

- Newer (bracketed, one field each):
  `[latitude: 12.345] [longitude: 67.890] [rel_alt: 1.3 abs_alt: 121.0]`
  with `[iso: 100] [shutter: 1/500.0] [fnum: 280] [ev: 0] [focal_len: 240]`.
  Note `rel_alt`/`abs_alt` share a single bracket, and the older firmware
  emits `[altitude: 120.0]` instead (treated as a relative height).
- Older (parenthesised): `GPS(..)` in two coordinate orders (lat-first with an `M`
  third field, else decided per file from labelled coords / range), see
  `_resolve_file_order`; ambiguous orders yield no fix, and no altitude is taken. Dotted dates
  (`2017.8.5`) are accepted. Newer firmware
  also misspells `[longtitude: ..]`.

`parse_track` returns every frame; `parse` keeps the historical
first-fix-only `SrtTelemetry` API (used by `dates`, `backfill_dates`,
`rules.dji_srt`). Both honour `first_valid_fix` — the first frame with a
real GPS lock, skipping the `(0, 0)` "null island" fixes a drone emits
before it acquires satellites on takeoff.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterator


# Generic `key: value` / `key : value` scanner. Brackets are stripped to
# spaces before scanning so a value never captures a trailing `]`, and so a
# combined `[rel_alt: 1.3 abs_alt: 121.0]` yields both pairs. Values run to
# the next whitespace, which keeps `1/500.0` (shutter) and `-20.296270`
# (latitude) intact.
_RE_KV = re.compile(r"([A-Za-z_]\w*)\s*:\s*(\S+)")
# Parenthesised `GPS(a,b,c)`. Two published dialects (JuanIrache/DJI_SRT_Parser
# samples, MIT) put the coordinates in opposite orders:
#   - Matrice 300 style: `GPS(36.6146,-6.1120,0.0M) BAROMETER:0.3M` -> LAT first,
#     third field carries an `M` unit.
#   - Old Phantom/Mavic Pro style: `HOME(149.0251,-20.2532) ...` +
#     `GPS(149.0251,-20.2533,16) ...` -> LON first, third field a satellite count.
# Neither third field nor BAROMETER is a verified MSL altitude, so no altitude
# is taken from this form.
_RE_NUM = r"-?\d+(?:\.\d+)?"
_RE_GPS_PAREN = re.compile(
    rf"GPS\s*\(\s*({_RE_NUM})\s*,\s*({_RE_NUM})\s*(?:,\s*({_RE_NUM})\s*(M)?\s*)?\)",
    re.IGNORECASE,
)
_RE_HOME = re.compile(rf"HOME\s*\(\s*({_RE_NUM})\s*,\s*({_RE_NUM})\s*\)", re.IGNORECASE)
# Dates: `2023-01-02 10:00:00`, `2023/01/02 ...`, and old DJI `2017.08.19 13:02:57`.
_RE_DATE = re.compile(
    r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})[ T](\d{1,2}):(\d{2}):(\d{2})"
)
_RE_CUE_TIME = re.compile(r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})")
_RE_BLOCK_SEP = re.compile(r"\n\s*\n")


@dataclass
class SrtTelemetry:
    """First-valid-fix summary — the historical API."""

    latitude: float | None = None
    longitude: float | None = None
    altitude: float | None = None
    datetime_original: datetime | None = None


@dataclass
class SrtFrame:
    """One subtitle cue's telemetry. Altitudes: `rel_alt` is height above
    the takeoff point, `abs_alt` is above sea level (use for GPX `<ele>`).
    Legacy `[altitude:]` lands in `rel_alt`."""

    index: int
    t_offset_s: float | None = None
    datetime: datetime | None = None
    latitude: float | None = None
    longitude: float | None = None
    rel_alt: float | None = None
    abs_alt: float | None = None
    iso: float | None = None
    shutter: str | None = None
    fnum: float | None = None
    ev: float | None = None
    focal_len: float | None = None

    def has_fix(self) -> bool:
        """True for a real GPS lock — coords present and not null-island."""
        if self.latitude is None or self.longitude is None:
            return False
        return not (self.latitude == 0.0 and self.longitude == 0.0)

    @property
    def ele(self) -> float | None:
        """Best elevation for a GPX track point: MSL if known, else AGL."""
        return self.abs_alt if self.abs_alt is not None else self.rel_alt


def _to_float(s: str | None) -> float | None:
    if s is None:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _cue_offset(block: str) -> float | None:
    """Seconds from clip start, read from the cue's `HH:MM:SS,mmm` start."""
    for line in block.splitlines():
        if "-->" in line:
            head = line.split("-->", 1)[0]
            m = _RE_CUE_TIME.search(head)
            if m:
                h, mi, s, ms = (int(m.group(i)) for i in range(1, 5))
                return h * 3600 + mi * 60 + s + ms / 1000.0
            return None
    return None


def _in_range(lat: float, lon: float) -> bool:
    return abs(lat) <= 90 and abs(lon) <= 180


LAT_FIRST, LON_FIRST = "lat-first", "lon-first"


def _resolve_file_order(
    parens: list[tuple[float, float, bool]],
    labelled: list[tuple[float, float]],
    homes: list[tuple[float, float]],
) -> str | None:
    """Coordinate order of every `GPS(a,b,..)` in a file, decided ONCE per file.

    1. Any `M`-suffixed third field -> lat-first (Matrice style).
    2. Else agreement within ~1 degree with in-file evidence: every labelled
       (latitude, longitude) pair plus each `HOME(..)` read both ways (HOME is
       evidence only through this check, never a dialect signature).
    3. Else exactly one order that is in range for every GPS cue.
    4. Else None: emit no fix from the GPS() cues.
    """
    if not parens:
        return None
    if any(unit for _, _, unit in parens):
        return LAT_FIRST

    def point(order: str, a: float, b: float) -> tuple[float, float]:
        return (a, b) if order == LAT_FIRST else (b, a)

    evidence = list(labelled)
    for h1, h2 in homes:
        evidence += [(h1, h2), (h2, h1)]
    orders = (LAT_FIRST, LON_FIRST)
    if evidence:
        agree = [
            o for o in orders
            if any(
                abs(point(o, a, b)[0] - la) <= 1.0 and abs(point(o, a, b)[1] - lo) <= 1.0
                for a, b, _ in parens for la, lo in evidence
            )
        ]
        if len(agree) == 1:
            return agree[0]
    valid = [
        o for o in orders
        if all(_in_range(*point(o, a, b)) for a, b, _ in parens)
    ]
    return valid[0] if len(valid) == 1 else None


def _parse_block(block: str, index: int, order: str | None = None) -> SrtFrame:
    frame = SrtFrame(index=index, t_offset_s=_cue_offset(block))

    m = _RE_DATE.search(block)
    if m:
        try:
            frame.datetime = datetime(
                int(m.group(1)), int(m.group(2)), int(m.group(3)),
                int(m.group(4)), int(m.group(5)), int(m.group(6)),
            )
        except ValueError:
            pass

    # Strip cue-timing lines (they contain `-->` and bare `00:00:01,000`
    # that would pollute the kv scan) and bracket delimiters.
    payload = "\n".join(
        ln for ln in block.splitlines() if "-->" not in ln
    ).replace("[", " ").replace("]", " ")
    kv = {k.lower(): v for k, v in _RE_KV.findall(payload)}

    frame.latitude = _to_float(kv.get("latitude"))
    # DJI firmware has shipped the misspelling `longtitude`.
    frame.longitude = _to_float(kv.get("longitude", kv.get("longtitude")))
    frame.rel_alt = _to_float(kv.get("rel_alt"))
    frame.abs_alt = _to_float(kv.get("abs_alt"))
    if frame.rel_alt is None and "altitude" in kv:
        frame.rel_alt = _to_float(kv.get("altitude"))
    frame.iso = _to_float(kv.get("iso"))
    frame.shutter = kv.get("shutter")
    frame.fnum = _to_float(kv.get("fnum"))
    frame.ev = _to_float(kv.get("ev"))
    frame.focal_len = _to_float(kv.get("focal_len"))

    # Parenthesised fallback when no bracketed coords were present.
    if frame.latitude is None or frame.longitude is None:
        mp = _RE_GPS_PAREN.search(block)
        if mp and order is not None:
            a, b = float(mp.group(1)), float(mp.group(2))
            lat, lon = (a, b) if order == LAT_FIRST else (b, a)
            if _in_range(lat, lon):
                frame.latitude, frame.longitude = lat, lon

    return frame


def iter_frames(text: str) -> Iterator[SrtFrame]:
    """Yield one `SrtFrame` per non-empty cue block, in file order."""
    blocks = [b for b in _RE_BLOCK_SEP.split(text.strip()) if b.strip()]
    # Pass 1: file-wide evidence for the `GPS(a,b)` order (see _resolve_file_order).
    parens: list[tuple[float, float, bool]] = []
    labelled: list[tuple[float, float]] = []
    homes: list[tuple[float, float]] = []
    for i, block in enumerate(blocks, 1):
        f = _parse_block(block, i)
        if f.latitude is not None and f.longitude is not None:
            labelled.append((f.latitude, f.longitude))
        # GPS()/HOME() are evidence (incl. the `M` dialect signature) even in a
        # cue that also carries labelled coordinates.
        mp = _RE_GPS_PAREN.search(block)
        if mp:
            parens.append((float(mp.group(1)), float(mp.group(2)), bool(mp.group(4))))
        mh = _RE_HOME.search(block)
        if mh:
            homes.append((float(mh.group(1)), float(mh.group(2))))
    order = _resolve_file_order(parens, labelled, homes)
    for i, block in enumerate(blocks, 1):
        yield _parse_block(block, i, order)


def parse_track(srt_path: Path) -> list[SrtFrame]:
    """Parse every frame of a DJI `.SRT` into `SrtFrame`s."""
    return list(iter_frames(srt_path.read_text(errors="replace")))


def first_valid_fix(frames: list[SrtFrame]) -> SrtFrame | None:
    """First frame with a real GPS lock (the takeoff point), or None."""
    for f in frames:
        if f.has_fix():
            return f
    return None


def parse(srt_path: Path) -> SrtTelemetry:
    """First-valid-fix summary. Streams cues, stopping once both a fix and a
    wall-clock time are known — cheap even on multi-thousand-frame files."""
    tele = SrtTelemetry()
    fix_found = False
    for frame in iter_frames(srt_path.read_text(errors="replace")):
        if not fix_found and frame.has_fix():
            tele.latitude = frame.latitude
            tele.longitude = frame.longitude
            tele.altitude = frame.ele
            fix_found = True
        if tele.datetime_original is None and frame.datetime is not None:
            tele.datetime_original = frame.datetime
        if fix_found and tele.datetime_original is not None:
            break
    return tele


def find_sibling(media_path: Path) -> Path | None:
    for suffix in (".SRT", ".srt"):
        candidate = media_path.with_suffix(suffix)
        if candidate.is_file():
            return candidate
    return None
