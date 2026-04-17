"""Trip-wide GPS anchor from folder notes file front-matter.

When the folder's notes file (TRIP.md / IMMY.md / README.md) has
`location.coords: [lat, lon]`, apply those coords to every media file
that has no GPS. More specific rules (e.g. dji-gps-from-srt) run first
and claim their files via the CLI's per-field dedup.

Confidence is HIGH when coords are explicit — the user has already
answered. The LOW-confidence interactive path (ask when no anchor set)
lands in iteration 2a.5.
"""

from __future__ import annotations

from pathlib import Path

from ..exif import ExifRow
from ..notes import parse_frontmatter, resolve
from .dji_srt import _has_gps
from .registry import Finding, Rule, register


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    loc = fm.get("location") or {}
    coords = loc.get("coords") if isinstance(loc, dict) else None
    if not (isinstance(coords, (list, tuple)) and len(coords) == 2):
        return []
    try:
        lat, lon = float(coords[0]), float(coords[1])
    except (TypeError, ValueError):
        return []

    out: list[Finding] = []
    for row in rows:
        if _has_gps(row):
            continue
        patch = {
            "GPSLatitude": f"{lat:.6f}",
            "GPSLatitudeRef": "N" if lat >= 0 else "S",
            "GPSLongitude": f"{lon:.6f}",
            "GPSLongitudeRef": "E" if lon >= 0 else "W",
        }
        out.append(Finding(
            rule="trip-gps-anchor",
            confidence="high",
            path=row.path,
            action="write_xmp",
            patch=patch,
            reason=f"anchor from {notes.name} front-matter location.coords",
        ))
    return out


register(Rule(name="trip-gps-anchor", confidence="high", propose=_propose))
