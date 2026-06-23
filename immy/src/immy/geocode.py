"""Reverse-geocode coordinates to (country, state, city) using Immich's own
local geodata — so a clip we geotag from its SRT gets place names *identical*
to every other asset Immich geocoded itself.

Immich (v2.7.5) only reverse-geocodes coords it reads fresh from a file's
EXIF. Our drone videos carry GPS only in the sidecar `.SRT`, the originals
are read-only, and locked coords are never re-extracted — so Immich will
never geocode them. We replicate `MapRepository.reverseGeocode` here against
the same Postgres: nearest `geodata_places` row within
`reverseGeocodeMaxDistance` (25 km, via the `earthdistance` extension), then
a `naturalearth_countries` polygon fallback for country-only.

`countryCode`/`admin_a3` → English country name via the vendored
i18n-iso-countries 7.6.0 'en' dataset (`getName` = first entry of the list).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import psycopg

# matches immich server/src/constants.ts (v2.7.5): export const
# reverseGeocodeMaxDistance = 25_000  (meters)
REVERSE_GEOCODE_MAX_DISTANCE = 25_000

_DATA = Path(__file__).parent / "data"


@dataclass
class Place:
    country: str | None = None
    state: str | None = None
    city: str | None = None

    def is_empty(self) -> bool:
        return not (self.country or self.state or self.city)


@lru_cache(maxsize=1)
def _country_names() -> dict[str, str]:
    """alpha-2 → English name. i18n-iso-countries stores some values as a
    list of synonyms; `getName` returns the first, so we do too."""
    raw = json.loads((_DATA / "iso_countries_en.json").read_text())["countries"]
    return {k: (v[0] if isinstance(v, list) else v) for k, v in raw.items()}


@lru_cache(maxsize=1)
def _alpha3_to_alpha2() -> dict[str, str]:
    rows = json.loads((_DATA / "iso_country_codes.json").read_text())
    return {r[1]: r[0] for r in rows if len(r) >= 2}


def country_name(code: str | None) -> str | None:
    """ISO 3166-1 alpha-2 *or* alpha-3 code → English name (matches Immich's
    `i18n-iso-countries getName(code, 'en')`)."""
    if not code:
        return None
    code = code.upper()
    if len(code) == 3:
        code = _alpha3_to_alpha2().get(code, code)
    return _country_names().get(code)


# Nearest place within the box, ordered by true earth distance — verbatim port
# of immich's geodata_places query.
_NEAREST_SQL = """
SELECT name, "admin1Name", "countryCode"
FROM geodata_places
WHERE earth_box(ll_to_earth_public(%(lat)s, %(lon)s), %(maxd)s)
      @> ll_to_earth_public(latitude, longitude)
ORDER BY earth_distance(
  ll_to_earth_public(%(lat)s, %(lon)s),
  ll_to_earth_public(latitude, longitude))
LIMIT 1
"""

# Country-only fallback: which natural-earth polygon contains the point.
_FALLBACK_SQL = """
SELECT admin_a3
FROM naturalearth_countries
WHERE coordinates @> point(%(lon)s, %(lat)s)
LIMIT 1
"""


def reverse_geocode(
    conn: psycopg.Connection,
    lat: float,
    lon: float,
    *,
    max_distance: int = REVERSE_GEOCODE_MAX_DISTANCE,
) -> Place:
    """Coords → Place via Immich's own geodata. Empty Place if nothing matches
    (e.g. mid-ocean beyond any country polygon)."""
    row = conn.execute(
        _NEAREST_SQL, {"lat": lat, "lon": lon, "maxd": max_distance}
    ).fetchone()
    if row is not None:
        name, admin1, cc = row
        return Place(country=country_name(cc), state=admin1, city=name)
    row = conn.execute(_FALLBACK_SQL, {"lat": lat, "lon": lon}).fetchone()
    if row is not None:
        return Place(country=country_name(row[0]))
    return Place()
