"""Guess the trip timezone from any geotagged file and write it to notes.

`trip-timezone` (HIGH) needs `timezone:` in notes to work. Setting it by
hand is fine for folders where `trip-gps-anchor` interactively asked
for coords — but on a drone folder where every file already has GPS in
EXIF, making the user type `Indian/Mauritius` by hand is silly. We
already know where the trip happened.

This rule reverse-looks-up the first available GPS coordinate via
`timezonefinder` (offline, no network), and if the answer is coherent
across the folder, writes `timezone: <zone>` into the notes front-
matter. `trip-timezone` HIGH then cascades it into XMP on the next
apply pass.

HIGH because tz-from-coords is unambiguous on the open ocean of the map
(which is most of it). Edge cases (Spain/Ceuta border, the Arizona
Navajo Nation, etc.) will show the zone the GPS actually sits in — if
the user wanted a different one they can edit notes. `write_notes`
action + patch hash in state.yml so re-audit is a no-op.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from ..exif import ExifRow
from ..notes import parse_frontmatter, resolve
from .registry import Finding, Rule, register


_finder = None


def _tz_finder():
    global _finder
    if _finder is None:
        from timezonefinder import TimezoneFinder
        _finder = TimezoneFinder()
    return _finder


def _signed(mag: object, ref: object, neg_refs: tuple[str, ...]) -> float | None:
    if mag is None:
        return None
    try:
        v = float(mag)
    except (TypeError, ValueError):
        return None
    if isinstance(ref, str) and ref.strip().upper() in neg_refs and v > 0:
        v = -v
    return v


def _coord(row: ExifRow) -> tuple[float, float] | None:
    # Composite is the friendly signed form, but pyexiftool's -fast2 read
    # sometimes skips composites — fall back to (magnitude, ref) from raw
    # EXIF/XMP and sign it ourselves.
    lat = row.get("Composite:GPSLatitude")
    lon = row.get("Composite:GPSLongitude")
    if lat is None or lon is None:
        lat = _signed(
            row.get("EXIF:GPSLatitude", "XMP:GPSLatitude"),
            row.get("EXIF:GPSLatitudeRef", "XMP:GPSLatitudeRef"),
            ("S",),
        )
        lon = _signed(
            row.get("EXIF:GPSLongitude", "XMP:GPSLongitude"),
            row.get("EXIF:GPSLongitudeRef", "XMP:GPSLongitudeRef"),
            ("W",),
        )
    if lat is None or lon is None:
        return None
    try:
        return float(lat), float(lon)
    except (TypeError, ValueError):
        return None


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    if isinstance(fm.get("timezone"), str) and fm["timezone"].strip():
        return []

    finder = _tz_finder()
    zones: Counter[str] = Counter()
    for row in rows:
        coord = _coord(row)
        if coord is None:
            continue
        zone = finder.timezone_at(lat=coord[0], lng=coord[1])
        if zone:
            zones[zone] += 1
    if not zones:
        return []

    # Require a clear majority — if two zones run close, stay silent and
    # let the interactive prompt decide (border-crossing trip, user picks).
    top = zones.most_common(2)
    top_zone, top_n = top[0]
    rest_n = top[1][1] if len(top) > 1 else 0
    if rest_n and top_n < rest_n * 2:
        return []

    return [Finding(
        rule="trip-timezone-guess-gps",
        confidence="high",
        path=notes,
        action="write_notes",
        patch={"timezone": top_zone},
        reason=f"{top_n}/{sum(zones.values())} geotagged file(s) land in {top_zone}",
    )]


register(Rule(name="trip-timezone-guess-gps", confidence="high", propose=_propose))
