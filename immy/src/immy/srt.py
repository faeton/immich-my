"""DJI-style .SRT telemetry parser.

A DJI clip's `.SRT` is one subtitle cue per frame; each cue's payload
carries that frame's wall-clock time plus a flat list of `key: value`
telemetry fields. Three field dialects appear in the wild:

- Newer (bracketed, one field each):
  `[latitude: 12.345] [longitude: 67.890] [rel_alt: 1.3 abs_alt: 121.0]`
  with `[iso: 100] [shutter: 1/500.0] [fnum: 280] [ev: 0] [focal_len: 240]`.
  Note `rel_alt`/`abs_alt` share a single bracket, and the older firmware
  emits `[altitude: 120.0]` instead (treated as a relative height).
- Older (parenthesised): `GPS(12.345,67.890,100.0)`.

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
_RE_GPS_PAREN = re.compile(
    r"GPS\s*\(\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*,\s*(-?\d+\.\d+)\s*\)"
)
_RE_DATE = re.compile(
    r"(\d{4})[-/](\d{2})[-/](\d{2})[ T](\d{2}):(\d{2}):(\d{2})"
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


def _parse_block(block: str, index: int) -> SrtFrame:
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
    frame.longitude = _to_float(kv.get("longitude"))
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
        if mp:
            frame.latitude = float(mp.group(1))
            frame.longitude = float(mp.group(2))
            if frame.abs_alt is None:
                frame.abs_alt = float(mp.group(3))

    return frame


def iter_frames(text: str) -> Iterator[SrtFrame]:
    """Yield one `SrtFrame` per non-empty cue block, in file order."""
    idx = 0
    for block in _RE_BLOCK_SEP.split(text.strip()):
        if not block.strip():
            continue
        idx += 1
        yield _parse_block(block, idx)


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
