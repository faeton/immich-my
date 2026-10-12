"""GPX + JSON track from SEI samples, with real UTC timestamps."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from xml.sax.saxutils import escape

from . import sei as sei_mod
from .model import Event


def frames_for_window(root: Path, ev: Event, a: float, b: float, *, hz: float = 1.0) -> list[dict]:
    """1 Hz (default) GPS frames covering [a,b] of the event, from the front
    segments' SEI. Each: t (timeline s), utc (ISO Z), lat, lon, speed_mps, heading."""
    out: list[dict] = []
    for seg in ev.overlapping(a, b):
        fn = seg.f.get("front")
        if not fn:
            continue
        samples = sei_mod.sei_track(root / ev.dir / fn)
        if not samples:
            continue
        fps = len(samples) / seg.cam_d("front") if seg.cam_d("front") else 36.0
        step = max(1, int(round(fps / hz)))
        base = datetime.fromisoformat(seg.utc) if seg.utc else None
        for i in range(0, len(samples), step):
            s = samples[i]
            if not sei_mod.valid_fix(s):
                continue
            t = seg.start + i / fps
            if t < a or t > b:
                continue
            utc = (base + timedelta(seconds=i / fps)).strftime("%Y-%m-%dT%H:%M:%SZ") if base else None
            out.append({"t": round(t, 2), "utc": utc, "lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
                        "speed_mps": s.get("speed_mps"), "heading": s.get("heading")})
    return out


def build_gpx(frames: list[dict], *, name: str) -> str:
    lines = ['<?xml version="1.0" encoding="UTF-8"?>',
             '<gpx version="1.1" creator="teslacam" xmlns="http://www.topografix.com/GPX/1/1">',
             "  <trk>", f"    <name>{escape(name)}</name>", "    <trkseg>"]
    for f in frames:
        t = f'<time>{f["utc"]}</time>' if f.get("utc") else ""
        lines.append(f'      <trkpt lat="{f["lat"]:.6f}" lon="{f["lon"]:.6f}">{t}</trkpt>')
    lines += ["    </trkseg>", "  </trk>", "</gpx>", ""]
    return "\n".join(lines)


def write_track(frames: list[dict], out_dir: Path, *, name: str, event_id: str) -> bool:
    if not frames:
        return False
    (out_dir / "track.gpx").write_text(build_gpx(frames, name=name))
    (out_dir / "track.json").write_text(json.dumps({
        "summary": {"source": "Tesla SEI", "event": event_id, "fixes": len(frames),
                    "start": frames[0].get("utc"), "end": frames[-1].get("utc")},
        "track": frames}, indent=1))
    return True
