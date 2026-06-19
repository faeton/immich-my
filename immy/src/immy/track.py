"""Write a parsed DJI track to GPX + JSON sidecars.

GPX is for map/route tools (and round-trips through
`rules.geotag_from_gpx._parse_gpx` — same 1.1 namespace, `lat`/`lon`
attrs, `<time>` child). JSON keeps the full per-frame telemetry —
including camera settings GPX has no place for — plus a summary header.

Times come from the drone's own clock and are emitted naive (GPX gets a
trailing `Z` only because the format wants one); they are not timezone-
corrected. The geotag path reads the track via `srt.parse_track`
directly, so this caveat only affects external consumers of the GPX.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

from .srt import SrtFrame


def _iso(dt: datetime | None) -> str | None:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") if dt is not None else None


def build_gpx(frames: list[SrtFrame], *, name: str = "", creator: str = "immy") -> str:
    """GPX 1.1 document for every frame that carries a GPS fix."""
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gpx version="1.1" creator="{escape(creator)}" '
        'xmlns="http://www.topografix.com/GPX/1/1">',
        "  <trk>",
    ]
    if name:
        lines.append(f"    <name>{escape(name)}</name>")
    lines.append("    <trkseg>")
    for f in frames:
        if not f.has_fix():
            continue
        pt = f'      <trkpt lat="{f.latitude:.7f}" lon="{f.longitude:.7f}">'
        inner = ""
        if f.ele is not None:
            inner += f"<ele>{f.ele:.3f}</ele>"
        iso = _iso(f.datetime)
        if iso is not None:
            inner += f"<time>{iso}Z</time>"
        lines.append(pt + inner + "</trkpt>" if inner else pt + "</trkpt>")
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(lines)


def _frame_dict(f: SrtFrame) -> dict:
    return {
        "index": f.index,
        "t_offset_s": f.t_offset_s,
        "datetime": _iso(f.datetime),
        "latitude": f.latitude,
        "longitude": f.longitude,
        "rel_alt": f.rel_alt,
        "abs_alt": f.abs_alt,
        "iso": f.iso,
        "shutter": f.shutter,
        "fnum": f.fnum,
        "ev": f.ev,
        "focal_len": f.focal_len,
    }


def build_json(frames: list[SrtFrame]) -> dict:
    """Full per-frame telemetry plus a summary header."""
    fixes = [f for f in frames if f.has_fix()]
    dts = [f.datetime for f in frames if f.datetime is not None]
    rels = [f.rel_alt for f in frames if f.rel_alt is not None]
    abss = [f.abs_alt for f in frames if f.abs_alt is not None]
    takeoff = fixes[0] if fixes else None
    summary = {
        "source": "DJI .SRT",
        "frames": len(frames),
        "fixes": len(fixes),
        "start_time": _iso(dts[0]) if dts else None,
        "end_time": _iso(dts[-1]) if dts else None,
        "takeoff": (
            {"latitude": takeoff.latitude, "longitude": takeoff.longitude}
            if takeoff else None
        ),
        "rel_alt_min": min(rels) if rels else None,
        "rel_alt_max": max(rels) if rels else None,
        "abs_alt_min": min(abss) if abss else None,
        "abs_alt_max": max(abss) if abss else None,
    }
    return {"summary": summary, "track": [_frame_dict(f) for f in frames]}


def write_gpx(frames: list[SrtFrame], dest: Path, *, name: str = "") -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(build_gpx(frames, name=name))


def write_json(frames: list[SrtFrame], dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(build_json(frames), indent=2))
