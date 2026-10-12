"""Reverse geocoding: one polite Nominatim lookup per ~110 m cell, cached."""

from __future__ import annotations

import json
from pathlib import Path

DEFAULT_AGENT = "cull/0.1 (personal media catalogue)"


class GeoCache:
    """`<dir>/geocache.json`: "lat,lon" (3 decimals) → city. `seed` is read first."""

    def __init__(self, path: Path, seed: Path | None = None, *, agent: str = DEFAULT_AGENT):
        self.path = path
        self.agent = agent
        self.data: dict[str, str | None] = {}
        for p in (seed, path):
            if p and p.exists():
                try:
                    self.data.update(json.loads(p.read_text()))
                except Exception:
                    pass
        self.dirty = False

    @staticmethod
    def key(lat: float, lon: float) -> str:
        return f"{lat:.3f},{lon:.3f}"

    def lookup(self, lat: float, lon: float) -> str | None:
        key = self.key(lat, lon)
        if key in self.data:
            return self.data[key]
        try:
            city = _fetch_city(lat, lon, self.agent)
        except Exception:  # network/parse failure: leave the cell uncached so a later scan retries
            return None
        self.data[key] = city
        self.dirty = True
        return city

    def flush(self) -> None:
        if self.dirty:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1))
            tmp.replace(self.path)
            self.dirty = False


def _fetch_city(lat: float, lon: float, agent: str) -> str | None:
    """One Nominatim reverse lookup (1 req/s: sleeps after the call, success or not)."""
    import time
    import urllib.parse
    import urllib.request
    url = "https://nominatim.openstreetmap.org/reverse?" + urllib.parse.urlencode(
        {"lat": lat, "lon": lon, "format": "json", "zoom": 10, "accept-language": "en"})
    try:
        req = urllib.request.Request(url, headers={"User-Agent": agent})
        j = json.loads(urllib.request.urlopen(req, timeout=15).read())
        a = j.get("address", {})
        return (a.get("city") or a.get("town") or a.get("village") or a.get("municipality")
                or a.get("county") or j.get("name"))
    finally:
        time.sleep(1.1)


def nominatim_city(lat: float, lon: float, *, agent: str = DEFAULT_AGENT) -> str | None:
    """One polite Nominatim reverse lookup (1 req/s, personal use); None on any failure."""
    try:
        return _fetch_city(lat, lon, agent)
    except Exception:
        return None
