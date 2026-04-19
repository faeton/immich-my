"""Geocode `location.name` from notes into `location.coords`.

When the user writes `location: { name: "Casela, Mauritius" }` in the
folder notes but leaves `coords:` blank, ask Nominatim for the lat/lon
and merge it back into the notes front-matter. The `trip-gps-anchor`
HIGH rule then fires on the next apply pass and stamps every GPS-less
file with those coords.

Transport: stdlib `urllib.request`, 5 s timeout, single query per
distinct place string. Results cached at `~/.immy/places.yml` so a
flaky uplink on the next run still reuses the previous answer.

HIGH confidence — the user explicitly put the name there. Bad matches
are the user's problem; they can delete and retype. Silent skip on any
network or parse failure so an offline Mac doesn't spam errors.

Nominatim ToS: max 1 req/s, real User-Agent required. This tool is
single-user and interactive, so we're comfortably within policy.
"""

from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

import yaml

from ..exif import ExifRow
from ..notes import parse_frontmatter, resolve as resolve_notes
from .registry import Finding, Rule, register


CACHE_PATH = Path.home() / ".immy" / "places.yml"
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
USER_AGENT = "immy/0.1 (https://github.com/faeton/immich-my)"
TIMEOUT_SECONDS = 5


def _load_cache() -> dict[str, list[float]]:
    if not CACHE_PATH.is_file():
        return {}
    try:
        data = yaml.safe_load(CACHE_PATH.read_text()) or {}
    except yaml.YAMLError:
        return {}
    return data if isinstance(data, dict) else {}


def _save_cache(cache: dict[str, list[float]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(yaml.safe_dump(cache, sort_keys=True))


def _query_nominatim(place: str) -> tuple[float, float] | None:
    params = urllib.parse.urlencode({"q": place, "format": "json", "limit": 1})
    req = urllib.request.Request(
        f"{NOMINATIM_URL}?{params}",
        headers={"User-Agent": USER_AGENT},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            body = resp.read()
    except Exception:
        return None
    try:
        hits = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(hits, list) or not hits:
        return None
    first = hits[0]
    try:
        return float(first["lat"]), float(first["lon"])
    except (KeyError, TypeError, ValueError):
        return None


def _propose(rows: list[ExifRow], folder: Path) -> list[Finding]:
    notes = resolve_notes(folder)
    if notes is None:
        return []
    fm = parse_frontmatter(notes)
    loc = fm.get("location") or {}
    if not isinstance(loc, dict):
        return []
    name = loc.get("name")
    if not isinstance(name, str) or not name.strip():
        return []
    # Skip if user already has coords — nothing to do.
    existing = loc.get("coords")
    if isinstance(existing, (list, tuple)) and len(existing) == 2:
        return []

    key = name.strip()
    cache = _load_cache()
    coords: tuple[float, float] | None = None
    hit = cache.get(key)
    if isinstance(hit, list) and len(hit) == 2:
        try:
            coords = float(hit[0]), float(hit[1])
        except (TypeError, ValueError):
            coords = None
    if coords is None:
        coords = _query_nominatim(key)
        if coords is None:
            return []  # silent offline skip
        cache[key] = [coords[0], coords[1]]
        try:
            _save_cache(cache)
        except OSError:
            pass  # cache is opportunistic

    lat, lon = coords
    return [Finding(
        rule="geocode-place",
        confidence="high",
        path=notes,
        action="write_notes",
        patch={"location_coords": [lat, lon]},
        reason=f"Nominatim: '{key}' → ({lat:+.4f}, {lon:+.4f})",
    )]


register(Rule(name="geocode-place", confidence="high", propose=_propose))
