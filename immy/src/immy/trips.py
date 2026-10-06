"""Travel albums: one Immich album per trip, found from where the library
was shot day by day.

`immy cluster` groups assets into events (hours, a few km). A trip is a
different unit: days to weeks, often several countries, and it holds the
media that carries no GPS at all (drone, action-cam and mirrorless files)
as much as the geotagged phone shots. So this module works at day
resolution:

1. **Day track.** Each local calendar day with geotagged assets gets a
   country by majority vote, then a city and position from that day's
   assets in the winning country (voting city and country independently
   lets a travel day pair one country with another's city).
2. **Home.** Days matching a configured home stay (a date window plus a
   country and/or a centre and radius) are not travel. With no homes
   configured, every day is travel: the nomad case.
3. **Runs.** The remaining days are cut into runs: a new run starts when
   the region changes, a home day intervenes, or more than `max_gap_days`
   pass with nothing geotagged. Regions come from `data/travel_regions.json`
   (Oceania, Southeast Asia, … — most European countries are their own
   region), so a month across five Pacific islands stays one trip while a
   weekend in Lisbon between two stints in Spain does not get swallowed.
4. **Transit.** A run of at most `transit_days` geotagged days that touches
   a neighbouring run is a stopover (a layover in Dubai on the way to the
   Seychelles) and merges into that neighbour.
5. **Membership.** Every live timeline asset whose local date falls inside
   a trip's first..last day joins it, geotagged or not. Trips never
   overlap, so an asset belongs to at most one.

Everything here is a pure transform over Python values; the CLI layer
(`immy trips`) owns Postgres and the Immich API.

Idempotency mirrors `immy cluster`: each album description carries an
`immy-trip:<key>` line, and a ledger under `state_root` records the assets
immy assigned plus each trip's date range, so a trip whose first day moves
(a late import) is matched back to its album instead of duplicated.
"""

from __future__ import annotations

import bisect
import hashlib
import json
import math
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

from .clustering import haversine_km


IMMY_TRIP_MARKER = "immy-trip:"
LEDGER_FILENAME = "trips-ledger.json"

DEFAULT_MAX_GAP_DAYS = 3
DEFAULT_TRANSIT_DAYS = 1
DEFAULT_MIN_ASSETS = 20
DEFAULT_TAG_ROOT = "Trips"
# An exact on-the-hour local time shared by this many assets is a fallback
# stamp (a year-only date written as `YYYY-01-01 12:00:00`), not a shot time.
DEFAULT_PLACEHOLDER_MIN = 10

_DATA = Path(__file__).parent / "data"


# --- reference data --------------------------------------------------------


@lru_cache(maxsize=1)
def _region_table() -> dict:
    return json.loads((_DATA / "travel_regions.json").read_text())


@lru_cache(maxsize=1)
def _name_to_code() -> dict[str, str]:
    """Immich's English country name → alpha-2. Immich writes
    `asset_exif.country` with i18n-iso-countries' English names, the same
    table `geocode.py` ships; synonyms (list values) all map back."""
    raw = json.loads((_DATA / "iso_countries_en.json").read_text())["countries"]
    out: dict[str, str] = {}
    for code, names in raw.items():
        for n in names if isinstance(names, list) else [names]:
            out[n] = code
    return out


def country_code(name: str | None) -> str | None:
    """Country name (or an alpha-2 code) → alpha-2, None when unknown."""
    if not name:
        return None
    if len(name) == 2 and name.isalpha():
        return name.upper()
    return _name_to_code().get(name)


def short_country(code: str, fallback: str) -> str:
    return _region_table()["short_names"].get(code, fallback)


class Regions:
    """alpha-2 → region label. A code in no region is its own region, keyed
    by its (alias-folded) code. `overrides` maps alpha-2 → label; an empty
    label makes that country a region of its own."""

    def __init__(self, overrides: dict[str, str] | None = None) -> None:
        table = _region_table()
        self._aliases: dict[str, str] = dict(table["aliases"])
        self._label: dict[str, str] = {}
        for label, codes in table["regions"].items():
            for c in codes:
                self._label[c] = label
        for code, label in (overrides or {}).items():
            code = code.upper()
            self._aliases.pop(code, None)
            if label:
                self._label[code] = label
            else:
                self._label.pop(code, None)

    def key(self, code: str) -> str:
        """The value runs compare: a region label, or a bare country code."""
        code = self._aliases.get(code, code)
        return self._label.get(code, code)

    def label(self, code: str) -> str | None:
        """Region label for naming, None for a country that is its own region."""
        return self._label.get(self._aliases.get(code, code))

    def fold(self, code: str) -> str:
        """The country a territory counts as (Gibraltar → Spain)."""
        return self._aliases.get(code, code)


# --- inputs ----------------------------------------------------------------

# One row per (local day, country, city) of trustworthy GPS. Some files carry
# the sign-flipped coordinate (-lat, -lon) of where they were shot (EXIF
# written without hemisphere refs): an Antarctic cruise reads as the Urals.
# Two assets from the same day at (lat, lon) and (-lat, -lon) can't both be
# right, so such days are detected positionally, the ghost hemisphere is
# picked by majority over a ±5-day window (a trip doesn't hop between
# antipodes mid-way, and a couple of Pacific days can run one-sided on
# their own), and every asset on the wrong side that day is dropped, not
# only the ones whose mirror still exists. A day where *every* file flipped
# has no mirror of its own; within ±5 days of a confirmed day, a wrong-side
# asset is dropped when its mirror lands within ~3° of real points from that
# window (a genuine Ushuaia → Urals hop would not mirror onto itself). Null island (0, 0) is dropped
# too: a chip with no fix, not a spot in the Atlantic.
def _ghost_ctes(tag: str, *, axis: str, lat_op: str, min_abs: float) -> str:
    """CTEs that find one class of sign-flipped GPS. `axis` is the column
    whose sign decides the hemisphere (`lat` for a full (-lat, -lon) flip,
    `lon` for a longitude-only one). A mirror's latitude satisfies
    `o.lat <lat_op> g.lat ≈ 0`: `+` for a full flip, `-` for lon-only."""
    return f"""
hemi_{tag} AS MATERIALIZED (
  SELECT d FROM gps GROUP BY d
  HAVING min({axis}) < -{min_abs} AND max({axis}) > {min_abs}
),
mirrored_{tag} AS MATERIALIZED (
  SELECT g.d, g.{axis} AS v
  FROM gps g JOIN hemi_{tag} h ON h.d = g.d
  WHERE abs(g.{axis}) > {min_abs}
    AND EXISTS (SELECT 1 FROM gps o
                WHERE o.d = g.d
                  AND abs(o.lat {lat_op} g.lat) < 0.5 AND abs(o.lon + g.lon) < 0.5)
),
affected_{tag} AS MATERIALIZED (SELECT DISTINCT d FROM mirrored_{tag}),
side_{tag} AS MATERIALIZED (
  SELECT dd.d,
         CASE WHEN count(*) FILTER (WHERE m.v < 0)
                 >= count(*) FILTER (WHERE m.v > 0) THEN -1 ELSE 1 END AS sgn
  FROM (SELECT DISTINCT d FROM gps) dd
  JOIN mirrored_{tag} m ON m.d BETWEEN dd.d - 5 AND dd.d + 5
  GROUP BY dd.d
),
kept_{tag} AS MATERIALIZED (
  SELECT DISTINCT g.d, round(g.lat) AS rlat, round(g.lon) AS rlon
  FROM gps g JOIN side_{tag} s ON s.d = g.d
  WHERE abs(g.{axis}) > {min_abs} AND sign(g.{axis}) = s.sgn
),
ghost_{tag} AS MATERIALIZED (
  SELECT g.id
  FROM gps g JOIN side_{tag} s ON s.d = g.d
  WHERE abs(g.{axis}) > {min_abs} AND sign(g.{axis}) <> s.sgn
    AND (EXISTS (SELECT 1 FROM affected_{tag} af WHERE af.d = g.d)
         OR EXISTS (SELECT 1 FROM kept_{tag} c
                    WHERE c.d BETWEEN g.d - 5 AND g.d + 5
                      AND abs(c.rlat {lat_op} g.lat) <= 3
                      AND abs(c.rlon + g.lon) <= 3))
)"""


# Placeholder dates: an importer that only knew the year (a Takeout
# "Photos from 2019" folder) stamps every such file with the same exact time,
# typically `2019-01-01 12:00:00`. Real shots don't pile up on one exact
# on-the-hour second, so a whole-hour local time shared by
# `placeholder_min`+ assets is treated as no date at all: it would otherwise
# invent a New Year's Day trip and pull those files into any real trip that
# spans the date.
_PLACEHOLDER_CTE = """
placeholder AS MATERIALIZED (
  SELECT a."localDateTime" AS t
  FROM asset a
  WHERE a."deletedAt" IS NULL
    AND a.visibility = 'timeline'
    AND a."ownerId" = %(owner)s
    AND date_trunc('hour', a."localDateTime" AT TIME ZONE 'UTC')
        = a."localDateTime" AT TIME ZONE 'UTC'
  GROUP BY 1
  HAVING count(*) >= %(placeholder_min)s
)"""

# Every live timeline asset with its local day; `placeholder` flags the
# fallback-dated ones so the caller can count and skip them.
ASSETS_SQL = f"""
WITH {_PLACEHOLDER_CTE.strip()}
SELECT a.id, (a."localDateTime" AT TIME ZONE 'UTC')::date,
       EXISTS (SELECT 1 FROM placeholder p WHERE p.t = a."localDateTime")
FROM asset a
WHERE a."deletedAt" IS NULL
  AND a.visibility = 'timeline'
  AND a."ownerId" = %(owner)s
"""

DAY_BUCKETS_SQL = f"""
WITH {_PLACEHOLDER_CTE.strip()},
gps AS MATERIALIZED (
  SELECT a.id, (a."localDateTime" AT TIME ZONE 'UTC')::date AS d,
         ae.country, ae.city, ae.latitude AS lat, ae.longitude AS lon
  FROM asset a
  JOIN asset_exif ae ON ae."assetId" = a.id
  WHERE a."deletedAt" IS NULL
    AND a.visibility = 'timeline'
    AND a."ownerId" = %(owner)s
    AND ae.latitude IS NOT NULL AND ae.longitude IS NOT NULL
    AND ae.country IS NOT NULL
    AND NOT (abs(ae.latitude) < 0.01 AND abs(ae.longitude) < 0.01)
    AND NOT EXISTS (SELECT 1 FROM placeholder p WHERE p.t = a."localDateTime")
),{_ghost_ctes("ll", axis="lat", lat_op="+", min_abs=0.5)},{_ghost_ctes("lon", axis="lon", lat_op="-", min_abs=5)}
SELECT g.d, g.country, g.city, avg(g.lat), avg(g.lon), count(*)
FROM gps g
WHERE NOT EXISTS (SELECT 1 FROM ghost_ll x WHERE x.id = g.id)
  AND NOT EXISTS (SELECT 1 FROM ghost_lon x WHERE x.id = g.id)
GROUP BY 1, 2, 3
"""


@dataclass(frozen=True)
class PlaceCount:
    """One `(day, country, city)` bucket of geotagged assets, as the CLI
    aggregates it in SQL. `lat`/`lon` are the bucket's mean position."""

    day: date
    country: str
    city: str | None
    lat: float
    lon: float
    n: int


@dataclass(frozen=True)
class Day:
    day: date
    country: str
    code: str
    city: str | None
    lat: float
    lon: float
    n: int  # geotagged assets in the winning country that day


@dataclass(frozen=True)
class HomeStay:
    """A window when a place was home. `country` and/or a centre+radius
    decide whether a day inside the window is at home; both set means both
    must match. Open-ended windows leave `start`/`end` as None."""

    name: str = "home"
    start: date | None = None
    end: date | None = None
    country: str | None = None  # alpha-2
    lat: float | None = None
    lon: float | None = None
    radius_km: float = 50.0

    def matches(self, d: Day) -> bool:
        if self.start and d.day < self.start:
            return False
        if self.end and d.day > self.end:
            return False
        if self.country and d.code != self.country:
            return False
        if self.lat is not None and self.lon is not None:
            if haversine_km(self.lat, self.lon, d.lat, d.lon) > self.radius_km:
                return False
        return bool(self.country) or self.lat is not None


def _circular_mean_lon(pairs: list[tuple[float, int]]) -> float:
    """Weighted mean longitude that survives the antimeridian: a day in Fiji
    with points at +179 and -179 should land near 180, not 0."""
    s = sum(math.sin(math.radians(lon)) * w for lon, w in pairs)
    c = sum(math.cos(math.radians(lon)) * w for lon, w in pairs)
    return math.degrees(math.atan2(s, c))


def build_days(buckets: list[PlaceCount]) -> list[Day]:
    """Collapse per-place buckets into one `Day` each, sorted by date.
    Buckets whose country Immich's name table doesn't know are ignored."""
    by_day: dict[date, list[PlaceCount]] = {}
    for b in buckets:
        if country_code(b.country):
            by_day.setdefault(b.day, []).append(b)
    days: list[Day] = []
    for d in sorted(by_day):
        rows = by_day[d]
        votes: Counter[str] = Counter()
        for b in rows:
            votes[b.country] += b.n
        # Ties break on name so the result never depends on row order.
        country = min(votes, key=lambda c: (-votes[c], c))
        mine = [b for b in rows if b.country == country]
        cities: Counter[str] = Counter()
        for b in mine:
            if b.city:
                cities[b.city] += b.n
        city = min(cities, key=lambda c: (-cities[c], c)) if cities else None
        n = sum(b.n for b in mine)
        days.append(Day(
            day=d, country=country, code=country_code(country) or "",
            city=city,
            lat=sum(b.lat * b.n for b in mine) / n,
            lon=_circular_mean_lon([(b.lon, b.n) for b in mine]),
            n=n,
        ))
    return days


# --- segmentation ----------------------------------------------------------


@dataclass
class Trip:
    days: list[Day] = field(default_factory=list)
    region: str = ""          # Regions.key of the run that founded it
    region_label: str | None = None
    asset_ids: list[str] = field(default_factory=list)
    _regions: "Regions | None" = field(default=None, repr=False)

    @property
    def start(self) -> date:
        return self.days[0].day

    @property
    def end(self) -> date:
        return self.days[-1].day

    @property
    def span_days(self) -> int:
        return (self.end - self.start).days + 1

    def countries(self) -> list[tuple[str, str]]:
        """(alpha-2, display name) by days present, then first appearance."""
        count: Counter[str] = Counter(d.code for d in self.days)
        first: dict[str, int] = {}
        names: dict[str, str] = {}
        for i, d in enumerate(self.days):
            first.setdefault(d.code, i)
            names.setdefault(d.code, d.country)
        order = sorted(count, key=lambda c: (-count[c], first[c]))
        return [(c, short_country(c, names[c])) for c in order]

    def named_countries(self) -> list[str]:
        """Countries worth putting in the album name: those in the trip's own
        region, plus any other with two or more days. A one-day stopover
        folded in as transit stays out of the name (it's in the description)."""
        count: Counter[str] = Counter(d.code for d in self.days)
        regions = self._regions
        names = [n for c, n in self.countries()
                 if count[c] >= 2 or (regions.key(c) if regions else c) == self.region]
        return names or [n for _, n in self.countries()]

    def top_city(self) -> str | None:
        """The city of a trip that mostly stayed put: one city holding at
        least half the geotagged days. A tour across many towns gets none,
        rather than being named after whichever one won by a day."""
        cities = Counter(d.city for d in self.days if d.city)
        if not cities:
            return None
        city, n = min(cities.items(), key=lambda kv: (-kv[1], kv[0]))
        # Immich's geodata labels some places "Geroskípou (quarter)".
        return re.sub(r"\s*\([^)]*\)$", "", city) if n * 2 >= len(self.days) else None

    def name(self) -> str:
        return name_for_trip(self)

    def key(self) -> str:
        return stable_key(self.region, self.start)

    def legs(self) -> list["Leg"]:
        """The itinerary: one leg per stretch in one country, in order.

        A leg runs from its first geotagged day to the day before the next
        leg starts, so the photo-less days between (flights, drone-only days)
        belong somewhere and every date in the trip is in exactly one leg. A
        one-day blip inside a country (a day trip over a border and back)
        doesn't break that country's leg. Territories count as their country.
        """
        fold = self._regions.fold if self._regions else (lambda c: c)
        runs: list[list[Day]] = []
        for d in self.days:
            if runs and fold(runs[-1][-1].code) == fold(d.code):
                runs[-1].append(d)
            else:
                runs.append([d])
        # Fold a one-day run sandwiched by the same country back into it.
        i = 1
        while i < len(runs) - 1:
            prev, cur, nxt = runs[i - 1], runs[i], runs[i + 1]
            if len(cur) == 1 and fold(prev[0].code) == fold(nxt[0].code):
                runs[i - 1] = prev + cur + nxt
                del runs[i:i + 2]
            else:
                i += 1
        legs: list[Leg] = []
        for j, run in enumerate(runs):
            end = runs[j + 1][0].day - timedelta(days=1) if j + 1 < len(runs) else self.end
            code = fold(run[0].code)
            name = next((d.country for d in run if d.code == code), run[0].country)
            legs.append(Leg(code=code, country=short_country(code, name),
                            start=run[0].day, end=end))
        return legs


@dataclass(frozen=True)
class Leg:
    code: str
    country: str
    start: date
    end: date

    def label(self) -> str:
        return f"{self.country} · {format_range(self.start, self.end)}"

    def short_label(self) -> str:
        """`Tonga · 2–7 Oct`: the year is already on the trip."""
        return f"{self.country} · {format_range(self.start, self.end).rsplit(' ', 1)[0]}" \
            if self.start.year == self.end.year else self.label()


def _home_between(a: date, b: date, home_days: list[date]) -> bool:
    """Any home day strictly between `a` and `b` (sorted list)."""
    i = bisect.bisect_right(home_days, a)
    return i < len(home_days) and home_days[i] < b


def segment(
    days: list[Day],
    *,
    homes: list[HomeStay] | None = None,
    regions: Regions | None = None,
    max_gap_days: int = DEFAULT_MAX_GAP_DAYS,
    transit_days: int = DEFAULT_TRANSIT_DAYS,
) -> list[Trip]:
    """Cut the day track into trips (no size filter; see `keep`)."""
    regions = regions or Regions()
    homes = homes or []
    away: list[Day] = []
    home_days: list[date] = []
    for d in sorted(days, key=lambda d: d.day):
        if any(h.matches(d) for h in homes):
            home_days.append(d.day)
        else:
            away.append(d)

    def joinable(a: Trip, b: Trip, gap_limit: int) -> bool:
        gap = (b.start - a.end).days - 1
        return gap <= gap_limit and not _home_between(a.end, b.start, home_days)

    runs: list[Trip] = []
    for d in away:
        key = regions.key(d.code)
        cur = runs[-1] if runs else None
        probe = Trip(days=[d], region=key)
        if cur and cur.region == key and joinable(cur, probe, max_gap_days):
            cur.days.append(d)
        else:
            runs.append(Trip(days=[d], region=key, region_label=regions.label(d.code),
                             _regions=regions))

    # Stopovers: fold a short run into a neighbour it touches (≤ 1 empty day
    # between, no home day). Smallest first, so a two-day stop can't absorb
    # a one-day one and then itself survive as a fake trip. After each fold,
    # re-join same-region neighbours the stopover had been splitting.
    changed = True
    while changed:
        changed = False
        for run in sorted(runs, key=lambda r: (len(r.days), r.start)):
            if len(run.days) > transit_days or len(runs) < 2:
                continue
            i = runs.index(run)
            cands = []
            for j in (i - 1, i + 1):
                if 0 <= j < len(runs):
                    other = runs[j]
                    a, b = (other, run) if j < i else (run, other)
                    if len(other.days) > len(run.days) and joinable(a, b, 1):
                        gap = (b.start - a.end).days
                        cands.append((gap, -len(other.days), j))
            if not cands:
                continue
            _, _, j = min(cands)
            target = runs[j]
            target.days = sorted(target.days + run.days, key=lambda d: d.day)
            runs.pop(i)
            changed = True
            break
        if changed:
            merged: list[Trip] = []
            for r in runs:
                if merged and merged[-1].region == r.region and joinable(merged[-1], r, max_gap_days):
                    merged[-1].days.extend(r.days)
                else:
                    merged.append(r)
            runs = merged
    return runs


def assign_assets(trips: list[Trip], assets: list[tuple[str, date]]) -> None:
    """Fill each trip's `asset_ids` with every asset dated first..last day."""
    ordered = sorted(trips, key=lambda t: t.start)
    starts = [t.start for t in ordered]
    for t in ordered:
        t.asset_ids = []
    for asset_id, day in assets:
        i = bisect.bisect_right(starts, day) - 1
        if i >= 0 and day <= ordered[i].end:
            ordered[i].asset_ids.append(asset_id)


def assets_by_leg(trip: Trip, assets: dict[str, date]) -> list[tuple["Leg", list[str]]]:
    """Split a trip's assets across its legs by date."""
    legs = trip.legs()
    out: list[tuple[Leg, list[str]]] = [(leg, []) for leg in legs]
    starts = [leg.start for leg in legs]
    for aid in trip.asset_ids:
        day = assets.get(aid)
        if day is None:
            continue
        i = max(bisect.bisect_right(starts, day) - 1, 0)
        out[i][1].append(aid)
    return out


def keep(trip: Trip, *, min_assets: int = DEFAULT_MIN_ASSETS) -> bool:
    return len(trip.asset_ids) >= min_assets


# --- naming / identity -----------------------------------------------------


def format_range(start: date, end: date) -> str:
    """`15 Apr 2024` / `15–17 Apr 2024` / `29 Apr – 3 May 2024`."""
    if start == end:
        return start.strftime("%-d %b %Y")
    if (start.year, start.month) == (end.year, end.month):
        return f"{start.strftime('%-d')}–{end.strftime('%-d %b %Y')}"
    if start.year == end.year:
        return f"{start.strftime('%-d %b')} – {end.strftime('%-d %b %Y')}"
    return f"{start.strftime('%-d %b %Y')} – {end.strftime('%-d %b %Y')}"


def name_for_trip(trip: Trip, *, max_countries: int = 3) -> str:
    """`2025-10 Oceania · Vanuatu, Fiji, Tonga +3`, `2024-05 Namibia`,
    `2026-07 Spain · Valencia`. The `YYYY-MM` prefix keeps Immich's
    album list in travel order; it is the month the trip started."""
    names = trip.named_countries()
    prefix = trip.start.strftime("%Y-%m")
    if len(names) == 1:
        city = trip.top_city()
        return f"{prefix} {names[0]}" + (f" · {city}" if city else "")
    shown = ", ".join(names[:max_countries])
    more = f" +{len(names) - max_countries}" if len(names) > max_countries else ""
    if trip.region_label:
        return f"{prefix} {trip.region_label} · {shown}{more}"
    return f"{prefix} {shown}{more}"


def stable_key(region: str, start: date) -> str:
    return hashlib.sha1(f"{region}|{start.isoformat()}".encode()).hexdigest()[:12]


def marker_line(key: str) -> str:
    return f"{IMMY_TRIP_MARKER}{key}"


def extract_key(description: str | None) -> str | None:
    if not description:
        return None
    for line in description.splitlines():
        s = line.strip()
        if s.startswith(IMMY_TRIP_MARKER):
            return s[len(IMMY_TRIP_MARKER):].strip() or None
    return None


def description_for(trip: Trip) -> str:
    """Dates, then the itinerary one leg per line when there is more than
    one country, then the marker:

        2 Oct – 11 Dec 2025 · 71 days · 6 countries
        Tonga · 2–7 Oct
        Fiji · 8–9 Oct
        …
        immy-trip:3f9c1a2b7d40
    """
    legs = trip.legs()
    head = f"{format_range(trip.start, trip.end)} · {trip.span_days} days"
    countries = {leg.code for leg in legs}
    if len(legs) == 1:
        return f"{head} · {legs[0].country}\n{marker_line(trip.key())}"
    lines = [f"{head} · {len(countries)} countries"]
    lines += [leg.short_label() for leg in legs]
    lines.append(marker_line(trip.key()))
    return "\n".join(lines)


def _tag_segment(text: str) -> str:
    # `/` is Immich's tag hierarchy separator.
    return text.replace("/", "-")


def tag_for(trip: Trip, root: str = DEFAULT_TAG_ROOT) -> str:
    """Hierarchical tag `Trips/2025/<album name>`: albums can't nest in
    Immich, tags can, so the year → trip tree lives here."""
    return f"{root}/{trip.start.year}/{_tag_segment(trip.name())}"


def leg_tags(trip: Trip, root: str = DEFAULT_TAG_ROOT) -> list[tuple["Leg", str]]:
    """`Trips/2025/<album name>/Tonga · 2–7 Oct 2025` per leg, or just the
    trip tag for a one-country trip. Immich resolves a parent tag through
    its closure table, so tagging only the most specific level still lists
    every leg's assets under the trip."""
    legs = trip.legs()
    base = tag_for(trip, root)
    if len(legs) == 1:
        return [(legs[0], base)]
    return [(leg, f"{base}/{_tag_segment(leg.label())}") for leg in legs]


# --- durable tagging --------------------------------------------------------
#
# Immich's tag API is not safe on read-only originals. Attaching a tag locks
# `asset_exif.tags` and queues a SidecarWrite. That write can't land, unlocks
# the field anyway and queues a metadata re-extraction. The re-extraction
# then replaces the asset's tags with what the files say: none of ours.
# Observed live: thousands of trip tags gone within minutes. So the link, the
# tag list and its lock go in together through SQL, and no job ever unlocks
# them. A later extraction skips the locked list and rebuilds the links from
# it (`applyTagList`). Tags themselves are still created through the API
# (`PUT /api/tags`), which queues nothing per asset.

LOCK_TAG_SQL = """
UPDATE asset_exif SET
  tags = (SELECT array(SELECT DISTINCT unnest(coalesce(tags, '{}') || ARRAY[%(value)s]::varchar[]))),
  "lockedProperties" = (SELECT array(SELECT DISTINCT unnest(
      coalesce("lockedProperties", '{}') || ARRAY['tags']::varchar[])))
WHERE "assetId" = %(asset)s
"""

LINK_TAG_SQL = """
INSERT INTO tag_asset ("assetId", "tagId") VALUES (%(asset)s, %(tag)s)
ON CONFLICT DO NOTHING
"""


def link_tags(conn, links: list[tuple[str, str, str]]) -> None:
    """(asset id, tag id, tag value) → linked and locked, in one commit."""
    with conn.cursor() as cur:
        params = [{"asset": a, "tag": t, "value": v} for a, t, v in links]
        cur.executemany(LOCK_TAG_SQL, params)
        cur.executemany(LINK_TAG_SQL, params)
    conn.commit()


# --- ledger ----------------------------------------------------------------
#
# key → {start, end, region, assets}. `assets` is what immy last put in the
# album (prune only ever removes those); start/end/region let a trip whose
# key changed (first day moved) find its old album by overlap.


def load_ledger(path: Path) -> dict[str, dict]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    trips = data.get("trips") if isinstance(data, dict) else None
    return trips if isinstance(trips, dict) else {}


def save_ledger(path: Path, ledger: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps({"schema": 1, "trips": dict(sorted(ledger.items()))}, indent=1))
    os.replace(tmp, path)


def ledger_match(trip: Trip, ledger: dict[str, dict], taken: set[str]) -> str | None:
    """An earlier key for this trip: same region, overlapping dates."""
    for key, entry in ledger.items():
        if key in taken or entry.get("region") != trip.region:
            continue
        try:
            s, e = date.fromisoformat(entry["start"]), date.fromisoformat(entry["end"])
        except (KeyError, ValueError):
            continue
        if s <= trip.end and trip.start <= e:
            return key
    return None


__all__ = [
    "IMMY_TRIP_MARKER", "LEDGER_FILENAME",
    "DEFAULT_PLACEHOLDER_MIN", "ASSETS_SQL", "DAY_BUCKETS_SQL",
    "DEFAULT_MAX_GAP_DAYS", "DEFAULT_TRANSIT_DAYS", "DEFAULT_MIN_ASSETS", "DEFAULT_TAG_ROOT",
    "Regions", "PlaceCount", "Day", "HomeStay", "Trip",
    "country_code", "build_days", "segment", "assign_assets", "keep",
    "name_for_trip", "format_range", "stable_key", "marker_line", "extract_key",
    "Leg", "assets_by_leg", "leg_tags", "link_tags", "LOCK_TAG_SQL", "LINK_TAG_SQL",
    "Leg", "assets_by_leg", "leg_tags", "link_tags", "LOCK_TAG_SQL", "LINK_TAG_SQL",
    "description_for", "tag_for", "load_ledger", "save_ledger", "ledger_match",
]
