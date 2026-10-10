"""Tesla H.264 SEI telemetry (NAL type 6, user-data-unregistered, protobuf).

Field map (reference: teslamotors/dashcam):
  1 version · 2 gear · 3 seq · 4 speed (m/s) · 5 accelerator pedal % ·
  6 steering · 7/8 blinker L/R · 9 brake · 10 autopilot · 11 lat · 12 lon ·
  13 heading · 14/15/16 linear accel x/y/z.
Present only in driving clips on recent firmware; parked (Sentry) clips carry
none. The old `_tools/build.py` had field 5 as "accel" — that was wrong.
"""

from __future__ import annotations

import struct
from pathlib import Path
from typing import Iterator

FIELDS = {
    1: "version", 2: "gear", 3: "seq", 4: "speed_mps", 5: "accel_pedal_pct",
    6: "steer", 7: "blinker_l", 8: "blinker_r", 9: "brake", 10: "autopilot",
    11: "lat", 12: "lon", 13: "heading", 14: "accel_x", 15: "accel_y", 16: "accel_z",
}


def _boxes(d: bytes, start: int, end: int):
    i = start
    while i + 8 <= end:
        ln = struct.unpack(">I", d[i:i + 4])[0]
        typ = d[i + 4:i + 8]
        hdr = 8
        if ln == 1:
            ln = struct.unpack(">Q", d[i + 8:i + 16])[0]
            hdr = 16
        elif ln == 0:
            ln = end - i
        if ln < hdr:
            break
        yield typ, i + hdr, i + ln
        i += ln


def _varint(b: bytes, i: int) -> tuple[int, int]:
    n = s = 0
    while i < len(b):
        c = b[i]
        i += 1
        n |= (c & 0x7F) << s
        if not c & 0x80:
            return n, i
        s += 7
    return n, i


def parse_protobuf(b: bytes) -> dict[int, object]:
    """Flat protobuf decode: {field_number: value}. Enough for Tesla's message
    (varints, 32/64-bit floats, length-delimited blobs)."""
    t: dict[int, object] = {}
    i = 0
    while i < len(b):
        tag, i = _varint(b, i)
        f, wt = tag >> 3, tag & 7
        if wt == 0:
            v, i = _varint(b, i)
        elif wt == 5:
            v = struct.unpack("<f", b[i:i + 4])[0]
            i += 4
        elif wt == 1:
            v = struct.unpack("<d", b[i:i + 8])[0]
            i += 8
        elif wt == 2:
            ln, i = _varint(b, i)
            v = b[i:i + ln]
            i += ln
        else:
            break
        t[f] = v
    return t


def strip_emulation_prevention(raw: bytes) -> bytes:
    """00 00 03 → 00 00 (H.264 emulation-prevention bytes)."""
    out = bytearray()
    z = 0
    for c in raw:
        if z >= 2 and c == 3:
            z = 0
            continue
        out.append(c)
        z = z + 1 if c == 0 else 0
    return bytes(out)


def iter_sei_payloads(data: bytes, *, max_samples: int = 100_000) -> Iterator[bytes]:
    """Yield the raw (emulation-stripped) protobuf payload of every Tesla SEI
    NAL in an MP4's mdat (length-prefixed AVC NALs). The car pads the SEI
    size with 0x42 bytes and ends the header with 0x69; the parser keys on
    that, as the throwaway tool did (works on every generation seen)."""
    mdat = next(((s, e) for typ, s, e in _boxes(data, 0, len(data)) if typ == b"mdat"), None)
    if not mdat:
        return
    i, end = mdat
    n = 0
    while i + 4 <= end and n < max_samples:
        ln = struct.unpack(">I", data[i:i + 4])[0]
        i += 4
        if ln < 1 or i + ln > end:
            break
        nal = data[i:i + ln]
        i += ln
        if nal[0] & 0x1F != 6 or len(nal) < 8:
            continue
        j = 3
        while j < len(nal) and nal[j] == 0x42:
            j += 1
        if j <= 3 or j + 1 >= len(nal) or nal[j] != 0x69:
            continue
        n += 1
        yield strip_emulation_prevention(nal[j + 1:len(nal) - 1])


def sei_track(path: Path, *, max_samples: int = 100_000) -> list[dict]:
    """One telemetry dict per SEI sample (≈ one per frame), named fields."""
    data = path.read_bytes()
    out: list[dict] = []
    for payload in iter_sei_payloads(data, max_samples=max_samples):
        try:
            pb = parse_protobuf(payload)
        except Exception:
            continue
        out.append({FIELDS.get(k, k): v for k, v in pb.items()})
    return out


def valid_fix(s: dict) -> bool:
    lat, lon = s.get("lat"), s.get("lon")
    return (isinstance(lat, float) and isinstance(lon, float)
            and abs(lat) > 0.01 and abs(lon) > 0.01 and abs(lat) <= 90 and abs(lon) <= 180)


def median_fix(samples: list[dict]) -> dict | None:
    """Median lat/lon of the valid fixes (robust to a few garbage samples).
    Returns the sample nearest the median latitude so lat/lon stay a pair."""
    fixes = [s for s in samples if valid_fix(s)]
    if not fixes:
        return None
    by_lat = sorted(fixes, key=lambda s: s["lat"])
    m = by_lat[len(by_lat) // 2]
    return {"lat": round(m["lat"], 6), "lon": round(m["lon"], 6), "fixes": len(fixes),
            "speed_mps": m.get("speed_mps")}


def last_fix(samples: list[dict]) -> dict | None:
    for s in reversed(samples):
        if valid_fix(s):
            return s
    return None
